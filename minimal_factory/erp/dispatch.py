"""ERP 下发发送方，读取订单、保存完整报文，再通过 HTTP 交给 MES。"""

import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from mes.schemas import DispatchEnvelope, DispatchHTTP, NativeDispatchPayload, ProtocolRouteStep
from mes.service import BusinessError, require

from .database import connect
from .http_trace import TraceHTTPHandler, read_response_body, logger


DEFAULT_EQUIPMENT = {"assembly": "E-ASM", "inspection": "E-QC", "transport": "E-MOVE"}
DEFAULT_MES_URL = "http://127.0.0.1:8000"


def prepare_message(path, data, mes_base_url=DEFAULT_MES_URL):
    """订单快照在 ERP 侧读取，重试使用第一次保存的同一份报文。"""
    request_json = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        saved = conn.execute(
            "SELECT request_json,envelope_json FROM erp_dispatch_outbox WHERE message_id=?",
            (data.message_id,),
        ).fetchone()
        if saved:
            require(saved["request_json"] == request_json, "同一消息编号不能对应不同下发内容")
            return DispatchEnvelope.model_validate_json(saved["envelope_json"])

        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (data.order_id,)).fetchone()
        require(order is not None, "来源订单不存在", 404)
        require(order["status"] == "pending", "这笔订单已经下发，请刷新工单列表")
        route = [ProtocolRouteStep(**step.model_dump(), equipment_id=DEFAULT_EQUIPMENT[step.kind])
                 for step in data.steps]
        envelope = DispatchEnvelope(
            protocol="HTTP-REST",
            message_id=data.message_id,
            timestamp=datetime.now(timezone.utc),
            source="ERP", target="MES", action="DISPATCH_ORDER",
            http=DispatchHTTP(url=mes_base_url.rstrip("/") + "/mes/dispatch"),
            payload=NativeDispatchPayload(
                order_id=order["order_id"], product_code=order["product_id"],
                product_name=order["product_name"], qty=order["quantity"],
                due_date=order["due_date"], created_at=order["created_at"], route_steps=route,
                priority=data.priority, planned_date=data.planned_date,
            ),
        )
        conn.execute("""
            INSERT INTO erp_dispatch_outbox(message_id,order_id,request_json,envelope_json)
            VALUES (?,?,?,?)
        """, (data.message_id, data.order_id, request_json, envelope.model_dump_json()))
        return envelope


def send_order(path, data, mes_base_url):
    """提交 ERP 发送记录后释放数据库连接，再发起实际 HTTP 请求。"""
    envelope = prepare_message(path, data, mes_base_url)
    url = envelope.http.url if envelope.http else mes_base_url.rstrip("/") + "/mes/dispatch"
    request = Request(
        url,
        data=envelope.model_dump_json().encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    # 这些头由 Request/http.client 实际发送；详细字节另由 TraceHTTPHandler 记录。
    from urllib.parse import urlsplit
    request.add_header("Host", urlsplit(url).netloc)
    request.add_header("Content-Length", str(len(request.data)))
    request.add_header("Accept-Encoding", "identity")
    request.add_header("Connection", "close")
    request.add_header("User-Agent", "minimal-factory-erp/1.0")
    exchange = {"url": url, "method": "POST", "request_headers": dict(request.header_items()),
                "sent_at": datetime.now(timezone.utc).isoformat(), "status_code": None}
    def record_exchange():
        with connect(path) as conn:
            conn.execute("UPDATE erp_dispatch_outbox SET http_json=? WHERE message_id=?",
                         (json.dumps(exchange, ensure_ascii=False), data.message_id))
    record_exchange()
    logger.info("[ERP->MES %s] Endpoint: POST %s", data.message_id, url)
    try:
        # 本地 ERP→MES 直连，不使用操作系统代理。
        with build_opener(ProxyHandler({}), TraceHTTPHandler(data.message_id)).open(request, timeout=15) as response:
            body = read_response_body(response, data.message_id)
            exchange.update(status_code=response.status, response_headers=dict(response.getheaders()),
                            response_body=body.decode("utf-8"))
            record_exchange()
            result = json.loads(body)
    except HTTPError as error:
        body = read_response_body(error, data.message_id)
        exchange.update(status_code=error.code, response_headers=dict(error.headers),
                        response_body=body.decode("utf-8", errors="replace"))
        record_exchange()
        try:
            detail = json.loads(body).get("detail", "MES 拒绝了下发报文")
        except (ValueError, AttributeError):
            detail = "MES 返回了异常响应"
        raise BusinessError(str(detail), error.code) from error
    except (URLError, OSError) as error:
        exchange["error"] = str(error)
        record_exchange()
        raise BusinessError("MES 通信失败，报文已保存。请在当前弹窗重试，同一消息不会重复建单", 502) from error
    except ValueError as error:
        raise BusinessError("MES 响应格式异常，报文已保存，可在当前弹窗重试", 502) from error

    require(isinstance(result, dict) and result.get("order_id") == data.order_id
            and bool(result.get("work_order_id")), "MES 响应缺少匹配的工单信息", 502)
    with connect(path) as conn:
        # 分库部署时也将 ERP 来源订单推进为已下发。
        if result.get("status") != "cancelled":
            conn.execute("UPDATE orders SET status='dispatched' WHERE order_id=? AND status='pending'",
                         (data.order_id,))
        conn.execute("UPDATE erp_dispatch_outbox SET acknowledged_at=? WHERE message_id=?",
                     (datetime.now(timezone.utc).isoformat(), data.message_id))
    return result
