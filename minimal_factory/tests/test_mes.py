"""使用临时数据库验证完整生产流程和失败回滚，不写入日常练习数据。"""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from erp.app import create_app
from mes import service
from mes.schemas import Dispatch, Note, Report, ReworkAction


class MESTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "factory.db"
        self.client = self.enterContext(TestClient(create_app(self.path)))

    def request(self, method, url, data=None, expected=200):
        response = self.client.request(method, url, json=data)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.content else None

    def order(self, quantity=2):
        return self.request("POST", "/orders", {"product_id": "P001", "product_name": "教学产品'样品", "quantity": quantity}, 201)

    def dispatch(self, order=None):
        return self.request("POST", "/mes/work-orders", {"order_id": (order or self.order())["order_id"]}, 201)

    def get(self, wo):
        return self.request("GET", f"/mes/work-orders/{wo['work_order_id']}")

    def prepare(self, wo=None):
        wo = wo or self.dispatch()
        ids = {"assembly": "E-ASM", "inspection": "E-QC", "transport": "E-MOVE"}
        for op in wo["operations"]:
            self.request("PUT", f"/mes/operations/{op['operation_id']}/equipment", {"equipment_id": ids[op["kind"]]})
        return self.request("POST", f"/mes/work-orders/{wo['work_order_id']}/release")

    def action(self, op, action, expected=200):
        return self.request("POST", f"/mes/operations/{op['operation_id']}/{action}", {"operator": "测试员", "note": action}, expected)

    def report(self, op, good=2, rework=0, expected=200, **extra):
        return self.request("POST", f"/mes/operations/{op['operation_id']}/reports", {
            "request_id": uuid4().hex, "operator": "测试员", "good_quantity": good,
            "rework_quantity": rework, "note": "测试加工结果", **extra,
        }, expected)

    def finish_operations(self, wo):
        for op in wo["operations"]:
            self.action(op, "start")
            wo = self.report(op, wo["quantity"])
        self.assertEqual(wo["status"], "awaiting_quality")
        return wo

    def inspection(self, wo, rework=0, expected=200, **extra):
        return self.request("POST", f"/mes/work-orders/{wo['work_order_id']}/inspections", {
            "request_id": uuid4().hex, "operator": "质检员", "rework_quantity": rework, "note": "检验说明", **extra,
        }, expected)

    def test_complete_flow_feedback_export_and_restart(self):
        wo = self.finish_operations(self.prepare())
        order_url = f"/orders/{wo['order_id']}"
        self.assertEqual(self.request("GET", order_url)["status"], "in_progress")
        completed = self.inspection(wo)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.request("GET", order_url)["status"], "completed")
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(len(completed["reports"]), 3)
        self.assertEqual(completed["progress"], 100)
        self.assertTrue(all(not e["occupied_by"] for e in self.request("GET", "/mes/equipment")))
        exported = self.request("GET", f"/mes/work-orders/{wo['work_order_id']}/export")
        self.assertEqual(exported["work_order"], completed)
        self.assertTrue(all(event["order_id"] == wo["order_id"] for event in completed["events"]))
        with TestClient(create_app(self.path)) as restarted:
            self.assertEqual(restarted.get(f"/mes/work-orders/{wo['work_order_id']}").json(), completed)
            self.assertEqual(restarted.get("/mes/dashboard").json()["completed_quantity"], 2)
            self.assertEqual(restarted.get("/").status_code, 200)

    def test_missing_duplicate_dispatch_and_cancel_redispatch(self):
        self.request("POST", "/mes/work-orders", {"order_id": "missing"}, 404)
        order = self.order()
        wo = self.dispatch(order)
        self.request("POST", "/mes/work-orders", {"order_id": order["order_id"]}, 409)
        cancelled = self.request("POST", f"/mes/work-orders/{wo['work_order_id']}/cancel", {"operator": "计划员", "note": "计划调整"})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["order_status"], "pending")
        self.assertNotEqual(self.dispatch(order)["work_order_id"], wo["work_order_id"])
        self.request("DELETE", f"/orders/{order['order_id']}", expected=409)

    def test_preconditions_wrong_equipment_sequence_and_pause(self):
        wo = self.dispatch()
        first, second = wo["operations"][:2]
        self.action(first, "start", 409)
        self.request("POST", f"/mes/work-orders/{wo['work_order_id']}/release", expected=409)
        self.request("PUT", f"/mes/operations/{first['operation_id']}/equipment", {"equipment_id": "E-QC"}, 409)
        wo = self.prepare(wo)
        self.action(second, "start", 409)
        self.report(first, expected=409)
        self.action(first, "start")
        self.action(first, "start", 409)
        self.action(first, "pause")
        self.report(first, expected=409)
        self.request("PATCH", "/mes/equipment/E-ASM", {"status": "maintenance", "reason": "保养"}, 409)
        self.request("POST", f"/mes/work-orders/{wo['work_order_id']}/cancel", {"operator": "计划员", "note": "已开工不能撤回"}, 409)
        self.action(first, "resume")
        result = self.report(first)
        self.assertEqual(result["operations"][0]["status"], "completed")
        self.action(first, "resume", 409)

    def test_overreport_validation_rework_and_retry_do_not_inflate_quantity(self):
        wo = self.prepare()
        op = wo["operations"][0]
        self.action(op, "start")
        before = self.get(wo)
        self.report(op, good=3, expected=409)
        self.report(op, good=0, expected=422)
        for bad in [-1, True, 1.5, "2"]:
            self.report(op, good=bad, expected=422)
        self.report(op, good=1, rework=1, note="", expected=422)
        self.assertEqual(self.get(wo), before)
        request_id = uuid4().hex
        result = self.report(op, good=1, rework=1, request_id=request_id)
        self.report(op, good=1, rework=1, request_id=request_id)
        self.assertEqual(result, self.get(wo))
        self.report(op, good=2, request_id=request_id, expected=409)
        result = self.report(op, good=1)
        self.assertEqual(result["operations"][0]["good_quantity"], 2)
        self.assertEqual(result["operations"][0]["rework_attempts"], 1)
        self.assertEqual(len(result["reports"]), 2)

    def test_final_quality_rework_reinspection_and_idempotency(self):
        wo = self.prepare()
        self.inspection(wo, expected=409)
        wo = self.finish_operations(wo)
        before = self.get(wo)
        self.inspection(wo, rework=3, expected=409)
        self.assertEqual(self.get(wo), before)
        request_id = uuid4().hex
        target = wo["operations"][0]["operation_id"]
        wo = self.inspection(wo, rework=1, request_id=request_id, rework_operation_id=target)
        self.inspection(wo, rework=1, request_id=request_id, rework_operation_id=target)
        self.assertEqual(len(self.get(wo)["inspections"]), 1)
        self.assertEqual(wo["order_status"], "in_progress")
        last = wo["operations"][-1]
        self.assertEqual(last["good_quantity"], 2)
        self.assertEqual(last["status"], "completed")
        self.inspection(wo, expected=409)
        task = wo["reworks"][0]
        self.rework(task, "start", equipment_id="E-ASM")
        self.rework(task, "finish")
        self.rework(task, "inspect-start", equipment_id="E-QC")
        self.rework(task, "inspect-finish", passed=True)
        approved = self.inspection(wo)
        self.assertEqual(approved["status"], "completed")
        self.assertEqual(len(approved["inspections"]), 2)
        self.assertEqual(self.request("GET", "/mes/dashboard")["completed_quantity"], 2)
        self.action(last, "start", 409)
        self.inspection(wo, expected=409)

    def rework(self, task, action, expected=200, **extra):
        return self.request("POST", f"/mes/reworks/{task['rework_id']}/{action}", {
            "request_id": uuid4().hex, "operator": "返修员", "note": "返修测试", **extra,
        }, expected)

    def failed_process_inspection(self, quantity=2, good=1, rejected=1):
        wo = self.prepare(self.dispatch(self.order(quantity)))
        assembly, qc = wo["operations"][:2]
        self.action(assembly, "start")
        self.report(assembly, good=quantity)
        self.action(qc, "start")
        return self.report(qc, good=good, rework=rejected, rework_operation_id=assembly["operation_id"])

    def test_process_rework_resources_reinspection_and_trace(self):
        wo = self.failed_process_inspection()
        assembly, qc, move = wo["operations"]
        task = wo["reworks"][0]
        self.assertEqual(qc["good_quantity"], 1)
        self.assertTrue(all(not e["occupied_by"] for e in self.request("GET", "/mes/equipment")))
        self.action(qc, "start", 409)
        self.action(move, "start", 409)
        self.report(qc, good=1, expected=409)
        self.rework(task, "inspect-start", equipment_id="E-QC", expected=409)
        self.rework(task, "start", equipment_id="E-QC", expected=409)
        request_id = uuid4().hex
        self.rework(task, "start", equipment_id="E-ASM", request_id=request_id)
        self.rework(task, "start", equipment_id="E-ASM", request_id=request_id)
        self.assertEqual(self.request("GET", "/mes/dashboard")["busy_equipment"], 1)
        self.request("PATCH", "/mes/equipment/E-ASM", {"status": "maintenance", "reason": "保养"}, 409)
        devices = {e["equipment_id"]: e for e in self.request("GET", "/mes/equipment")}
        self.assertEqual(devices["E-ASM"]["occupied_by"], task["rework_id"])
        self.assertIsNone(devices["E-QC"]["occupied_by"])
        self.rework(task, "finish")
        self.rework(task, "inspect-start", equipment_id="E-QC")
        self.request("PATCH", "/mes/equipment/E-QC", {"status": "maintenance", "reason": "保养"}, 409)
        self.rework(task, "inspect-finish", passed=False, note="", expected=409)
        failed = self.rework(task, "inspect-finish", passed=False)
        self.assertEqual(failed["reworks"][0]["cycle"], 2)
        self.assertEqual(failed["operations"][1]["good_quantity"], 1)
        self.rework(task, "start", equipment_id="E-ASM")
        self.rework(task, "finish")
        self.rework(task, "inspect-start", equipment_id="E-QC")
        request_id = uuid4().hex
        result = self.rework(task, "inspect-finish", passed=True, request_id=request_id)
        self.rework(task, "inspect-finish", passed=True, request_id=request_id)
        self.rework(task, "inspect-finish", passed=False, request_id=request_id, expected=409)
        self.rework(task, "inspect-finish", passed=True, expected=409)
        self.assertEqual(result["operations"][1]["good_quantity"], 2)
        self.assertEqual(result["operations"][1]["status"], "completed")
        self.assertEqual(len(result["rework_actions"]), 8)
        self.assertEqual(len(result["reports"]), 2)
        self.assertEqual(result["operations"][0]["good_quantity"], 2)
        self.action(move, "start")
        wo = self.report(move)
        self.inspection(wo)
        exported = self.request("GET", f"/mes/work-orders/{wo['work_order_id']}/export")
        self.assertEqual(exported["work_order"]["reworks"][0]["status"], "completed")

    def test_partial_inspection_repair_does_not_finish_uninspected_units(self):
        wo = self.failed_process_inspection(quantity=3)
        task = wo["reworks"][0]
        self.rework(task, "start", equipment_id="E-ASM")
        self.rework(task, "finish")
        self.rework(task, "inspect-start", equipment_id="E-QC")
        wo = self.rework(task, "inspect-finish", passed=True)
        qc = wo["operations"][1]
        self.assertEqual(qc["good_quantity"], 2)
        self.assertEqual(qc["status"], "pending")
        self.action(wo["operations"][2], "start", 409)
        self.action(qc, "start")
        wo = self.report(qc, good=1)
        self.assertEqual(wo["operations"][1]["status"], "completed")

    def test_rework_target_validation_rolls_back_report(self):
        wo = self.prepare()
        assembly, qc, move = wo["operations"]
        foreign = self.prepare()["operations"][0]
        self.action(assembly, "start")
        self.report(assembly)
        self.action(qc, "start")
        before = self.get(wo)
        for target, status in [(None, 409), ("missing", 404), (foreign["operation_id"], 409),
                               (qc["operation_id"], 409), (move["operation_id"], 409)]:
            self.report(qc, good=1, rework=1, rework_operation_id=target, expected=status)
            self.assertEqual(self.get(wo), before)

    def test_rework_and_normal_work_compete_for_equipment(self):
        wo = self.failed_process_inspection()
        task = wo["reworks"][0]
        other = self.prepare()
        op = other["operations"][0]
        self.action(op, "start")
        self.rework(task, "start", equipment_id="E-ASM", expected=409)
        self.report(op)
        self.rework(task, "start", equipment_id="E-ASM")
        third = self.prepare()
        self.action(third["operations"][0], "start", 409)
        self.rework(task, "finish")
        self.action(third["operations"][0], "start")
        self.action(other["operations"][1], "start")
        self.rework(task, "inspect-start", equipment_id="E-QC", expected=409)
        self.report(other["operations"][1])
        self.rework(task, "inspect-start", equipment_id="E-QC")
        # Restart preserves the active reservation and does not credit production twice.
        with TestClient(create_app(self.path)) as restarted:
            devices = restarted.get("/mes/equipment").json()
            self.assertEqual(next(e for e in devices if e["equipment_id"] == "E-QC")["occupied_by"], task["rework_id"])

    def test_concurrent_repair_and_normal_start_have_only_one_winner(self):
        wo = self.failed_process_inspection()
        task = wo["reworks"][0]
        other = self.prepare()

        def start(repair):
            try:
                if repair:
                    service.execute_rework(self.path, task["rework_id"], "start", ReworkAction(
                        request_id=uuid4().hex, operator="并发测试", equipment_id="E-ASM"))
                else:
                    service.execute_operation(self.path, other["operations"][0]["operation_id"],
                                              "start", Note(operator="并发测试", note="开工"))
                return 200
            except service.BusinessError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(start, [True, False])), [200, 409])
        self.assertEqual(self.request("GET", "/mes/dashboard")["busy_equipment"], 1)

    def test_existing_mes_reports_survive_additive_migration(self):
        wo = self.inspection(self.finish_operations(self.prepare()))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TABLE rework_actions")
            conn.execute("DROP TABLE reworks")
            conn.execute("ALTER TABLE production_reports DROP COLUMN rework_operation_id")
            conn.execute("ALTER TABLE inspections DROP COLUMN rework_operation_id")
        service.initialize(self.path)
        service.initialize(self.path)
        self.assertEqual(self.get(wo), wo)

    def test_equipment_maintenance_and_plan(self):
        wo = self.dispatch()
        self.request("PATCH", "/mes/equipment/E-ASM", {"status": "maintenance", "reason": "保养"})
        self.request("PUT", f"/mes/operations/{wo['operations'][0]['operation_id']}/equipment", {"equipment_id": "E-ASM"}, 409)
        self.request("PATCH", "/mes/equipment/E-ASM", {"status": "available", "reason": "保养结束"})
        updated = self.request("PATCH", f"/mes/work-orders/{wo['work_order_id']}/plan", {"priority": 1, "planned_date": "2026-10-23"})
        self.assertEqual(updated["priority"], 1)
        self.assertEqual(updated["planned_date"], "2026-10-23")
        self.prepare(wo)
        self.action(wo["operations"][0], "start")
        self.request("PATCH", f"/mes/work-orders/{wo['work_order_id']}/plan", {"priority": 3}, 409)
        self.request("POST", "/mes/equipment", {"name": "新增组装台", "kind": "assembly"}, 201)
        self.request("POST", "/mes/equipment", {"name": "新增组装台", "kind": "assembly"}, 409)

    def test_delete_equipment_preserves_audit_and_survives_restart(self):
        eq = self.request("POST", "/mes/equipment", {"name": "Robot_02", "kind": "transport"}, 201)
        eq_id = eq["equipment_id"]
        self.request("PATCH", f"/mes/equipment/{eq_id}", {"status": "maintenance", "reason": "演示维护"})
        result = self.request("DELETE", f"/mes/equipment/{eq_id}")
        self.assertEqual(result, {"equipment_id": eq_id, "name": "Robot_02", "deleted": True})
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM equipment").fetchone()[0], 3)
            events = conn.execute("SELECT action,detail,equipment_id FROM events ORDER BY event_id").fetchall()
            self.assertEqual([e[0] for e in events], ["equipment_created", "equipment_state", "equipment_deleted"])
            self.assertTrue(all(eq_id in e[1] and "Robot_02" in e[1] and e[2] is None for e in events))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.request("DELETE", f"/mes/equipment/{eq_id}", expected=404)
        service.initialize(self.path)
        self.assertEqual(len(self.request("GET", "/mes/equipment")), 3)

    def test_delete_default_or_assigned_equipment_is_rejected_without_changes(self):
        for eq_id in ("E-ASM", "E-QC", "E-MOVE"):
            self.request("DELETE", f"/mes/equipment/{eq_id}", expected=409)
        eq = self.request("POST", "/mes/equipment", {"name": "额外组装台", "kind": "assembly"}, 201)
        wo = self.dispatch()
        self.request("PUT", f"/mes/operations/{wo['operations'][0]['operation_id']}/equipment",
                     {"equipment_id": eq["equipment_id"]})
        with closing(sqlite3.connect(self.path)) as conn:
            before = list(conn.iterdump())
        self.request("DELETE", f"/mes/equipment/{eq['equipment_id']}", expected=409)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_delete_equipment_rolls_back_if_audit_write_fails(self):
        eq = self.request("POST", "/mes/equipment", {"name": "Robot_02", "kind": "transport"}, 201)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""CREATE TRIGGER reject_delete_event BEFORE INSERT ON events
                            WHEN NEW.action='equipment_deleted'
                            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
            conn.commit()
            before = list(conn.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            service.delete_equipment(self.path, eq["equipment_id"])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_competing_work_orders_cannot_occupy_same_machine(self):
        first, second = self.prepare(), self.prepare()
        note = Note(operator="并发测试", note="开工")

        def start(wo):
            try:
                service.execute_operation(self.path, wo["operations"][0]["operation_id"], "start", note)
                return 200
            except service.BusinessError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(start, [first, second])), [200, 409])
        devices = self.request("GET", "/mes/equipment")
        self.assertEqual(sum(bool(eq["occupied_by"]) for eq in devices), 1)
        winner = first if self.get(first)["status"] == "in_progress" else second
        loser = second if winner == first else first
        self.action(winner["operations"][0], "pause")
        self.action(loser["operations"][0], "start", 409)
        self.action(winner["operations"][0], "resume")
        self.report(winner["operations"][0])
        self.action(loser["operations"][0], "start")

    def test_concurrent_duplicate_dispatch_and_report(self):
        order = self.order()
        data = Dispatch(order_id=order["order_id"])

        def dispatch(_):
            try:
                return service.dispatch(self.path, data)["work_order_id"]
            except service.BusinessError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        wo = self.prepare(self.request("GET", f"/mes/work-orders/{next(x for x in results if x)}"))
        op = wo["operations"][0]
        self.action(op, "start")
        report = Report(request_id=uuid4().hex, operator="并发测试", good_quantity=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: service.report(self.path, op["operation_id"], report), range(2)))
        result = self.get(wo)
        self.assertEqual(result["operations"][0]["good_quantity"], 1)
        self.assertEqual(len(result["reports"]), 1)

    def test_route_validation_and_unknown_resources(self):
        order = self.order()
        for change in [{"steps": []}, {"steps": [{"name": "", "kind": "assembly", "minutes": 1}]},
                       {"steps": [{"name": "组装", "kind": "unknown", "minutes": 1}]}, {"priority": True}]:
            self.request("POST", "/mes/work-orders", {"order_id": order["order_id"], **change}, 422)
        self.assertEqual(self.request("GET", "/mes/work-orders"), [])
        self.assertEqual(self.request("GET", f"/orders/{order['order_id']}")["status"], "pending")
        for url in ["/mes/work-orders/missing", "/mes/work-orders/missing/export"]:
            self.request("GET", url, expected=404)
        self.request("POST", "/mes/operations/missing/start", {"operator": "测试员", "note": "开始"}, 404)


class MigrationTests(unittest.TestCase):
    def test_legacy_order_is_backed_up_and_survives_idempotent_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("""CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0), due_date TEXT,
                    status TEXT NOT NULL CHECK(status = 'pending'), created_at TEXT NOT NULL
                )""")
                conn.execute("INSERT INTO orders VALUES ('OLD','P001','原有订单',2,NULL,'pending','2026-10-16T00:00:00+00:00')")
            with TestClient(create_app(path)) as client:
                self.assertEqual(client.get("/orders/OLD").json()["product_name"], "原有订单")
                self.assertEqual(client.post("/mes/work-orders", json={"order_id": "OLD"}).status_code, 201)
            backup = path.with_name("legacy.before-mes.db")
            self.assertTrue(backup.exists())
            with closing(sqlite3.connect(backup)) as conn:
                self.assertEqual(conn.execute("SELECT status FROM orders").fetchone()[0], "pending")
            with TestClient(create_app(path)) as client:
                self.assertEqual(client.get("/orders/OLD").json()["status"], "dispatched")
                self.assertEqual(len(client.get("/mes/work-orders").json()), 1)


if __name__ == "__main__":
    unittest.main()
