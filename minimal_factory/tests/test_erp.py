"""用独立临时数据库验证业务，不写入你的练习订单。"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from erp.app import create_app


class ERPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "orders.db"
        self.payload = {
            "product_id": "P001",
            "product_name": "简易装配件'样品",
            "quantity": 2,
            "due_date": "2026-10-23",
        }

    def test_create_query_and_restart_preserve_order(self):
        with TestClient(create_app(self.db)) as client:
            self.assertEqual(client.get("/orders").json(), [])
            response = client.post("/orders", json=self.payload)
            self.assertEqual(response.status_code, 201)
            order = response.json()
            self.assertEqual(order["status"], "pending")
            for key, value in self.payload.items():
                self.assertEqual(order[key], value)
            self.assertTrue(order["order_id"].startswith("ORD-"))
            self.assertEqual(client.get(f"/orders/{order['order_id']}").json(), order)
            self.assertEqual(client.get("/orders").json(), [order])

        # 关闭旧应用，再创建一个新应用：数据应从磁盘读出。
        with TestClient(create_app(self.db)) as restarted:
            self.assertEqual(restarted.get("/orders").json(), [order])
            second = restarted.post("/orders", json=self.payload)
            self.assertEqual(second.status_code, 201)
            self.assertNotEqual(second.json()["order_id"], order["order_id"])

    def test_invalid_inputs_never_create_orders(self):
        with TestClient(create_app(self.db)) as client:
            for quantity in [0, -1, 1.5, True, "2"]:
                with self.subTest(quantity=quantity):
                    response = client.post("/orders", json={**self.payload, "quantity": quantity})
                    self.assertEqual(response.status_code, 422)
            for change in [
                {"product_id": "   "},
                {"product_name": ""},
                {"due_date": "2026-02-30"},
                {"status": "completed"},
            ]:
                with self.subTest(change=change):
                    response = client.post("/orders", json={**self.payload, **change})
                    self.assertEqual(response.status_code, 422)
            self.assertEqual(client.get("/orders").json(), [])

    def test_missing_order_and_optional_due_date(self):
        with TestClient(create_app(self.db)) as client:
            self.assertEqual(client.get("/orders/not-found").status_code, 404)
            payload = {key: value for key, value in self.payload.items() if key != "due_date"}
            response = client.post("/orders", json=payload)
            self.assertEqual(response.status_code, 201)
            self.assertIsNone(response.json()["due_date"])

    def test_delete_only_selected_order_and_restart_preserves_deletion(self):
        with TestClient(create_app(self.db)) as client:
            wrong_order = client.post("/orders", json=self.payload).json()
            kept_order = client.post("/orders", json={**self.payload, "quantity": 3}).json()
            deleted_path = f"/orders/{wrong_order['order_id']}"

            response = client.delete(deleted_path)
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")
            self.assertEqual(client.get(deleted_path).status_code, 404)
            self.assertEqual(client.get("/orders").json(), [kept_order])

        # 重启后删除仍生效，其他订单的内容保留；重复删除返回 404。
        with TestClient(create_app(self.db)) as restarted:
            self.assertEqual(restarted.get(deleted_path).status_code, 404)
            self.assertEqual(restarted.get(f"/orders/{kept_order['order_id']}").json(), kept_order)
            self.assertEqual(restarted.delete(deleted_path).status_code, 404)
            self.assertEqual(restarted.get("/orders").json(), [kept_order])

    def test_delete_unknown_or_sql_like_id_never_removes_orders(self):
        with TestClient(create_app(self.db)) as client:
            order = client.post("/orders", json=self.payload).json()
            for order_id in ["not-found", "' OR 1=1 --"]:
                with self.subTest(order_id=order_id):
                    response = client.delete(f"/orders/{order_id}")
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json(), {"detail": "订单不存在"})
                    self.assertEqual(client.get("/orders").json(), [order])


if __name__ == "__main__":
    unittest.main()
