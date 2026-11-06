"""四协议沙盒与数据驱动故障级联验收；所有数据库均在临时目录。"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from mes import service as mes
from simulation import engine, field_protocols
from simulation.inputs import configure, demo_snapshot
from simulation.schemas import FieldScenario, Override, RunRequest
from simulation.service import SimulationService


def fixture(mode="normal", devices=None):
    snapshot = demo_snapshot()
    snapshot["orders"] = snapshot["orders"][:1]
    wo = snapshot["orders"][0]
    wo["quantity"] = 2
    config = {"mode": mode}
    if devices is not None:
        config["anomaly_equipment_ids"] = devices
    return configure(snapshot, {wo["work_order_id"]: Override(field_protocols=FieldScenario(**config))})


class DatabaseObserver:
    """在故障/恢复事件发出当刻读取已提交的真实SQLite状态。"""
    def __init__(self, path):
        self.path, self.states = path, []

    def on_event(self, event):
        if event["event_type"] in ("device_anomaly_affected_work_order", "device_recovered"):
            with mes.connect(self.path) as conn:
                row = dict(conn.execute("SELECT * FROM equipment WHERE equipment_id=?",
                                        (event["equipment_id"],)).fetchone())
            self.states.append((event, row))

    def observe(self, env, context, completed, stop):
        yield completed


class FieldSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def simulate(self, snapshot, name="case"):
        folder = self.root / name
        folder.mkdir()
        observer = DatabaseObserver(folder / "mes.db")
        result = engine.simulate(folder, "SIM-" + "a"*32, snapshot,
                                 threading.Event(), lambda *_: None, observer)
        with mes.connect(folder / "mes.db") as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            rows = [dict(r) for r in conn.execute("SELECT * FROM protocol_messages ORDER BY rowid")]
            db_messages = [json.loads(row["raw_json"]) for row in rows]
            protocol_events = [json.loads(r[0]) for r in conn.execute(
                "SELECT detail FROM events WHERE action IN (?,?,?,?) ORDER BY event_id", (
                    "mes_to_field_mqtt_dispatched", "field_opcua_signal_reported",
                    "field_vision_http_reported", "field_modbus_reported"))]
        jsonl = [json.loads(line) for line in (folder / "protocol_messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["protocol_messages"], db_messages)
        self.assertEqual(jsonl, db_messages)
        self.assertEqual(protocol_events, db_messages)
        self.assertTrue(all(row["alarm_summary"] for row in rows if row["is_anomaly"]))
        self.assertEqual(len({m["message_id"] for m in db_messages}), len(db_messages))
        return folder, result, observer

    def test_normal_12_minutes_no_fault_and_four_protocols(self):
        folder, result, observer = self.simulate(fixture())
        self.assertEqual(result["makespan_min"], 12)
        self.assertEqual(result["delay_summary"][0], dict(work_order_id="DEMO-WO-1",
            planned_minutes=12, actual_minutes=12, delay_minutes=0, anomaly_status="NORMAL"))
        self.assertEqual(observer.states, [])
        self.assertTrue(all(not a["had_fault"] and a["status"] == "normal" for a in result["equipment_alerts"]))
        messages = result["protocol_messages"]
        self.assertEqual(len(messages), 6)
        self.assertEqual({m["protocol"] for m in messages},
                         {"MQTT", "OPC-UA", "HTTP-Vision-Webhook", "Modbus-TCP"})
        self.assertEqual([m["virtual_minute"] for m in messages], [0, 6, 6, 10, 10, 12])

    def test_all_anomalies_real_fault_repair_rework_and_delay(self):
        folder, result, observer = self.simulate(fixture("anomaly"))
        self.assertEqual(result["makespan_min"], 28)
        self.assertEqual(result["delay_summary"][0]["delay_minutes"], 16)
        self.assertEqual(result["delay_summary"][0]["anomaly_status"], "DELAYED_BY_FAULT")
        self.assertEqual([r["status"] for _, r in observer.states],
                         ["fault", "available", "fault", "available", "fault", "available"])
        self.assertEqual([e["sim_time_min"] for e, _ in observer.states], [6, 12, 16, 19, 23, 28])
        for event, row in observer.states:
            self.assertEqual(row["status"], event["detail"]["status"])
            self.assertTrue(row["alarm_message"])
        self.assertEqual(len(result["protocol_messages"]), 10)
        self.assertEqual(sum(m["is_anomaly"] for m in result["protocol_messages"]), 3)
        alerts = result["equipment_alerts"]
        self.assertTrue(all(a["had_fault"] and a["status"] == "fault" and
                            a["current_status"] == "available" for a in alerts))
        self.assertTrue(all(a["fault_records"][0]["recovered_virtual_minute"] is not None for a in alerts))
        self.assertIn("(3/4)", next(a for a in alerts if a["device_code"] == "Vision_01")["alarm_message"])
        detail = mes.get_work_order(folder / "mes.db", "DEMO-WO-1")
        self.assertEqual((detail["planned_minutes"], detail["actual_minutes"], detail["delay_minutes"]), (12, 28, 16))
        self.assertEqual(detail["reworks"][0]["status"], "completed")
        self.assertEqual(len(detail["rework_actions"]), 4)
        self.assertEqual([(i["phase"], i["end_min"]-i["start_min"]) for i in result["intervals"]],
                         [("normal", 12), ("normal", 4), ("repair", 3), ("reinspection", 2), ("normal", 7)])

    def test_each_device_independently_changes_duration(self):
        for device, expected in (("E-ASM", 18), ("E-QC", 17), ("E-MOVE", 17)):
            with self.subTest(device=device):
                _, result, observer = self.simulate(fixture("anomaly", [device]), device)
                self.assertEqual(result["makespan_min"], expected)
                self.assertEqual(len(observer.states), 2)
                self.assertEqual(sum(a["had_fault"] for a in result["equipment_alerts"]), 1)

    def test_returned_sample_drives_fault_instead_of_requested_mode(self):
        original = field_protocols.build_cnc_opcua_message

        def normal_sample(*args, **kwargs):
            kwargs["is_anomaly"] = False
            return original(*args, **kwargs)

        with patch("simulation.field_protocols.build_cnc_opcua_message", side_effect=normal_sample):
            _, result, observer = self.simulate(fixture("anomaly", ["E-ASM"]))
        self.assertEqual(result["makespan_min"], 12)
        self.assertFalse(observer.states)

    def test_sampled_replay_and_probability_extremes(self):
        snapshot = fixture("sampled")
        snapshot["orders"][0]["field_protocols"].update(seed=42, anomaly_probability=.5)
        _, first, _ = self.simulate(snapshot, "first")
        _, second, _ = self.simulate(snapshot, "second")
        self.assertEqual(first["protocol_messages"], second["protocol_messages"])
        self.assertEqual(first["delay_summary"], second["delay_summary"])
        for probability, expected in ((0, 12), (1, 28)):
            snapshot["orders"][0]["field_protocols"]["anomaly_probability"] = probability
            _, result, _ = self.simulate(snapshot, f"probability-{probability}")
            self.assertEqual(result["makespan_min"], expected)

    def test_concurrent_orders_wait_for_fault_recovery(self):
        snapshot = demo_snapshot()
        snapshot = configure(snapshot, {w["work_order_id"]: Override(field_protocols=FieldScenario(mode="anomaly"))
                                        for w in snapshot["orders"]})
        _, result, observer = self.simulate(snapshot)
        self.assertEqual(len(result["orders"]), 2)
        self.assertTrue(all(w["anomaly_status"] == "DELAYED_BY_FAULT" for w in result["orders"]))
        self.assertEqual(sum(r["status"] == "fault" for _, r in observer.states), 6)
        for eq in snapshot["equipment"]:
            intervals = sorted((i for i in result["intervals"] if i["equipment_id"] == eq["equipment_id"]),
                               key=lambda i: i["start_min"])
            self.assertTrue(all(a["end_min"] <= b["start_min"] for a, b in zip(intervals, intervals[1:])))

    def test_service_persists_summary_get_export_and_source_unchanged(self):
        source = self.root / "source.db"
        engine.seed_database(source, fixture())
        before = source.read_bytes()
        service = SimulationService(source, self.root / "test-runs")
        service.startup()
        self.addCleanup(service.shutdown)
        request = RunRequest(source="work_orders", work_order_ids=["DEMO-WO-1"], overrides={
            "DEMO-WO-1": Override(field_protocols=FieldScenario(mode="anomaly"))})
        started = service.start(request)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = service.get(started["run_id"])
            if record["status"] != "running":
                break
            time.sleep(.01)
        self.assertEqual(record["status"], "completed", record.get("error"))
        for key in ("equipment_alerts", "delay_summary", "protocol_messages"):
            self.assertEqual(record[key], record["summary"][key])
        saved = json.loads((service.folder(record["run_id"]) / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, record["summary"])
        exported = json.loads(service.export(record["run_id"], "json")[0])
        self.assertEqual(exported["delay_summary"][0]["actual_minutes"], 28)
        self.assertEqual(source.read_bytes(), before)

    def test_validation_and_reset_to_normal(self):
        for bad in ({"sample_index": 12}, {"cnc_repair_minutes": 0}, {"anomaly_probability": 2},
                    {"mode": "typo"}, {"anomaly_equipment_ids": ["other"]}):
            with self.assertRaises(ValidationError):
                FieldScenario(**bad)
        snapshot = fixture("anomaly")
        normal = configure(snapshot, {"DEMO-WO-1": Override(field_protocols=None)})
        self.assertEqual(normal["orders"][0]["field_protocols"]["mode"], "normal")
        snapshot["orders"][0]["operations"] = snapshot["orders"][0]["operations"][1:]
        with self.assertRaises(mes.BusinessError):
            configure(snapshot, {})

    def test_existing_database_cannot_be_overwritten(self):
        path = self.root / "mes.db"
        path.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            engine.seed_database(path, fixture())
        self.assertEqual(path.read_bytes(), b"do not overwrite")


if __name__ == "__main__":
    unittest.main()
