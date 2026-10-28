"""独立实验的数值、隔离、恢复、接口与导出测试。"""
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from erp.app import create_app
from mes import service as mes
from simulation import engine
from simulation.inputs import demo_snapshot
from simulation.schemas import RunRequest
from simulation.service import SimulationService, write_json


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "erp.db"
        self.app = create_app(self.path)
        self.client = self.enterContext(TestClient(self.app))
        self.service = self.app.state.simulation

    def run_case(self, data=None):
        response = self.client.post("/simulation/runs", json=data or {"source": "demo"})
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(response.json()["run_id"])

    def wait(self, run_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = self.client.get(f"/simulation/runs/{run_id}")
            self.assertEqual(response.status_code, 200, response.text)
            record = response.json()
            if record["status"] != "running":
                return record
            time.sleep(.01)
        self.fail("Simulation did not finish in 10 seconds")

    def assert_success(self, record, times):
        self.assertEqual(record["status"], "completed", record.get("error"))
        self.assertEqual([w["completed_min"] for w in record["summary"]["orders"]], times)
        self.assertTrue(all(w["mes_status"] == w["erp_status"] == "completed" for w in record["summary"]["orders"]))
        folder = self.service.folder(record["run_id"])
        self.assertEqual(record["summary"]["completed_quantity"], sum(w["quantity"] for w in record["input"]["orders"]))
        with mes.connect(folder / "mes.db") as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        for eq in record["summary"]["equipment"]:
            intervals = sorted((i for i in record["summary"]["intervals"] if i["equipment_id"] == eq["equipment_id"]),
                               key=lambda i: i["start_min"])
            self.assertTrue(all(a["end_min"] <= b["start_min"] for a, b in zip(intervals, intervals[1:])))
            self.assertLessEqual(eq["utilization"], 100)
        events = record["events"]
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events)+1)))
        self.assertEqual([e["sim_time_min"] for e in events], sorted(e["sim_time_min"] for e in events))
        self.assertTrue(all(e["recorded_at"] and e["run_id"] == record["run_id"] for e in events))

    def failure_config(self):
        return {"inspection_operation_id": "DEMO-1-OP-2", "target_operation_id": "DEMO-1-OP-1", "quantity": 1}

    def test_baseline_resources_feedback_and_source_isolation(self):
        before = self.path.read_bytes()
        record = self.run_case()
        self.assert_success(record, [6, 9])
        self.assertEqual([w["waiting_min"] for w in record["summary"]["orders"]], [0, 3])
        self.assertEqual(record["summary"]["makespan_min"], 9)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.get("/orders").json(), [])
        self.assertTrue(all(not e["occupied_by"] for e in mes.list_equipment(self.service.folder(record["run_id"]) / "mes.db")))

    def test_duration_and_batch_quantity(self):
        record = self.run_case({"overrides": {
            f"DEMO-WO-{n}": {"minutes": {f"DEMO-{n}-OP-1": 5}} for n in (1, 2)}})
        self.assert_success(record, [8, 13])
        record = self.run_case({"overrides": {f"DEMO-WO-{n}": {"quantity": 2} for n in (1, 2)}})
        self.assert_success(record, [12, 18])

    def test_rework_competes_and_reinspection_does_not_inflate_output(self):
        record = self.run_case({"overrides": {"DEMO-WO-1": {"failure": self.failure_config()}}})
        self.assert_success(record, [12, 9])
        self.assertEqual(record["summary"]["completed_quantity"], 2)
        self.assertEqual(record["summary"]["rework_quantity"], 1)
        repair = next(i for i in record["summary"]["intervals"] if i["phase"] == "repair")
        self.assertEqual((repair["start_min"], repair["end_min"], repair["equipment_id"]), (6, 9, "E-ASM"))
        reinspection = next(i for i in record["summary"]["intervals"] if i["phase"] == "reinspection")
        self.assertEqual((reinspection["start_min"], reinspection["end_min"], reinspection["equipment_id"]), (9, 11, "E-QC"))
        wo = mes.get_work_order(self.service.folder(record["run_id"]) / "mes.db", "DEMO-WO-1")
        self.assertEqual(wo["reworks"][0]["status"], "completed")
        self.assertEqual(len(wo["rework_actions"]), 4)
        self.assertTrue(all(op["good_quantity"] == 1 for op in wo["operations"]))

    def test_replay_is_deterministic_and_keeps_previous_run(self):
        first = self.run_case({"overrides": {"DEMO-WO-1": {"failure": self.failure_config()}}})
        original_bytes = (self.service.folder(first["run_id"]) / "input.json").read_bytes()
        second = self.run_case({"source": "replay", "replay_run_id": first["run_id"]})
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assert_success(second, [12, 9])
        keys = ("sequence", "sim_time_min", "event_type", "order_id", "work_order_id",
                "operation_id", "equipment_id", "quantity", "phase", "wait_min",
                "accepted_quantity", "rejected_quantity")
        self.assertEqual([{k: e.get(k) for k in keys} for e in first["events"]],
                         [{k: e.get(k) for k in keys} for e in second["events"]])
        self.assertEqual(original_bytes, (self.service.folder(first["run_id"]) / "input.json").read_bytes())
        third = self.run_case({"source": "replay", "replay_run_id": first["run_id"],
                               "overrides": {"DEMO-WO-1": {"failure": None}}})
        self.assert_success(third, [6, 9])

    def test_completed_source_copy_and_replay_survive_source_changes(self):
        order = self.client.post("/orders", json={"product_id": "S", "product_name": "=SUM(1,2)", "quantity": 1}).json()
        wo = self.client.post("/mes/work-orders", json={"order_id": order["order_id"]}).json()
        wo_id = wo["work_order_id"]
        payload = {"source": "work_orders", "work_order_ids": [wo_id]}
        self.assertEqual(self.client.post("/simulation/runs", json=payload).status_code, 409)
        for op, eq in zip(wo["operations"], ("E-ASM", "E-QC", "E-MOVE")):
            self.client.put(f"/mes/operations/{op['operation_id']}/equipment", json={"equipment_id": eq})
        self.client.post(f"/mes/work-orders/{wo_id}/release")
        for n, op in enumerate(wo["operations"]):
            self.client.post(f"/mes/operations/{op['operation_id']}/start", json={"operator": "测试", "note": "开工"})
            self.client.post(f"/mes/operations/{op['operation_id']}/reports",
                             json={"request_id": f"source-report-{n}", "operator": "测试", "good_quantity": 1})
        self.client.post(f"/mes/work-orders/{wo_id}/inspections",
                         json={"request_id": "source-quality", "operator": "测试", "rework_quantity": 0})
        self.client.patch("/mes/equipment/E-ASM", json={"status": "maintenance", "reason": "测试"})
        before = self.path.read_bytes()
        record = self.run_case(payload)
        self.assert_success(record, [6])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(record["mapping"][0]["source_work_order_id"], wo_id)
        result = mes.get_work_order(self.service.folder(record["run_id"]) / "mes.db", wo_id)
        self.assertEqual(len(result["reports"]), 3)  # 历史没有带入或累加。
        with mes.transaction(self.path) as conn:
            conn.execute("UPDATE operations SET minutes=99 WHERE work_order_id=?", (wo_id,))
        replay = self.run_case({"source": "replay", "replay_run_id": record["run_id"]})
        self.assert_success(replay, [6])
        exported = self.client.get(f"/simulation/runs/{record['run_id']}/export?format=summary_csv")
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["product_name"], "'=SUM(1,2)")

    def test_invalid_input_is_rejected_before_run_creation(self):
        bad = [
            {"source": "work_orders", "work_order_ids": []},
            {"source": "work_orders", "work_order_ids": ["x"]*51},
            {"overrides": {"missing": {"quantity": 1}}},
            {"overrides": {"DEMO-WO-1": {"quantity": True}}},
            {"overrides": {"DEMO-WO-1": {"minutes": {"DEMO-1-OP-1": 0}}}},
            {"overrides": {"DEMO-WO-1": {"minutes": {"missing": 1}}}},
            {"overrides": {"DEMO-WO-1": {"failure": {**self.failure_config(), "quantity": 2}}}},
            {"overrides": {"DEMO-WO-1": {"failure": {**self.failure_config(), "target_operation_id": "DEMO-1-OP-3"}}}},
            {"overrides": {"DEMO-WO-1": {"failure": {**self.failure_config(), "inspection_operation_id": "DEMO-1-OP-1"}}}},
        ]
        for payload in bad:
            response = self.client.post("/simulation/runs", json=payload)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/simulation/runs").json(), [])
        self.assertEqual(self.client.get("/simulation/runs/not-a-run").status_code, 404)
        self.assertEqual(self.client.post("/simulation/preview", json={"source": "replay", "replay_run_id": "../erp.db"}).status_code, 404)

    def test_exports_match_summary_and_csv_has_bom(self):
        record = self.run_case()
        url = f"/simulation/runs/{record['run_id']}/export"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        exported = response.json()
        self.assertEqual(exported["summary"], record["summary"])
        self.assertEqual(exported["events"], record["events"])
        self.assertEqual(len(exported["mes_work_orders"]), 2)
        response = self.client.get(url + "?format=events_csv")
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), len(record["events"]))
        self.assertEqual(float(rows[-1]["sim_time_min"]), 9)
        self.assertEqual(self.client.get(url + "?format=bad").status_code, 422)

    def test_one_active_run_including_concurrent_service_instances(self):
        entered, release = threading.Event(), threading.Event()
        original = engine.simulate

        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        with patch("simulation.service.simulate", side_effect=blocked):
            first = self.client.post("/simulation/runs", json={})
            self.assertEqual(first.status_code, 202)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.client.post("/simulation/runs", json={}).status_code, 409)
                other = SimulationService(self.path)
                other.startup()
                try:
                    self.assertEqual(other.get(first.json()["run_id"])["status"], "running")
                    with self.assertRaises(mes.BusinessError):
                        other.start(RunRequest())
                finally:
                    other.shutdown()
            finally:
                release.set()
            self.assert_success(self.wait(first.json()["run_id"]), [6, 9])

    def test_failure_and_restart_interruption_are_not_success(self):
        with patch("simulation.service.simulate", side_effect=RuntimeError("测试异常")):
            record = self.run_case()
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["summary"])
        self.assertIn("测试异常", record["error"])
        self.assertEqual(self.client.get(f"/simulation/runs/{record['run_id']}/export?format=summary_csv").status_code, 409)
        folder = self.service.folder(record["run_id"])
        saved = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        saved.update(status="running", finished_at=None)
        write_json(folder / "run.json", saved)
        restarted = SimulationService(self.path)
        restarted.startup()
        try:
            self.assertEqual(restarted.get(record["run_id"])["status"], "interrupted")
            self.assertTrue((folder / "input.json").is_file())
        finally:
            restarted.shutdown()
        self.assert_success(self.run_case({"source": "replay", "replay_run_id": record["run_id"]}), [6, 9])

    def test_event_file_keeps_partial_results_on_interrupt(self):
        folder = Path(self.temp.name) / "interrupted-engine"
        folder.mkdir()
        stop = threading.Event()

        def progress(*_):
            stop.set()

        with self.assertRaises(engine.Interrupted):
            engine.simulate(folder, "SIM-" + "a"*32, demo_snapshot(), stop, progress)
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(events)
        self.assertFalse(any(e["event_type"] == "order_completed" for e in events))


if __name__ == "__main__":
    unittest.main()
