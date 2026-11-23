"""Isolated regression for P1 synthetic in-process telemetry and read API."""
import json
import hashlib
import socket
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mes.service import BusinessError
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
            selected = Path(r["source_file"]).read_bytes().splitlines()[r["reference_sample_index"]]
            self.assertEqual(hashlib.sha256(selected).hexdigest(), r["sample_hash"])
            sample = json.loads(selected)
            self.assertEqual(r["source_sample_time_s"], sample["sample_time_s"])
            self.assertEqual(r["source_time_s"], sample["source_time_s"])
            groups[(r["operation_id"], r["phase"], r["rework_id"])].append(r)
            if r["protocol"] == "OPC-UA":
                nodes = {n["node_id"].rsplit(".", 1)[-1]: n["value"]
                         for n in r["payload"]["nodes"]}
                self.assertEqual(nodes["MotorTemperature"], r["metrics"]["temperature_c"])
            else:
                frame = bytes.fromhex(r["payload"]["response_frame_hex"])
                decoded = [int.from_bytes(frame[9+i*2:11+i*2], "big") for i in range(4)]
                self.assertEqual(decoded, r["payload"]["raw_registers"])
                self.assertEqual(len(frame), 17)
                self.assertEqual(int.from_bytes(frame[4:6], "big"), len(frame) - 6)
                self.assertEqual(frame[6:9], bytes([r["payload"]["unit_id"], 3, 8]))
                self.assertTrue(all(type(n) is int and 0 <= n <= 65535 for n in decoded))
                self.assertEqual(r["metrics"], dict(current_a=decoded[0] / 1000,
                    temperature_c=decoded[1] / 10, load_percent=decoded[2] / 10,
                    alarm_code=decoded[3]))
        for group in groups.values():
            self.assertEqual([r["sample_sequence"] for r in group], list(range(len(group))))
            times = [r["virtual_minute"] for r in group]
            self.assertEqual(times, sorted(set(times)))
            for a, b in zip(group, group[1:]):
                self.assertAlmostEqual((b["virtual_minute"] - a["virtual_minute"]) * 60, 1, places=6)
                self.assertEqual(b["elapsed_s"] - a["elapsed_s"], 1)
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

    def test_real_http_paging_invalid_parameters_and_legacy(self):
        folder, _, records = self.run_case()
        (folder / "run.json").write_text(json.dumps({"run_id": RUN_ID}), encoding="utf-8")
        legacy_id = "SIM-" + "b" * 32
        legacy = self.root / legacy_id
        legacy.mkdir()
        (legacy / "run.json").write_text(json.dumps({"run_id": legacy_id}), encoding="utf-8")
        service = SimulationService(self.root / "unused-source.db", self.root)
        app = FastAPI()
        app.include_router(create_router(service))
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop():
            server.should_exit = True
            thread.join(10)
            listener.close()
            self.assertFalse(thread.is_alive())

        thread.start()
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(server.started)
        client = build_opener(ProxyHandler({}))

        def get(path, expected=200):
            try:
                response = client.open(url + path, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                self.assertEqual(response.status, expected)
                return json.load(response)

        path = f"/simulation/runs/{RUN_ID}/telemetry"
        actual, offset = [], 0
        while True:
            page = get(path + f"?offset={offset}&limit=73")
            self.assertEqual(page["total"], len(records))
            self.assertTrue(page["available"])
            actual.extend(page["items"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(actual, records)
        self.assertEqual(get(path + "?offset=9999")["items"], [])
        for query in ("offset=-1", "limit=0", "limit=1001", "offset=x", "limit=x"):
            get(path + "?" + query, 422)
        get("/simulation/runs/bad/telemetry", 404)
        get("/simulation/runs/SIM-" + "c" * 32 + "/telemetry", 404)
        old = get(f"/simulation/runs/{legacy_id}/telemetry")
        self.assertFalse(old["available"])
        self.assertEqual((old["total"], old["items"], old["next_offset"]), (0, [], None))
        # A running writer's incomplete tail is excluded; completed rows remain readable.
        with (folder / "telemetry.jsonl").open("ab") as stream:
            stream.write(b'{"partial":')
        self.assertEqual(get(path)["total"], len(records))
        self.assertFalse((legacy / "telemetry.jsonl").exists())

    def test_terminal_telemetry_is_not_written_when_p03_rejects_message(self):
        original = engine.field.build_cnc_opcua_message

        def wrong_run(*args, **kwargs):
            message, source = original(*args, **kwargs)
            if args[2] > 0:
                message["run_id"] = "SIM-" + "d" * 32
            return message, source

        folder = self.root / RUN_ID
        folder.mkdir()
        with patch.object(engine.field, "build_cnc_opcua_message", side_effect=wrong_run):
            with self.assertRaisesRegex(BusinessError, "run_id"):
                engine.simulate(folder, RUN_ID, fixture(), threading.Event(), lambda *_: None)
        records = [json.loads(line) for line in (folder / "telemetry.jsonl").read_bytes().splitlines()]
        self.assertEqual(len(records), 360)
        self.assertTrue(all(r["terminal_message_id"] is None for r in records))
        self.assertTrue(all(r["run_id"] == RUN_ID for r in records))
        messages = [json.loads(line) for line in (folder / "protocol_messages.jsonl").read_bytes().splitlines()]
        self.assertEqual([m["protocol"] for m in messages], ["MQTT"])


if __name__ == "__main__":
    unittest.main()
