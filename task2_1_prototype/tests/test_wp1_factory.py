from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import struct
import tempfile
import threading
import unittest
import zlib

from PIL import Image

from task2_1_prototype.config import IdRegistry, load_config, validate_config
from task2_1_prototype.contracts import RawIngestMessage
from task2_1_prototype.simulators.legacy_bridge import LEGACY_ROOT, load_legacy, new_erp_client
from task2_1_prototype.simulators.protocol_emulator import ProtocolEmitter, decode_plc_frame
from task2_1_prototype.simulators.simpy_factory import FactoryObserver, run_demo


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_snapshot(folder, snapshot, config, observed=True):
    folder.mkdir()
    registry = IdRegistry(config)
    for ordinal, work in enumerate(snapshot["orders"]):
        registry.bind_work_order(work, ordinal)
    observer = FactoryObserver(folder, config, registry, ProtocolEmitter(folder)) if observed else None
    _, _, _, _, engine, _ = load_legacy()
    summary = engine.simulate(folder, "WP1_DEMO", snapshot, threading.Event(), lambda *_: None, observer=observer)
    return summary


class FactoryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.output = cls.root / "demo"
        cls.source_db = LEGACY_ROOT / "data" / "erp.db"
        cls.original_hash = hashlib.sha256(cls.source_db.read_bytes()).hexdigest() if cls.source_db.exists() else None
        cls.config = load_config()
        cls.result = run_demo(cls.output, cls.config)
        cls.snapshot = json.loads((cls.output / "input.json").read_text())
        cls.summary = json.loads((cls.output / "summary.json").read_text())
        cls.signals = read_lines(cls.output / "signals.jsonl")

    def test_real_business_transactions_inventory_and_source_isolation(self):
        with closing(sqlite3.connect(self.output / "demo_erp.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT status FROM orders").fetchone()[0], "dispatched")
            self.assertEqual(conn.execute("SELECT status FROM work_orders").fetchone()[0], "released")
            self.assertEqual(conn.execute("SELECT count(*) FROM operations WHERE equipment_id IS NOT NULL").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT resource_id,quantity FROM inventory").fetchone(), ("RES_MAT_01", 100))
        with closing(sqlite3.connect(self.output / "mes.db")) as conn:
            self.assertEqual(conn.execute("SELECT status FROM orders").fetchone()[0], "completed")
        if self.original_hash:
            self.assertEqual(hashlib.sha256(self.source_db.read_bytes()).hexdigest(), self.original_hash)
            self.assertEqual(self.original_hash.upper(), "D98A81A49CB950AC8DF38B4B164856D48295E10EDF44BE564A080048D4F0424A")
        with self.assertRaisesRegex(ValueError, "original ERP"):
            with new_erp_client(self.source_db):
                self.fail("protected source database opened")
        with self.assertRaises(FileExistsError):
            run_demo(self.output)

    def test_samples_are_in_real_half_open_activity_windows_and_fault_has_30_samples(self):
        self.assertEqual(len(self.signals), 360)
        self.assertEqual(self.result["makespan_min"], 6)
        for sample in self.signals:
            ctx = sample["context"]
            matching = [i for i in self.summary["intervals"]
                        if i["work_order_id"] == ctx["legacy_ids"]["work_order_id"]
                        and i["operation_id"] == ctx["legacy_ids"]["operation_id"]]
            self.assertEqual(len(matching), 1)
            interval = matching[0]
            self.assertLessEqual(interval["start_min"] * 60, ctx["sim_time_s"])
            self.assertLess(ctx["sim_time_s"], interval["end_min"] * 60)
            self.assertEqual(ctx["order_id"], "ORDER_A")
            self.assertEqual(ctx["work_order_id"], "WO023")
        faulty = [s for s in self.signals if s["injected_fault"]]
        self.assertEqual([s["context"]["sim_time_s"] for s in faulty], list(range(60, 90)))
        self.assertTrue(all(s["signals"]["temperature_c"] > 90 and s["signals"]["vibration_mm_s"] > 10 for s in faulty))
        cnc = [s for s in self.signals if s["equipment_id"] == "CNC_01"]
        self.assertEqual([s["context"]["sim_time_s"] for s in cnc], list(range(180)))
        self.assertFalse(any(s["equipment_id"] == "CNC_01" and s["context"]["sim_time_s"] >= 180 for s in self.signals))

    def test_fixed_seed_replay_is_byte_identical_and_global_rng_unchanged(self):
        replay = self.root / "same_input_replay"
        before = random.getstate()
        run_snapshot(replay, deepcopy(self.snapshot), deepcopy(self.config))
        self.assertEqual(random.getstate(), before)
        for name in ("signals.jsonl", "images.jsonl", "device_logs.jsonl", "equipment.log", "plc_frames.bin"):
            self.assertEqual((replay / name).read_bytes(), (self.output / name).read_bytes(), name)
        for frame in (self.output / "sim_images").glob("*.png"):
            self.assertEqual(frame.read_bytes(), (replay / "sim_images" / frame.name).read_bytes())

    def test_png_full_chunk_crc_decode_and_metadata(self):
        images = read_lines(self.output / "images.jsonl")
        self.assertEqual(len(images), 9)
        self.assertTrue({"start", "processing", "quality", "alarm"} <= {i["stage"] for i in images})
        for record in images:
            path = self.output / record["image_ref"]
            content = path.read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), record["sha256"])
            self.assertEqual(content[:8], b"\x89PNG\r\n\x1a\n")
            offset, chunks = 8, []
            while offset < len(content):
                size = struct.unpack(">I", content[offset:offset+4])[0]
                data = content[offset+4:offset+8+size]
                crc = struct.unpack(">I", content[offset+8+size:offset+12+size])[0]
                self.assertEqual(zlib.crc32(data) & 0xFFFFFFFF, crc)
                chunks.append(data[:4])
                offset += size + 12
            self.assertEqual(chunks[-1], b"IEND")
            self.assertIn(b"IDAT", chunks)
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                im.load()
                self.assertEqual(im.size, (960, 600))
                metadata = json.loads(im.info["task21_metadata"])
                self.assertEqual(metadata["process_id"], record["process_id"])
                self.assertEqual(metadata["annotation_source"], "generator_metadata_not_vision")

    def test_logs_have_devices_context_codes_and_explicit_recovery(self):
        rows = read_lines(self.output / "device_logs.jsonl")
        self.assertEqual(len(rows), 8)
        self.assertEqual((self.output / "equipment.log").read_text(encoding="utf-8").splitlines(), [r["log_message"] for r in rows])
        for row in rows:
            line, ctx = row["log_message"], row["context"]
            self.assertRegex(line, r"\[PLC-0x[0-9A-F]{2}\].*Code 0x[0-9A-F]{2}:")
            for key in ("equipment_id", "process_id", "work_order_id", "order_id"):
                self.assertIn(ctx[key], line)
        self.assertEqual(sum(r.get("state_action") == "fault" for r in rows), 1)
        self.assertEqual(sum(r.get("state_action") == "fault_cleared" for r in rows), 1)

    def test_all_transport_envelopes_can_satisfy_shared_contract_without_wp2_service(self):
        records = read_lines(self.output / "transport_records.jsonl")
        self.assertEqual({r["protocol"] for r in records}, {"REST_API", "SQLITE", "MQTT", "OPC_UA", "PLC_FRAME"})
        self.assertEqual({r["modality"] for r in records}, {"structured_record", "timeseries_signal", "image", "log_text"})
        for record in records:
            # Test-only expected envelope: no production connector or service is implemented.
            raw = {"message_id": f"WP1-{record['sequence']}", **{k: record[k] for k in
                   ("source_system", "protocol", "modality", "timestamp", "object_hint")},
                   "raw_payload": record["payload"], "raw_context": record["context"]}
            RawIngestMessage.model_validate(raw)
        deliveries = read_lines(self.output / "mqtt_received.jsonl")
        self.assertEqual(len(deliveries), self.result["protocols"]["MQTT"])
        self.assertEqual([r["payload"] for r in deliveries], [r for r in records if r["protocol"] == "MQTT"])
        binary = (self.output / "plc_frames.bin").read_bytes()
        frames = [r for r in records if r["protocol"] == "PLC_FRAME"]
        self.assertEqual(len(binary), 18 * len(self.signals))
        counters = {}
        for record in frames:
            p = record["payload"]
            frame = bytes.fromhex(p["frame_hex"])
            self.assertEqual(frame, binary[p["offset"]:p["offset"]+p["length"]])
            decoded = decode_plc_frame(frame)
            address = decoded["address"]
            self.assertEqual(decoded["heartbeat"], counters.get(address, 0))
            counters[address] = decoded["heartbeat"] + 1

    def test_normal_and_fault_have_same_noise_outside_injection(self):
        normal_config = deepcopy(self.config)
        normal_config["fault"]["enabled"] = False
        folder = self.root / "normal"
        run_snapshot(folder, deepcopy(self.snapshot), normal_config)
        normal = read_lines(folder / "signals.jsonl")
        self.assertEqual(len(normal), len(self.signals))
        self.assertEqual(sum(a["signals"] != b["signals"] for a, b in zip(normal, self.signals)), 30)
        for a, b in zip(normal, self.signals):
            if not b["injected_fault"]:
                self.assertEqual(a["signals"], b["signals"])


class LegacyHookTests(unittest.TestCase):
    def test_disabled_observer_exact_default_and_rework_event_baselines(self):
        reference = json.loads((Path(__file__).parent / "fixtures" / "legacy_event_baseline.json").read_text())
        _, _, _, _, _, inputs = load_legacy()
        for name, expected in reference["cases"].items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                snapshot = inputs.demo_snapshot()
                if name == "rework":
                    snapshot["orders"][0]["failure"] = dict(inspection_operation_id="DEMO-1-OP-2", target_operation_id="DEMO-1-OP-1", quantity=1)
                folder = Path(temp) / name
                summary = run_snapshot(folder, snapshot, load_config(), observed=False)
                self.assertEqual([w["completed_min"] for w in summary["orders"]], expected["completed_min"])
                self.assertEqual(summary["makespan_min"], expected["makespan_min"])
                normalized, ids = [], {}
                for event in read_lines(folder / "events.jsonl"):
                    event.pop("recorded_at")
                    event.pop("run_id")
                    if event.get("rework_id"):
                        event["rework_id"] = ids.setdefault(event["rework_id"], "RW"+str(len(ids)+1))
                    normalized.append(event)
                self.assertEqual(normalized, expected["events"])

    def test_coarse_sample_period_does_not_extend_makespan_or_mix_work_orders(self):
        _, _, _, _, _, inputs = load_legacy()
        snapshot = inputs.demo_snapshot()
        config = load_config()
        config["fault"]["enabled"] = False
        config["sample_interval_s"] = 301.0
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "coarse"
            summary = run_snapshot(folder, snapshot, config)
            self.assertEqual([w["completed_min"] for w in summary["orders"]], [6, 9])
            self.assertEqual(summary["makespan_min"], 9)
            samples = read_lines(folder / "signals.jsonl")
            self.assertEqual(len(samples), 6)
            cnc = [s for s in samples if s["equipment_id"] == "CNC_01"]
            self.assertEqual([(s["context"]["sim_time_s"], s["context"]["work_order_id"], s["context"]["process_id"]) for s in cnc],
                             [(0, "WO023", "OP20"), (180, "WO023_2", "OP20_2")])

    def test_invalid_sampling_period_rejected_and_observer_error_not_hidden(self):
        for period in (0, -1, float("nan"), True):
            config = load_config()
            config["sample_interval_s"] = period
            with self.assertRaises(ValueError):
                validate_config(config)
        config = load_config()
        config["fault"]["end_offset_s"] = 181
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "failure"
            with self.assertRaisesRegex(ValueError, "outside active"):
                run_demo(folder, config)
            self.assertEqual(json.loads((folder / "run.json").read_text())["status"], "failed")
            self.assertFalse((folder / "summary.json").exists())
