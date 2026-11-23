"""Isolated regression for P1 synthetic in-process telemetry and read API."""
import json
import tempfile
import threading
import unittest
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from simulation import engine
from simulation.api import create_router
from simulation.inputs import configure, demo_snapshot
from simulation.schemas import FieldScenario, Override
from simulation.service import SimulationService

RUN_ID = "SIM-" + "a" * 32


def fixture(mode="normal", devices=None):
    snapshot = demo_snapshot()
    snapshot["orders"] = snapshot["orders"][:1]
    wo = snapshot["orders"][0]
    wo["quantity"] = 2
    config = {"mode": mode}
    if devices is not None:
        config["anomaly_equipment_ids"] = devices
    return configure(snapshot, {wo["work_order_id"]: Override(field_protocols=FieldScenario(**config))})


class ContinuousTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_case(self, mode="normal", devices=None):
        folder = self.root / RUN_ID
        folder.mkdir()
        summary = engine.simulate(folder, RUN_ID, fixture(mode, devices),
                                  threading.Event(), lambda *_: None)
        records = [json.loads(x) for x in
                   (folder / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
        return folder, summary, records

    def verify_records(self, records):
        groups = defaultdict(list)
        for r in records:
            self.assertEqual(r["run_id"], RUN_ID)
            self.assertEqual(r["sample_period_s"], 1)
            self.assertTrue(r["synthetic"])
            self.assertEqual(r["transport_mode"], "local_derived")
            self.assertIn(r["protocol"], ("OPC-UA", "Modbus-TCP"))
            self.assertTrue(r["work_order_id"] and r["operation_id"] and r["order_id"])
            self.assertTrue(Path(r["source_file"]).is_file())
            self.assertTrue(r["sample_hash"])
            groups[(r["operation_id"], r["phase"], r["rework_id"])].append(r)
            if r["protocol"] == "OPC-UA":
                nodes = {n["node_id"].rsplit(".", 1)[-1]: n["value"]
                         for n in r["payload"]["nodes"]}
                self.assertEqual(nodes["MotorTemperature"], r["metrics"]["temperature_c"])
            else:
                frame = bytes.fromhex(r["payload"]["response_frame_hex"])
                decoded = [int.from_bytes(frame[9+i*2:11+i*2], "big") for i in range(4)]
                self.assertEqual(decoded, r["payload"]["raw_registers"])
        for group in groups.values():
            self.assertEqual([r["sample_sequence"] for r in group], list(range(len(group))))
            times = [r["virtual_minute"] for r in group]
            self.assertEqual(times, sorted(set(times)))
            self.assertTrue(group[-1]["terminal_message_id"])
            self.assertEqual(group[-1]["generation_method"], "existing_terminal_protocol_sample")
            self.assertFalse(any(r["is_anomaly"] for r in group[:-1]))

    def test_normal_one_second_samples_preserve_existing_protocol(self):
        _, summary, records = self.run_case()
        self.assertEqual((summary["makespan_min"], len(summary["protocol_messages"])), (12, 6))
        self.assertEqual(len(records), 482)
        self.verify_records(records)
        cnc = [r for r in records if r["device_code"] == "CNC_01"]
        self.assertEqual((cnc[0]["virtual_minute"], cnc[-1]["virtual_minute"]), (0, 6))
        self.assertEqual(len(cnc), 361)
        self.assertGreater(len({r["metrics"]["current_a"] for r in cnc}), 2)

    def test_anomalies_and_rework_preserve_original_timing(self):
        for mode, devices, minutes in (("anomaly", ["E-QC"], 17), ("anomaly", None, 28)):
            with self.subTest(mode=mode, devices=devices):
                self.root = Path(self.temp.name) / str(minutes)
                self.root.mkdir()
                _, summary, records = self.run_case(mode, devices)
                self.assertEqual((summary["makespan_min"], len(summary["protocol_messages"])),
                                 (minutes, 10))
                self.assertEqual(len(records), 663)
                self.verify_records(records)
                self.assertTrue(any(r["phase"] == "repair" for r in records))
                self.assertEqual(sum(r["is_anomaly"] for r in records),
                                 2 if minutes == 28 else 0)

    def test_bounded_http_pagination_and_legacy_run(self):
        folder, _, records = self.run_case()
        (folder / "run.json").write_text(
            json.dumps({"run_id": RUN_ID, "status": "completed"}), encoding="utf-8")
        service = SimulationService(self.root / "unused-source.db", self.root)
        app = FastAPI()
        app.include_router(create_router(service))
        with TestClient(app) as client:
            url = "/simulation/runs/" + RUN_ID + "/telemetry"
            first = client.get(url, params={"limit": 100})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["total"], len(records))
            self.assertEqual(len(first.json()["items"]), 100)
            self.assertEqual(first.json()["next_offset"], 100)
            second = client.get(url, params={"offset": 100, "limit": 1000})
            self.assertEqual(second.json()["items"], records[100:])
            self.assertIsNone(second.json()["next_offset"])
            self.assertEqual(client.get(url, params={"limit": 1001}).status_code, 422)
            self.assertEqual(client.get("/simulation/runs/SIM-" + "b"*32 + "/telemetry").status_code, 404)
            (folder / "telemetry.jsonl").unlink()
            old = client.get(url).json()
            self.assertFalse(old["available"])
            self.assertEqual(old["items"], [])


if __name__ == "__main__":
    unittest.main()
