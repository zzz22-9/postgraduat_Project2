"""使用实际本地 HTTP 和临时数据库验证 ERP→MES 报文下发。"""

import copy
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

import httpx
import uvicorn

from erp import dispatch as sender
from erp.app import create_app
from mes import service
from mes.schemas import ERPDispatchRequest


class DispatchProtocolTests(unittest.TestCase):
    evidence_dir = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shared.db"
        self.url = self.start_server(self.path)
        self.client = httpx.Client(base_url=self.url, timeout=20, trust_env=False)
        self.addCleanup(self.client.close)

    def start_server(self, path, mes_url=None):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        app = create_app(path, mes_base_url=mes_url or url)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop():
            server.should_exit = True
            thread.join(10)
            listener.close()
            self.assertFalse(thread.is_alive(), "测试服务未退出")

        thread.start()
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(server.started, "测试 HTTP 服务未启动")
        return url

    def create_order(self, client=None):
        response = (client or self.client).post("/orders", json={
            "product_id": "P001", "product_name": "法兰", "quantity": 2, "due_date": "2026-12-01",
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def instruction(self, order):
        return {"order_id": order["order_id"], "message_id": str(uuid4()), "priority": 2,
                "planned_date": None, "steps": [
                    {"name": "组装", "kind": "assembly", "minutes": 3},
                    {"name": "过程质检", "kind": "inspection", "minutes": 2},
                    {"name": "搬运", "kind": "transport", "minutes": 1},
                ]}

    def send(self, data, client=None):
        return (client or self.client).post(f"/orders/{data['order_id']}/dispatch", json=data)

    def query(self, sql, path=None):
        with closing(sqlite3.connect(path or self.path)) as conn:
            return conn.execute(sql).fetchall()

    def save_evidence(self, name, path):
        if self.evidence_dir:
            with closing(sqlite3.connect(path)) as source:
                with closing(sqlite3.connect(self.evidence_dir / name)) as target:
                    source.backup(target)

    def test_shared_database_real_http_and_complete_event(self):
        order = self.create_order()
        data = self.instruction(order)
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            response = self.send(data)
        self.assertEqual(response.status_code, 201, response.text)
        trace = "\n".join(record.getMessage() for record in captured.records)
        self.assertIn("> POST /mes/dispatch HTTP/1.1", trace)
        self.assertIn("> Host: ", trace)
        self.assertIn("> Content-Type: application/json", trace)
        self.assertIn("> Content-Length: ", trace)
        self.assertIn("< HTTP/1.1 201 Created", trace)
        self.assertIn("< content-type: application/json", trace.lower())
        self.assertIn('"protocol":"HTTP-REST"', trace)
        work_order = response.json()
        self.assertEqual(work_order["order_id"], order["order_id"])
        self.assertEqual(work_order["status"], "draft")
        self.assertEqual([op["equipment_id"] for op in work_order["operations"]], ["E-ASM", "E-QC", "E-MOVE"])
        self.assertEqual([op["minutes"] for op in work_order["operations"]], [3, 2, 1])
        self.assertTrue(all(op["status"] == "pending" for op in work_order["operations"]))
        self.assertEqual(len(work_order["events"]), 1)
        event = work_order["events"][0]
        self.assertEqual(event["action"], "erp_to_mes_protocol_dispatched")
        envelope = json.loads(event["detail"])
        self.assertEqual(envelope["protocol"], "HTTP-REST")
        self.assertEqual(envelope["message_id"], data["message_id"])
        self.assertEqual(envelope["payload"]["qty"], 2)
        outgoing, acknowledged = self.query("SELECT envelope_json,acknowledged_at FROM erp_dispatch_outbox")[0]
        self.assertEqual(json.loads(outgoing), envelope)
        self.assertTrue(acknowledged)
        self.assertEqual(self.client.get(f"/orders/{order['order_id']}").json()["status"], "dispatched")
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])
        html = self.client.get("/").text
        self.assertIn("/orders/${encodeURIComponent(id)}/dispatch", html)
        self.assertIn("查看完整协议报文", html)
        if self.evidence_dir:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            (self.evidence_dir / "ERP到MES_JSON消息体.json").write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
            (self.evidence_dir / "HTTP请求与响应.log").write_text(trace + "\n", encoding="utf-8")
            (self.evidence_dir / "MES响应与事件.json").write_text(
                json.dumps(work_order, ensure_ascii=False, indent=2), encoding="utf-8")
        self.save_evidence("shared_test.db", self.path)

    def test_http_trace_includes_rejected_response(self):
        data = self.instruction(self.create_order())
        self.client.patch("/mes/equipment/E-ASM", json={"status": "maintenance", "reason": "test"})
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            response = self.send(data)
        self.assertEqual(response.status_code, 409, response.text)
        trace = "\n".join(record.getMessage() for record in captured.records)
        self.assertIn("< HTTP/1.1 409 Conflict", trace)
        self.assertIn('"detail"', trace)
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders"), [(0,)])

    def test_connector_feed_orders_outbox_and_alias_are_read_only(self):
        order = self.create_order()
        data = self.instruction(order)
        self.assertEqual(self.send(data).status_code, 201)
        before = self.path.read_bytes()
        feed = self.client.get("/erp/feed")
        self.assertEqual(feed.status_code, 200, feed.text)
        self.assertEqual(feed.json(), self.client.get("/orders/dispatch-outbox").json())
        self.assertEqual(len(self.client.get("/orders").json()), 1)
        self.assertEqual(self.client.get(f"/orders/{order['order_id']}").status_code, 200)
        item = feed.json()["dispatch_outbox"][0]
        self.assertEqual(item["message_id"], data["message_id"])
        self.assertEqual(item["endpoint"], self.url + "/mes/dispatch")
        self.assertEqual(item["http"]["status_code"], 201)
        self.assertEqual(item["http"]["method"], "POST")
        self.assertIn("application/json", str(item["http"]["request_headers"]))
        self.assertEqual(item["payload"]["qty"], 2)
        self.assertEqual(item["envelope"]["payload"], item["payload"])
        self.assertEqual(item["delivery_state"], "acknowledged")
        self.assertEqual(self.client.post("/erp/feed").status_code, 405)
        self.assertEqual(self.path.read_bytes(), before)

    def test_connector_preserves_legacy_payload_without_fabricating_http_status(self):
        data = ERPDispatchRequest.model_validate(self.instruction(self.create_order()))
        current = sender.prepare_message(self.path, data).model_dump(mode="json")
        native = current["payload"]
        legacy = {**current, "protocol": "HTTP-REST/ISA95-Lite-v1", "payload": {
            "order": {"order_id": native["order_id"], "product_id": native["product_code"],
                      "product_name": native["product_name"], "quantity": native["qty"],
                      "due_date": native["due_date"], "created_at": native["created_at"], "status": "pending"},
            "route": native["route_steps"], "priority": native["priority"], "planned_date": native["planned_date"]}}
        legacy.pop("http")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE erp_dispatch_outbox SET envelope_json=?", (json.dumps(legacy),))
        first = self.client.post("/mes/dispatch", json=legacy)
        second = self.client.post("/mes/dispatch", json=legacy)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["work_order_id"], second.json()["work_order_id"])
        item = self.client.get("/erp/feed").json()["dispatch_outbox"][0]
        self.assertEqual(item["payload"], legacy["payload"])
        self.assertIsNone(item["http"])
        self.assertIsNone(item["endpoint"])

    def test_separate_erp_and_mes_databases_over_real_http(self):
        mes_path = Path(self.temp.name) / "mes.db"
        erp_path = Path(self.temp.name) / "erp.db"
        mes_url = self.start_server(mes_path)
        erp_url = self.start_server(erp_path, mes_url)
        with httpx.Client(base_url=erp_url, trust_env=False, timeout=20) as client:
            order = self.create_order(client)
            self.assertEqual(self.query("SELECT COUNT(*) FROM orders", mes_path), [(0,)])
            response = self.send(self.instruction(order), client)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["quantity"], 2)
            self.assertEqual(client.get(f"/orders/{order['order_id']}").json()["status"], "dispatched")
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders", erp_path), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders", mes_path), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM operations", mes_path), [(3,)])
        self.assertEqual(self.query("SELECT quantity FROM orders", mes_path), [(2,)])
        if self.evidence_dir:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.save_evidence("split_erp_test.db", erp_path)
        self.save_evidence("split_mes_test.db", mes_path)

    def test_concurrent_retries_and_conflicting_message(self):
        data = self.instruction(self.create_order())
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.send(data), range(2)))
        self.assertEqual([r.status_code for r in responses], [201, 201])
        self.assertEqual(responses[0].json()["work_order_id"], responses[1].json()["work_order_id"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(1,)])
        self.assertEqual(self.send({**data, "priority": 1}).status_code, 409)
        envelope = json.loads(self.query("SELECT envelope_json FROM mes_dispatch_inbox")[0][0])
        envelope["payload"]["qty"] = 3
        self.assertEqual(self.client.post("/mes/dispatch", json=envelope).status_code, 409)

    def test_lost_http_response_retries_saved_message_without_duplicate(self):
        data = self.instruction(self.create_order())
        opener = build_opener(ProxyHandler({}))

        def lose_response(request, timeout):
            with opener.open(request, timeout=timeout) as response:
                response.read()
            raise URLError("test: response lost after MES committed")

        with patch("erp.dispatch.build_opener") as factory:
            factory.return_value.open.side_effect = lose_response
            self.assertEqual(self.send(data).status_code, 502)
        saved = self.query("SELECT envelope_json,acknowledged_at FROM erp_dispatch_outbox")[0]
        self.assertIsNone(saved[1])
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders"), [(1,)])
        retry = self.send(data)
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders"), [(1,)])
        after = self.query("SELECT envelope_json,acknowledged_at FROM erp_dispatch_outbox")[0]
        self.assertEqual(after[0], saved[0])
        self.assertTrue(after[1])

    def test_unavailable_mes_preserves_pending_order_and_retry(self):
        data = self.instruction(self.create_order())
        with patch("erp.dispatch.build_opener", side_effect=URLError("test: unavailable")):
            self.assertEqual(self.send(data).status_code, 502)
        self.assertEqual(self.query("SELECT status FROM orders"), [("pending",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders"), [(0,)])
        self.assertEqual(self.send(data).status_code, 201)

    def test_invalid_protocol_or_equipment_has_no_mes_writes(self):
        data = ERPDispatchRequest.model_validate(self.instruction(self.create_order()))
        envelope = sender.prepare_message(self.path, data).model_dump(mode="json")
        bad = copy.deepcopy(envelope)
        bad["protocol"] = "wrong"
        self.assertEqual(self.client.post("/mes/dispatch", json=bad).status_code, 422)
        for equipment_id, expected in [("missing", 404), ("E-QC", 409)]:
            bad = copy.deepcopy(envelope)
            bad["payload"]["route_steps"][0]["equipment_id"] = equipment_id
            self.assertEqual(self.client.post("/mes/dispatch", json=bad).status_code, expected)
        self.assertEqual(self.query("SELECT COUNT(*) FROM work_orders"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM mes_dispatch_inbox"), [(0,)])
        self.assertEqual(self.query("SELECT status FROM orders"), [("pending",)])

    def test_audit_failure_rolls_back_work_order_operations_and_inbox(self):
        data = ERPDispatchRequest.model_validate(self.instruction(self.create_order()))
        envelope = sender.prepare_message(self.path, data)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""CREATE TRIGGER reject_protocol_event BEFORE INSERT ON events
                            WHEN NEW.action='erp_to_mes_protocol_dispatched'
                            BEGIN SELECT RAISE(ABORT, 'test audit failure'); END""")
            conn.commit()
            before = list(conn.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            service.dispatch(self.path, envelope)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_mes_views_use_received_snapshot(self):
        data = ERPDispatchRequest.model_validate(self.instruction(self.create_order()))
        envelope = sender.prepare_message(self.path, data)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE orders SET quantity=9,product_name='changed after snapshot',due_date=NULL")
        response = self.client.post("/mes/dispatch", json=envelope.model_dump(mode="json"))
        self.assertEqual(response.status_code, 201, response.text)
        for work_order in [response.json(), self.client.get("/mes/work-orders").json()[0]]:
            self.assertEqual(work_order["quantity"], 2)
            self.assertEqual(work_order["product_name"], "法兰")
            self.assertEqual(work_order["due_date"], "2026-12-01")

    def test_cancel_and_new_message_redispatch(self):
        data = self.instruction(self.create_order())
        first = self.send(data).json()
        cancel = self.client.post(f"/mes/work-orders/{first['work_order_id']}/cancel",
                                  json={"operator": "test", "note": "test cancel"})
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(self.send(data).json()["work_order_id"], first["work_order_id"])
        fresh = self.send({**data, "message_id": str(uuid4())})
        self.assertEqual(fresh.status_code, 201, fresh.text)
        self.assertNotEqual(fresh.json()["work_order_id"], first["work_order_id"])


if __name__ == "__main__":
    unittest.main()
