"""P0.1：原业务时间、旧快照未知值与源库硬只读的行为验证。"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from erp.app import create_app
from mes import service as mes
from simulation.field_runtime import initialize_sandbox
from simulation.inputs import configure, demo_snapshot, readonly_source, source_snapshot
from simulation.schemas import RunRequest
from simulation.service import write_json


class BusinessTimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="factory-p01-")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "source.db"
        self.app = create_app(self.path)
        self.client = self.enterContext(TestClient(self.app))
        self.service = self.app.state.simulation

    def timed_work_order(self):
        """仅在临时库设置两个不同日期，避免依赖机器时钟和执行速度。"""
        order = self.client.post("/orders", json={
            "product_id": "TIME", "product_name": "时间来源测试件", "quantity": 2,
        }).json()
        wo = self.client.post("/mes/work-orders", json={"order_id": order["order_id"]}).json()
        for op, eq in zip(wo["operations"], ("E-ASM", "E-QC", "E-MOVE")):
            result = self.client.put(f"/mes/operations/{op['operation_id']}/equipment",
                                     json={"equipment_id": eq})
            self.assertEqual(result.status_code, 200, result.text)
        with mes.transaction(self.path) as conn:
            conn.execute("UPDATE orders SET created_at=? WHERE order_id=?",
                         ("2026-11-01T08:00:00+00:00", order["order_id"]))
            conn.execute("UPDATE work_orders SET created_at=? WHERE work_order_id=?",
                         ("2026-11-02T09:00:00+00:00", wo["work_order_id"]))
        return wo

    def run_case(self, request):
        started = self.service.start(RunRequest(**request))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = self.service.get(started["run_id"])
            if record["status"] != "running":
                self.assertEqual(record["status"], "completed", record.get("error"))
                return record
            time.sleep(.01)  # 仅等待后台任务返回，不用于SimPy生产计时。
        self.fail("仿真未在10秒内完成")

    def sandbox_times(self, record):
        with readonly_source(self.service.folder(record["run_id"]) / "mes.db") as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            return dict(conn.execute("""SELECT o.created_at AS order_created_at,
                w.created_at AS work_order_created_at FROM work_orders w
                JOIN orders o USING(order_id) WHERE w.work_order_id=?""",
                (record["input"]["orders"][0]["work_order_id"],)).fetchone())

    def test_original_times_persist_through_run_and_replay_without_source_lookup(self):
        wo = self.timed_work_order()
        before = self.path.read_bytes()
        first = self.run_case({"source": "work_orders", "work_order_ids": [wo["work_order_id"]]})
        self.assertEqual(self.path.read_bytes(), before)
        expected = {"order_created_at": "2026-11-01T08:00:00+00:00",
                    "work_order_created_at": "2026-11-02T09:00:00+00:00"}
        self.assertEqual(self.sandbox_times(first), expected)
        item = first["input"]["orders"][0]
        self.assertEqual({k: item[k] for k in expected}, expected)
        self.assertEqual(item["created_at"], expected["work_order_created_at"])
        self.assertEqual(first["summary"]["makespan_min"], 12)
        self.assertEqual(first["summary"]["completed_quantity"], 2)
        saved_input = (self.service.folder(first["run_id"]) / "input.json").read_bytes()
        # 修改临时源库后，重放仍必须使用原快照，而非今天的业务值。
        with mes.transaction(self.path) as conn:
            conn.execute("UPDATE orders SET created_at='2026-11-03T00:00:00+00:00'")
            conn.execute("UPDATE work_orders SET created_at='2026-11-04T00:00:00+00:00'")
        before_replay = self.path.read_bytes()
        with patch("simulation.inputs.readonly_source", side_effect=AssertionError("重放不应查询源库")):
            replay = self.run_case({"source": "replay", "replay_run_id": first["run_id"]})
        self.assertEqual(self.sandbox_times(replay), expected)
        self.assertEqual(replay["summary"]["makespan_min"], 12)
        self.assertEqual(self.path.read_bytes(), before_replay)
        self.assertEqual((self.service.folder(first["run_id"]) / "input.json").read_bytes(), saved_input)

    def test_legacy_replay_keeps_order_time_unknown_without_rewriting_old_input(self):
        wo = self.timed_work_order()
        legacy = source_snapshot(self.path, [wo["work_order_id"]])
        for item in legacy["orders"]:
            item.pop("order_created_at")
            item.pop("work_order_created_at")
        old_folder = self.service.root / ("SIM-" + "c" * 32)
        old_folder.mkdir()
        write_json(old_folder / "input.json", legacy)
        write_json(old_folder / "run.json", {
            "run_id": old_folder.name, "status": "interrupted",
            "created_at": "2026-11-03T00:00:00+00:00",
        })
        old_bytes = (old_folder / "input.json").read_bytes()
        source_bytes = self.path.read_bytes()
        # 不允许回主库猜原订单时间，即使主库现在仍有它。
        with patch("simulation.inputs.readonly_source", side_effect=AssertionError("旧快照不应补查源库")):
            record = self.run_case({"source": "replay", "replay_run_id": old_folder.name})
        item = record["input"]["orders"][0]
        self.assertIsNone(item["order_created_at"])
        self.assertEqual(item["work_order_created_at"], legacy["orders"][0]["created_at"])
        self.assertEqual(self.sandbox_times(record), {
            "order_created_at": None, "work_order_created_at": "2026-11-02T09:00:00+00:00"})
        self.assertEqual(record["summary"]["makespan_min"], 12)
        self.assertEqual((old_folder / "input.json").read_bytes(), old_bytes)
        self.assertEqual(self.path.read_bytes(), source_bytes)

    def test_readonly_source_blocks_writes_even_when_query_only_is_disabled(self):
        wo = self.timed_work_order()
        before = self.path.read_bytes()
        with readonly_source(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            for query_only in (True, False):
                conn.execute("PRAGMA query_only=" + ("ON" if query_only else "OFF"))
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("UPDATE orders SET created_at='wrong' WHERE order_id=?", (wo["order_id"],))
        self.assertEqual(self.path.read_bytes(), before)

    def test_source_snapshot_rejects_an_accidental_write_in_its_reader(self):
        wo = self.timed_work_order()
        before = self.path.read_bytes()

        def accidental_write(conn, work_order_id):
            conn.execute("UPDATE work_orders SET created_at='wrong' WHERE work_order_id=?", (work_order_id,))

        with patch("simulation.inputs.mes.detail", side_effect=accidental_write):
            with self.assertRaises(sqlite3.OperationalError):
                source_snapshot(self.path, [wo["work_order_id"]])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_source_path_does_not_create_a_database(self):
        missing = self.path.parent / "typo source #1.db"
        with self.assertRaises(sqlite3.OperationalError):
            source_snapshot(missing, ["WO-missing"])
        self.assertFalse(missing.exists())

    def test_demo_times_are_explicit_and_legacy_normalization_is_in_memory(self):
        demo = demo_snapshot()
        self.assertTrue(all(w["order_created_at"] == w["work_order_created_at"] == w["created_at"]
                            for w in demo["orders"]))
        legacy = json.loads(json.dumps(demo))
        for item in legacy["orders"]:
            item.pop("order_created_at")
            item.pop("work_order_created_at")
        unchanged = json.dumps(legacy, sort_keys=True)
        result = configure(legacy, {})
        self.assertEqual(json.dumps(legacy, sort_keys=True), unchanged)
        self.assertTrue(all(w["order_created_at"] is None for w in result["orders"]))

    def test_sandbox_initialization_refuses_populated_order_tables(self):
        self.timed_work_order()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "空的仿真沙盒"):
            with mes.transaction(self.path) as conn:
                initialize_sandbox(conn)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
