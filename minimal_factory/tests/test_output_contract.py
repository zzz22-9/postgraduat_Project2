"""P0.4: real HTTP reads and independent reads of persisted output; no network protocol mocks."""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import uvicorn

from erp.app import create_app
from simulation import engine
from simulation.service import SimulationService


def read_batch(folder, run_id):
    """An external consumer: only stdlib file/SQLite reads, never a protocol builder."""
    def read(name):
        return json.loads((folder / name).read_text(encoding="utf-8"))

    def lines(name):
        raw = (folder / name).read_bytes()
        assert not raw or raw.endswith(b"\n"), name
        return [json.loads(line) for line in raw.splitlines()]

    meta, snapshot, mapping = read("run.json"), read("input.json"), read("mapping.json")
    assert meta["run_id"] == run_id and meta["status"] == "completed"
    summary, messages, sources, events = (read("summary.json"), lines("protocol_messages.jsonl"),
                                          lines("source_context.jsonl"), lines("events.jsonl"))
    by_id = {s["message_id"]: s for s in sources}
    assert len(by_id) == len(sources) == len(messages)
    assert set(by_id) == {m["message_id"] for m in messages}
    assert len({m["message_id"] for m in messages}) == len(messages)
    assert messages == summary["protocol_messages"]
    assert summary["event_count"] == len(events)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["run_id"] == run_id for e in events)
    assert all(a["sim_time_min"] <= b["sim_time_min"] for a, b in zip(events, events[1:]))
    orders = {w["work_order_id"]: w for w in snapshot["orders"]}
    assert {w["work_order_id"] for w in mapping} == set(orders)
    protocol_events = [e for e in events if isinstance(e.get("detail"), dict)
                       and "message_id" in e["detail"] and "payload" in e["detail"]]
    assert [e["detail"] for e in protocol_events] == messages
    uri = (folder / "mes.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        db_rows = [dict(r) for r in conn.execute("SELECT * FROM protocol_messages ORDER BY rowid")]
        assert [json.loads(r["raw_json"]) for r in db_rows] == messages
        actions = ("mes_to_field_mqtt_dispatched", "field_opcua_signal_reported",
                   "field_vision_http_reported", "field_modbus_reported")
        assert [json.loads(r[0]) for r in conn.execute(
            "SELECT detail FROM events WHERE action IN (?,?,?,?) ORDER BY event_id", actions)] == messages
        reworks = {r["rework_id"]: dict(r) for r in conn.execute("SELECT * FROM reworks")}
        for message, event, row in zip(messages, protocol_events, db_rows):
            assert message["run_id"] == run_id and message["transport_mode"] == "local_frame"
            wo = orders[message["work_order_id"]]
            op = next(o for o in wo["operations"] if o["operation_id"] == message["operation_id"])
            assert event["order_id"] == wo["order_id"]
            assert event["work_order_id"] == wo["work_order_id"]
            assert event["operation_id"] == op["operation_id"]
            assert event["equipment_id"] == op["equipment_id"] == message["equipment_id"]
            assert message["device_code"] == {"E-ASM": "CNC_01", "E-QC": "Vision_01",
                                               "E-MOVE": "Robot_01"}[op["equipment_id"]]
            assert message["virtual_minute"] == event["sim_time_min"]
            for key in ("message_id", "virtual_minute", "protocol", "work_order_id", "operation_id",
                        "equipment_id", "device_code", "is_anomaly", "source_file", "sample_hash"):
                assert row[key] == message[key], key
            if event["phase"] != "normal":
                rw = reworks[event["rework_id"]]
                assert event["quantity"] == rw["quantity"]
                assert event["work_order_id"] == rw["work_order_id"]
                assert event["operation_id"] == rw[
                    "target_operation_id" if event["phase"] == "repair" else "source_operation_id"]
            else:
                assert event["rework_id"] is None and event["quantity"] == wo["quantity"]
            source = by_id[message["message_id"]]
            assert all(source[k] == message[k] for k in ("source_file", "sample_hash"))
            path = Path(source["source_file"])
            raw = path.read_bytes()
            if message["protocol"] in ("OPC-UA", "Modbus-TCP"):
                selected = raw.splitlines()[source["sample_index"]]
                sample = json.loads(selected)
                assert hashlib.sha256(selected).hexdigest() == source["sample_hash"]
                assert all(source[k] == sample[k] for k in
                           ("sample_id", "sample_index", "sample_time_s", "source_time_s"))
                manifest = json.loads((path.parent / "manifest.json").read_bytes())
                entry = next(e for e in manifest["field_library"]["files"] if e["path"] == path.name)
                assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
                upstream = path.parents[2] / sample["source_file"]
                assert hashlib.sha256(upstream.read_bytes()).hexdigest() == sample["source_sha256"]
                if message["protocol"] == "OPC-UA":
                    assert [n["value"] for n in message["payload"]["nodes"]] == [
                        sample[k] for k in ("current_a", "temperature_c", "load_percent", "alarm_code")]
                else:
                    p = message["payload"]
                    regs = [sample["holding_registers"][str(i)] for i in range(40001, 40005)]
                    assert regs == p["raw_registers"]
                    frame = bytes.fromhex(p["response_frame_hex"])
                    assert frame[6:9] == bytes([p["unit_id"], 3, 8])
                    assert [int.from_bytes(frame[i:i+2], "big") for i in range(9, 17, 2)] == regs
            else:
                assert hashlib.sha256(raw).hexdigest() == source["sample_hash"]
                assert source["sample_time_s"] is None and source["source_time_s"] is None
                if message["protocol"] == "MQTT":
                    p = message["payload"]
                    assert json.loads(bytes.fromhex(p["payload_bytes_hex"])) == p["payload_json"]
                    assert p["payload_json"]["quantity"] == event["quantity"]
                else:
                    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
                    manifest = json.loads((path.parent / "manifest.json").read_bytes())
                    entry = next(e for e in manifest["field_samples"] if e["sample_id"] == source["sample_id"])
                    assert entry["image_sha256"] == source["sample_hash"]
                    detection = message["payload"]["detections"][0]
                    assert detection["count"] == entry["bolt_count"]
                    assert detection["PASS"] == (entry["bolt_count"] == 4)
    return {"run_id": run_id, "status": meta["status"], "protocol_count": len(messages),
            "source_count": len(sources), "event_count": len(events),
            "makespan_min": summary["makespan_min"], "rework_quantity": summary["rework_quantity"]}


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("P04_EVIDENCE_DIR")
        if evidence:
            self.root = Path(evidence) / self._testMethodName
            self.root.mkdir(parents=True, exist_ok=False)
        else:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            self.root = Path(temp.name)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        self.url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self.app = create_app(self.root / "isolated.db", mes_base_url=self.url)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="critical"))
        thread = threading.Thread(target=self.server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop():
            self.server.should_exit = True
            thread.join(10)
            listener.close()
            self.assertFalse(thread.is_alive())

        thread.start()
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not self.server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.server.started)
        self.client = build_opener(ProxyHandler({}))
        self.reads = []

    def request(self, path, data=None, expected=200, method=None):
        request = Request(self.url + path, data=None if data is None else json.dumps(data).encode(),
                          headers={"Content-Type": "application/json"}, method=method)
        try:
            response = self.client.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            self.assertEqual(response.status, expected, raw)
            result = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            self.reads.append({"path": path, "method": request.get_method(), "status": response.status,
                               "content_type": response.headers.get("Content-Type"),
                               "body": result if isinstance(result, (list, dict)) else {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}})
            (self.root / "http_reads.json").write_text(json.dumps(
                {"base_url": self.url, "reads": self.reads}, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

    def work_order(self):
        order = self.request("/orders", {"product_id": "P001", "product_name": "P04 fixture", "quantity": 2}, 201)
        wo = self.request(f"/orders/{order['order_id']}/dispatch", {
            "order_id": order["order_id"], "message_id": "p04-" + self._testMethodName, "priority": 2,
            "steps": [{"name": "CNC", "kind": "assembly", "minutes": 3},
                      {"name": "Vision", "kind": "inspection", "minutes": 2},
                      {"name": "Robot", "kind": "transport", "minutes": 1}]}, 201)
        return order, wo

    def run_request(self, wo, mode="normal", devices=None):
        scenario = {"mode": mode}
        if devices is not None:
            scenario["anomaly_equipment_ids"] = devices
        return {"source": "work_orders", "work_order_ids": [wo["work_order_id"]],
                "overrides": {wo["work_order_id"]: {"field_protocols": scenario}}}

    def wait_run(self, run_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.request(f"/simulation/runs/{run_id}")
            if result["status"] != "running":
                return result
            time.sleep(.02)
        self.fail("isolated run did not finish")

    def test_http_read_inventory_and_images(self):
        self.assertEqual(self.request("/orders"), [])
        order, wo = self.work_order()
        oid, wid = order["order_id"], wo["work_order_id"]
        before = hashlib.sha256((self.root / "isolated.db").read_bytes()).hexdigest()
        self.request("/health")
        self.assertEqual(self.request("/orders")[0]["order_id"], oid)
        self.assertEqual(self.request(f"/orders/{oid}")["status"], "dispatched")
        feed = self.request("/erp/feed")
        self.assertEqual(feed, self.request("/orders/dispatch-outbox"))
        self.assertEqual(feed["dispatch_outbox"][0]["http"]["status_code"], 201)
        self.request("/mes/dashboard")
        self.assertEqual(self.request("/mes/work-orders")[0]["work_order_id"], wid)
        detail = self.request(f"/mes/work-orders/{wid}")
        self.assertEqual(detail, self.request(f"/mes/work-orders/{wid}/export")["work_order"])
        self.assertEqual(len(self.request("/mes/equipment")), 3)
        self.request("/simulation/runs")
        asset_root = Path(engine.__file__).resolve().parents[1] / "synthetic_images/rendered_v1"
        for name in ("normal_front.png", "normal_side.png", "defect_front.png", "defect_side.png"):
            self.assertEqual(self.request("/static/rendered_v1/" + name), (asset_root / name).read_bytes())
        for path in ("/orders/missing", "/mes/work-orders/missing", "/simulation/runs/bad",
                     "/static/rendered_v1/not_allowed.png", "/mes/vision/webhook"):
            self.assertIn("detail", self.request(path, expected=404))
        self.assertIn("detail", self.request("/orders", {"quantity": 0}, 422))
        self.assertEqual(before, hashlib.sha256((self.root / "isolated.db").read_bytes()).hexdigest())

    def test_completed_modes_independent_file_and_http_reads(self):
        _, wo = self.work_order()
        results = []
        for mode, devices, minutes, count in (("normal", None, 12, 6),
                                               ("anomaly", ["E-QC"], 17, 10),
                                               ("anomaly", None, 28, 10)):
            started = self.request("/simulation/runs", self.run_request(wo, mode, devices), 202)
            rid = started["run_id"]
            record = self.wait_run(rid)
            self.assertEqual(record["status"], "completed")
            folder = self.app.state.simulation.root / rid
            result = read_batch(folder, rid)
            self.assertEqual((result["makespan_min"], result["protocol_count"]), (minutes, count))
            self.assertNotIn("source_context", record)
            exported = self.request(f"/simulation/runs/{rid}/export")
            self.assertEqual(exported["protocol_messages"], record["protocol_messages"])
            self.assertEqual(exported["summary"], json.loads((folder / "summary.json").read_bytes()))
            self.assertTrue(exported["mes_work_orders"])
            self.request(f"/simulation/runs/{rid}/export?format=events_csv")
            self.request(f"/simulation/runs/{rid}/export?format=summary_csv")
            self.request(f"/simulation/runs/{rid}/export?format=invalid", expected=422)
            results.append(result)
        (self.root / "batch_reads.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    def test_failed_run_preserves_prefix_and_is_not_completed(self):
        _, wo = self.work_order()
        with patch.object(engine.field, "build_cnc_opcua_message", side_effect=RuntimeError("P04 controlled failure")):
            started = self.request("/simulation/runs", self.run_request(wo), 202)
            rid = started["run_id"]
            record = self.wait_run(rid)
        folder = self.app.state.simulation.root / rid
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["summary"])
        self.assertFalse((folder / "summary.json").exists())
        self.assertEqual(len(record["protocol_messages"]), 1)
        self.assertEqual(record["protocol_messages"][0]["protocol"], "MQTT")
        self.assertTrue(record["events"])
        self.assertEqual(len((folder / "source_context.jsonl").read_bytes().splitlines()), 1)
        self.assertEqual(self.request(f"/simulation/runs/{rid}/export")["mes_work_orders"], [])
        self.request(f"/simulation/runs/{rid}/export?format=summary_csv", expected=409)
        self.request(f"/simulation/runs/{rid}/export?format=events_csv")
        with self.assertRaises(AssertionError):
            read_batch(folder, rid)

    def test_legacy_completed_run_without_a2_remains_readable(self):
        _, wo = self.work_order()
        started = self.request("/simulation/runs", self.run_request(wo), 202)
        rid = started["run_id"]
        self.assertEqual(self.wait_run(rid)["status"], "completed")
        folder = self.app.state.simulation.root / rid
        # Deliberate legacy-shape copy; never remove provenance from the real run.
        legacy = self.root / "legacy"
        legacy.mkdir()
        shutil.copytree(folder, legacy / rid)
        (legacy / rid / "source_context.jsonl").unlink()
        service = SimulationService(self.root / "isolated.db", legacy)
        record = service.get(rid)
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["protocol_messages"])
        self.assertIsNotNone(record["summary"])
        self.assertFalse((legacy / rid / "source_context.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
