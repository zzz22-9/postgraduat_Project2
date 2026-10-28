from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from task2_1_prototype.contracts.models import RawIngestMessage, SemanticObject, parse_timestamp, utc_text
from task2_1_prototype.services.semantic_object_service import MappingEngine, MappingError, SQLiteStore, open_store, semantic_mapping
from task2_1_prototype.services.semantic_object_service.api import create_app, create_router
from task2_1_prototype.services.semantic_object_service.fusion import SIGNALS
from task2_1_prototype.services.semantic_object_service.models import VALIDATORS
from task2_1_prototype.services.semantic_object_service.pipeline import run_batch, summarize, fault_timeline
from task2_1_prototype.services.connector_service.pipeline import run_batch as connector_batch
from task2_1_prototype.simulators.simpy_factory import run_demo


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def hashes(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}


class Wp3SemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.wp1, cls.wp2 = cls.root / "wp1", cls.root / "wp2"
        run_demo(cls.wp1)
        connector_batch(cls.wp1, cls.wp2)
        cls.source_hashes = (hashes(cls.wp1), hashes(cls.wp2))
        cls.raws = read_lines(cls.wp2 / "raw_ingest_messages.jsonl")
        cls.full_store = SQLiteStore()
        cls.addClassCleanup(cls.full_store.close)
        cls.full_engine = MappingEngine(cls.full_store)
        cls.responses = cls.full_engine.map_batch(cls.raws)

    def setUp(self):
        self.store = SQLiteStore()
        self.addCleanup(self.store.close)
        self.engine = MappingEngine(self.store)

    def sample(self, mode="timeseries_signal", second=0, protocol="MQTT"):
        return deepcopy(next(r for r in self.raws if r["modality"] == mode and r["protocol"] == protocol
                             and r["raw_context"].get("sim_time_s") == second
                             and r["raw_context"]["_ingest"]["resolved_ids"].get("equipment_id") == "CNC_01"))

    def retime(self, raw, seconds, suffix):
        raw = deepcopy(raw)
        raw["message_id"] += "/" + suffix
        raw["raw_context"]["sim_time_s"] = seconds
        raw["timestamp"] = utc_text(parse_timestamp(raw["raw_context"]["simulation_epoch"]) + timedelta(seconds=seconds))
        raw["raw_context"]["_ingest"]["source_sequence"] += 10000
        return raw

    def check_object(self, obj):
        self.assertEqual(set(obj), {"object_id", "object_type", "domain", "level", "state", "context", "timestamp"})
        SemanticObject.model_validate(obj)
        VALIDATORS["SemanticObject"].validate(obj)

    def test_full_batch_outputs_exact_seven_fields_five_types_four_levels(self):
        self.assertEqual(len(self.raws), 2206)
        objects = self.full_store.query()
        self.assertEqual(len(objects), 12)
        self.assertEqual({o["object_type"] for o in objects}, {"order", "work_order", "process", "equipment", "resource"})
        self.assertEqual({o["level"] for o in objects}, {"enterprise", "factory", "line", "device"})
        for obj in objects + self.responses + self.full_store.query(history=True):
            self.check_object(obj)
        for obj in objects:
            if obj["object_type"] == "process":
                self.assertEqual((obj["domain"], obj["level"]), ("manufacturing", "line"))
        self.assertEqual(self.full_store.counts(), {"semantic_objects": 12, "semantic_state_history": 2215,
                                                 "processed_messages": 2206, "signal_cycles": 360})

    def test_standard_function_accepts_dict_and_raw_model_without_global_state(self):
        raw = self.sample()
        first = semantic_mapping(raw)
        second = semantic_mapping(RawIngestMessage.model_validate(raw))
        self.assertEqual(first, second)
        self.check_object(first)
        self.assertEqual(first["state"]["sample_count"], 1)

    def test_reject_source_envelopes_diagnostics_and_invalid_contracts(self):
        invalid = [{"sequence": 1, "payload": {}}, {"code": "PLC_FRAME", "reason": "CRC mismatch"}]
        raw = self.sample()
        for key in raw:
            item = deepcopy(raw)
            del item[key]
            invalid.append(item)
        invalid += [{**raw, "unexpected": True}, {**raw, "protocol": "BAD"}, {**raw, "timestamp": "2026-02-30T00:00:00Z"}]
        for value in (float("nan"), float("inf")):
            item = deepcopy(raw)
            item["raw_payload"]["temperature_c"] = value
            invalid.append(item)
        for item in invalid:
            with self.assertRaises((ValidationError, ValueError)):
                self.engine.semantic_mapping(item)
        self.assertEqual(self.store.counts()["processed_messages"], 0)

    def test_required_provenance_and_simulation_epoch_consistency(self):
        for change in ("provenance", "parent", "clock"):
            raw = self.sample()
            if change == "provenance":
                raw["raw_context"].pop("_ingest")
            elif change == "parent":
                raw["raw_context"]["_ingest"]["resolved_ids"]["process_instance_key"] = ["WO_OTHER", "OP20"]
            else:
                raw["raw_context"]["sim_time_s"] = 100
            with self.assertRaises(MappingError):
                self.engine.semantic_mapping(raw)

    def test_all_2160_signals_align_to_360_physical_cycles(self):
        summary = summarize(self.full_store)
        self.assertEqual(summary["signal_cycles"], 360)
        self.assertEqual(summary["complete_cycles"], 360)
        self.assertEqual(summary["cycles_with_all_three_protocols"], 360)
        self.assertEqual(summary["cycles_by_device"], {"CNC_01": 180, "QC_01": 120, "MOVE_01": 60})
        self.assertEqual(self.full_store.latest("CNC_01")["state"]["sample_count"], 180)
        self.assertEqual(self.full_store.latest("CNC_01")["context"]["observed_protocols"], ["MQTT", "OPC_UA", "PLC_FRAME"])

    def test_opc_four_points_aggregate_in_any_same_time_order(self):
        records = [deepcopy(r) for r in self.raws if r["modality"] == "timeseries_signal"
                   and r["protocol"] == "OPC_UA" and r["raw_context"].get("sim_time_s") == 0]
        random.Random(23).shuffle(records)
        results = self.engine.map_batch(records)
        self.assertEqual(len(results), 4)
        latest = self.store.latest("CNC_01")
        self.assertEqual(latest["state"]["sample_count"], 1)
        self.assertTrue(latest["state"]["signal_complete"])
        self.assertEqual(set(latest["state"]["latest_signal"]), set(SIGNALS))

    def test_plc_quantization_and_protocol_arrival_order(self):
        records = [deepcopy(r) for r in self.raws if r["modality"] == "timeseries_signal"
                   and r["raw_context"].get("sim_time_s") == 0]
        # Quantized frame first, then OPC metrics, then full precision MQTT.
        records.sort(key=lambda r: {"PLC_FRAME": 0, "OPC_UA": 1, "MQTT": 2}[r["protocol"]])
        self.engine.map_batch(records)
        latest = self.store.latest("CNC_01")
        expected = self.sample()["raw_payload"]
        for name in SIGNALS:
            self.assertEqual(latest["state"][name], expected[name])
        self.assertEqual(latest["state"]["sample_count"], 1)

    def test_conflicting_protocol_observation_is_atomic_failure(self):
        self.engine.semantic_mapping(self.sample())
        before = self.store.latest("CNC_01")
        counts = self.store.counts()
        bad = self.sample(protocol="PLC_FRAME")
        bad["raw_payload"]["temperature_c"] += 20
        with self.assertRaisesRegex(MappingError, "quantization"):
            self.engine.semantic_mapping(bad)
        self.assertEqual(self.store.latest("CNC_01"), before)
        self.assertEqual(self.store.counts(), counts)

    def test_sliding_window_uses_physical_samples_once(self):
        records = [r for r in self.raws if r["modality"] == "timeseries_signal" and r["raw_context"].get("sim_time_s", 999) <= 11]
        self.engine.map_batch(records)
        state = self.store.latest("CNC_01")["state"]
        expected = [self.sample(second=i)["raw_payload"]["temperature_c"] for i in range(2, 12)]
        self.assertEqual(state["sample_count"], 12)
        self.assertEqual(state["window_sample_count"], 10)
        self.assertAlmostEqual(state["window_features"]["temperature_c"]["mean"], sum(expected) / 10, places=7)
        self.assertEqual(state["window_features"]["temperature_c"]["peak"], max(expected))
        self.assertEqual(state["window_features"]["temperature_c"]["count"], 10)

    def test_real_fault_interval_and_explicit_recovery(self):
        summary = summarize(self.full_store)
        interval = summary["fault_intervals"]
        self.assertEqual([(i["object_id"], i["start_s"], i["end_s"]) for i in interval], [("CNC_01", 60, 90)])
        final = self.full_store.latest("CNC_01")["state"]
        self.assertEqual((final["fault_count"], final["recovery_count"]), (1, 1))
        self.assertFalse(final["fault_latched"])
        self.assertEqual(final["last_fault_codes"], ["0x84"])
        self.assertNotIn("active_fault_codes", final)
        samples = {}
        for obj in self.full_store.query(history=True, object_id="CNC_01"):
            samples[obj["context"]["sim_time_s"]] = obj["state"]["status"]
        self.assertEqual([i for i in range(180) if samples[i] == "fault"], list(range(60, 90)))

    def test_generator_truth_removed_or_inverted_does_not_change_diagnosis(self):
        subset = [r for r in self.raws if r["raw_context"]["_ingest"]["resolved_ids"].get("equipment_id") == "CNC_01"
                  and (r["modality"] == "log_text" or r["modality"] == "timeseries_signal" and r["protocol"] == "MQTT")]
        timelines = []
        for mode in ("remove", "opposite"):
            store = SQLiteStore()
            try:
                records = deepcopy(subset)
                for raw in records:
                    payload = raw["raw_payload"]
                    for target in (payload, payload.get("source_payload", {})):
                        if mode == "remove":
                            target.pop("injected_fault", None)
                            target.pop("control_cmd", None)
                        else:
                            target["injected_fault"] = not (60 <= raw["raw_context"]["sim_time_s"] < 90)
                            target["control_cmd"] = 0 if 60 <= raw["raw_context"]["sim_time_s"] < 90 else 2
                MappingEngine(store).map_batch(records)
                timelines.append(fault_timeline(store.query(history=True)))
            finally:
                store.close()
        self.assertEqual(timelines[0], timelines[1])
        self.assertEqual([(i["start_s"], i["end_s"]) for i in timelines[0]["fault_intervals"]], [(60, 90)])

    def test_numeric_thresholds_do_not_need_alarm_log_or_generator_truth(self):
        raw = self.sample(second=60)
        raw["raw_payload"]["injected_fault"] = False
        raw["raw_payload"]["control_cmd"] = 0
        obj = self.engine.semantic_mapping(raw)
        self.assertEqual(obj["state"]["status"], "fault")
        self.assertEqual(obj["state"]["fault_count"], 1)

    def test_warning_overload_idle_rules_and_boundary(self):
        for metric, value, status in (("temperature_c", 72, "running"), ("temperature_c", 73, "warning"),
                                      ("vibration_mm_s", 4, "warning"), ("load_ratio", .85, "overload")):
            raw = self.sample()
            raw["raw_payload"][metric] = value
            self.assertEqual(semantic_mapping(raw)["state"]["status"], status)
        raw = self.sample()
        raw["raw_payload"].update(spindle_speed_rpm=0, load_ratio=0)
        self.assertEqual(semantic_mapping(raw)["state"]["status"], "idle")

    def test_normal_images_info_and_business_status_cannot_clear_fault(self):
        self.engine.semantic_mapping(self.sample(second=60))
        image = self.retime(self.sample("image"), 61, "normal-image")
        info = self.retime(self.sample("log_text"), 62, "info")
        business = self.retime(self.sample(), 63, "business")
        business.update(modality="structured_record", raw_payload={"status": "running"})
        for raw in (image, info, business):
            obj = self.engine.semantic_mapping(raw)
            self.assertEqual(obj["state"]["status"], "fault")
            self.assertTrue(obj["state"]["fault_latched"])
        process = self.store.latest("OP20")
        self.assertEqual(process["state"]["visual_inspection"]["annotation_source"], "synthetic_label")

    def test_normal_signals_alone_cannot_clear_fault(self):
        self.engine.semantic_mapping(self.sample(second=60))
        obj = self.engine.semantic_mapping(self.sample(second=90))
        self.assertEqual(obj["state"]["signal_health"], "running")
        self.assertEqual(obj["state"]["status"], "fault")

    def test_recovery_log_requires_normal_current_or_subsequent_full_signal(self):
        self.engine.semantic_mapping(self.sample(second=60))
        clear = self.sample("log_text", 90)
        obj = self.engine.semantic_mapping(clear)
        self.assertEqual(obj["state"]["status"], "fault")
        self.assertIn("recovery_pending_since", obj["state"])
        obj = self.engine.semantic_mapping(self.sample(second=90))
        self.assertEqual(obj["state"]["status"], "running")
        self.assertEqual(obj["state"]["recovery_count"], 1)

    def test_normal_signal_then_same_time_recovery_log_is_supported(self):
        self.engine.semantic_mapping(self.sample(second=60))
        self.engine.semantic_mapping(self.sample(second=90))
        obj = self.engine.semantic_mapping(self.sample("log_text", 90))
        self.assertEqual(obj["state"]["status"], "running")

    def test_error_log_parses_severity_codes_and_fault(self):
        obj = self.engine.semantic_mapping(self.sample("log_text", 60))
        self.assertEqual(obj["state"]["log"]["level"], "ERROR")
        self.assertEqual(obj["state"]["active_fault_codes"], ["0x84"])
        self.assertEqual(obj["state"]["status"], "fault")

    def test_late_old_fault_and_recovery_only_enter_history(self):
        self.engine.map_batch([self.sample(second=60), self.sample("log_text", 90), self.sample(second=90)])
        before = self.store.latest("CNC_01")
        late = self.retime(self.sample("log_text", 60), 65, "late-fault")
        result = self.engine.semantic_mapping(late)
        self.assertTrue(result["context"]["history"]["late"])
        self.assertFalse(result["context"]["history"]["applied_to_latest"])
        self.assertEqual(self.store.latest("CNC_01"), before)
        self.assertEqual(result["state"]["status"], "fault")
        # Old recovery evidence cannot change a newer fault either.
        self.engine.semantic_mapping(self.retime(self.sample(second=60), 100, "new-fault"))
        before = self.store.latest("CNC_01")
        self.engine.semantic_mapping(self.retime(self.sample("log_text", 90), 95, "old-clear"))
        self.assertEqual(self.store.latest("CNC_01"), before)

    def test_duplicate_persistence_restart_and_message_conflict(self):
        path = self.root / "restart" / "semantic_objects.sqlite3"
        raw = self.sample(second=60)
        store = SQLiteStore(path)
        try:
            first = MappingEngine(store).semantic_mapping(raw)
            counts = store.counts()
        finally:
            store.close()
        store = SQLiteStore(path)
        try:
            engine = MappingEngine(store)
            self.assertEqual(engine.semantic_mapping(raw), first)
            self.assertEqual(store.counts(), counts)
            bad = deepcopy(raw)
            bad["raw_payload"]["temperature_c"] += 1
            with self.assertRaisesRegex(MappingError, "different content"):
                engine.semantic_mapping(bad)
            engine.map_batch([self.sample("log_text", 90), self.sample(second=90)])
            self.assertEqual(store.latest("CNC_01")["state"]["recovery_count"], 1)
        finally:
            store.close()

    def test_concurrent_same_message_is_atomic_idempotent(self):
        raw = self.sample()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.engine.semantic_mapping, [raw] * 20))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.store.counts()["processed_messages"], 1)
        self.assertEqual(self.store.counts()["signal_cycles"], 1)
        self.assertEqual(self.store.counts()["semantic_state_history"], 1)

    def test_process_parent_isolation_generic_operation_ids_and_no_merging(self):
        template = deepcopy(next(r for r in self.raws if r["object_hint"]["suggested_type"] == "process"))
        for work, process in (("WO023", "OP20"), ("WO024", "OP20_2"), ("WO023", "OP10"), ("WO023", "OP30")):
            raw = deepcopy(template)
            raw["message_id"] += work + process
            ids = raw["raw_context"]["_ingest"]["resolved_ids"]
            ids.update(object_id=process, process_id=process, work_order_id=work, process_instance_key=[work, process])
            result = self.engine.semantic_mapping(raw)
            self.assertEqual(result["object_id"], process)
            self.assertEqual(result["context"]["work_order"], work)
            self.assertEqual(result["level"], "line")
        self.assertEqual(len(self.store.query(object_type="process")), 4)
        bad = deepcopy(template)
        bad["message_id"] += "wrong-parent"
        ids = bad["raw_context"]["_ingest"]["resolved_ids"]
        ids.update(work_order_id="WO024", process_instance_key=["WO024", "OP20"])
        with self.assertRaises(MappingError):
            self.engine.semantic_mapping(bad)

    def test_image_updates_equipment_and_process_without_fake_vision_diagnosis(self):
        raw = self.sample("image", 60)
        obj = self.engine.semantic_mapping(raw)
        self.assertNotEqual(obj["state"]["status"], "fault")
        for object_id in ("CNC_01", "OP20"):
            vision = self.store.latest(object_id)["state"]["visual_inspection"]
            self.assertEqual(vision["synthetic_label"], "fault")
            self.assertEqual(vision["annotation_source"], "synthetic_label")
            self.assertEqual(vision["image_ref"], raw["raw_payload"]["image_ref"])
            self.assertEqual(vision["sha256"], raw["raw_payload"]["sha256"])
        self.assertEqual(self.store.counts()["semantic_state_history"], 2)

    def test_multi_target_update_rolls_back_on_process_identity_conflict(self):
        self.engine.semantic_mapping(self.sample("image"))
        before, counts = self.store.query(), self.store.counts()
        bad = self.retime(self.sample("image"), 1, "wrong-parent")
        ids = bad["raw_context"]["_ingest"]["resolved_ids"]
        ids.update(work_order_id="WO_OTHER", process_instance_key=["WO_OTHER", "OP20"])
        with self.assertRaises(MappingError):
            self.engine.semantic_mapping(bad)
        self.assertEqual(self.store.query(), before)
        self.assertEqual(self.store.counts(), counts)

    def test_order_merge_material_and_organization_nodes(self):
        order = self.full_store.latest("ORDER_A")
        self.assertEqual(order["state"]["status"], "dispatched")
        self.assertEqual(order["context"]["timestamp_basis"], "acquisition_wall_clock")
        self.assertEqual(set(order["context"]["sources"]), {"REST_API/structured_record", "SQLITE/structured_record"})
        history = self.full_store.query(history=True, object_id="ORDER_A")
        self.assertEqual([r["state"]["status"] for r in history], ["pending", "dispatched"])
        self.assertEqual(self.full_store.latest("RES_MAT_01")["context"]["resource_kind"], "material")
        for name, level in (("ENT_01", "enterprise"), ("FAC_01", "factory"), ("LINE_01", "line")):
            obj = self.full_store.latest(name)
            self.assertEqual((obj["context"]["resource_kind"], obj["level"]), ("organization", level))

    def test_wall_and_simulation_work_order_tracks_are_not_compared_as_one_clock(self):
        obj = self.full_store.latest("WO023")
        self.assertEqual(obj["state"]["status"], "completed")
        self.assertTrue(obj["context"]["clock_scope"].startswith("simulation/"))
        self.assertEqual(obj["context"]["clock_states"]["wall"]["state"]["status"], "released")
        self.assertLess(obj["timestamp"], obj["context"]["clock_states"]["wall"]["timestamp"])
        # Older wall event at the same timestamp is blocked by transport sequence.
        records = [r for r in self.raws if r["source_system"] == "MES" and r["object_hint"]["suggested_type"] == "work_order"]
        self.engine.map_batch(list(reversed(records)))
        self.assertEqual(self.store.latest("WO023")["state"]["status"], "released")

    def test_filters_and_history_time_window(self):
        self.assertEqual(len(self.full_store.query(object_type="resource", domain="enterprise")), 2)
        self.assertEqual(len(self.full_store.query(level="device")), 3)
        records = self.full_store.query(history=True, object_id="CNC_01", start="2026-10-28T10:31:00+08:00", end="2026-10-28T10:31:01+08:00")
        self.assertTrue(records)
        self.assertTrue(all(r["context"]["sim_time_s"] in (60, 61) for r in records))
        with self.assertRaises(ValueError):
            self.full_store.query(start="invalid")

    def test_fastapi_single_batch_filters_history_and_mountable_router(self):
        with TestClient(create_app(engine=self.engine)) as client:
            response = client.post("/semantic/object", json=self.sample())
            self.assertEqual(response.status_code, 200, response.text)
            self.check_object(response.json())
            response = client.post("/semantic/object", json=[self.sample(second=1), self.sample(second=2)])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()), 2)
            response = client.get("/semantic/objects", params={"object_type": "equipment", "domain": "manufacturing", "level": "device"})
            self.assertEqual(len(response.json()), 1)
            self.check_object(response.json()[0])
            self.assertEqual(len(client.get("/semantic/objects/CNC_01/history").json()), 3)
            self.assertEqual(client.get("/semantic/objects", params={"level": "invalid"}).status_code, 422)
            self.assertEqual(client.get("/semantic/objects/CNC_01/history", params={"start": "yesterday"}).status_code, 422)
        self.assertEqual(len(create_router(lambda: self.engine).routes), 3)

    def test_fastapi_rejects_source_envelope_and_batch_is_atomic(self):
        with TestClient(create_app(engine=self.engine)) as client:
            self.assertEqual(client.post("/semantic/object", json={"sequence": 1, "payload": {}}).status_code, 422)
            bad = self.sample(second=1)
            bad["raw_context"].pop("_ingest")
            result = client.post("/semantic/object", json=[self.sample(), bad])
            self.assertEqual(result.status_code, 422)
            self.assertEqual(self.store.counts()["processed_messages"], 0)
            client.post("/semantic/object", json=self.sample())
            bad = self.sample()
            bad["raw_payload"]["temperature_c"] += 1
            self.assertEqual(client.post("/semantic/object", json=bad).status_code, 409)

    def test_factory_lifespan_reopens_persisted_database(self):
        path = self.root / "api" / "semantic_objects.sqlite3"
        with TestClient(create_app(path)) as client:
            self.assertEqual(client.post("/semantic/object", json=self.sample()).status_code, 200)
        with TestClient(create_app(path)) as client:
            self.assertEqual(len(client.get("/semantic/objects").json()), 1)

    def test_storage_guard_rules_batch_and_postgresql_extension_boundary(self):
        with self.assertRaises(ValueError):
            SQLiteStore(self.wp1 / "semantic_objects.sqlite3")
        with self.assertRaises(ValueError):
            SQLiteStore(self.wp2 / "semantic_objects.sqlite3")
        with self.assertRaises(ValueError):
            SQLiteStore(self.wp1 / "demo_erp.sqlite3")
        with self.assertRaises(NotImplementedError):
            open_store(":memory:", backend="POSTGRESQL")
        self.engine.semantic_mapping(self.sample())
        bad = self.sample(second=1)
        bad["raw_context"]["_ingest"]["batch_id"] = "another-batch"
        with self.assertRaises(MappingError):
            self.engine.semantic_mapping(bad)
        with self.assertRaises(MappingError):
            MappingEngine(self.store, rules={"window_size": 20})

    def test_pipeline_full_batch_exports_and_repeat(self):
        output = self.root / "wp3"
        summary = run_batch(self.wp2, output, repeat=2)
        self.assertEqual(summary["input_messages"], 2206)
        self.assertEqual(summary["duplicate_deliveries"], 2206)
        self.assertEqual(summary["signal_cycles"], 360)
        self.assertEqual(len(read_lines(output / "semantic_objects.jsonl")), 12)
        self.assertEqual(len(read_lines(output / "semantic_state_history.jsonl")), 2215)
        for obj in read_lines(output / "semantic_objects.jsonl"):
            self.check_object(obj)
        with self.assertRaises(FileExistsError):
            run_batch(self.wp2, output)
        with self.assertRaises(ValueError):
            run_batch(self.wp2, self.wp2 / "nested")

    def test_z_source_wp1_and_wp2_files_unchanged(self):
        self.assertEqual((hashes(self.wp1), hashes(self.wp2)), self.source_hashes)


if __name__ == "__main__":
    unittest.main()
