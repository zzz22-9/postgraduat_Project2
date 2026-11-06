"""Connector 的只读源头：原订单主数据、原报文和已记录的 HTTP 交互。"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def read_feed(path):
    # mode=ro + query_only：本接口不能创建库、修改状态或补造历史寻址证据。
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        orders = [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY created_at,order_id")]
        by_id = {row["order_id"]: row for row in orders}
        messages = []
        for row in conn.execute("SELECT * FROM erp_dispatch_outbox ORDER BY rowid"):
            envelope = json.loads(row["envelope_json"])
            http = json.loads(row["http_json"]) if "http_json" in row.keys() and row["http_json"] else None
            addressing = http or envelope.get("http") or {}
            messages.append({"message_id": row["message_id"], "order_id": row["order_id"],
                             "timestamp": envelope["timestamp"], "protocol": envelope["protocol"],
                             "endpoint": addressing.get("url"), "http": http,
                             "payload": envelope["payload"], "envelope": envelope,
                             "acknowledged_at": row["acknowledged_at"],
                             "delivery_state": "acknowledged" if row["acknowledged_at"] else "unconfirmed",
                             "order": by_id.get(row["order_id"])})
    return {"source": "ERP", "orders": orders, "dispatch_outbox": messages}
