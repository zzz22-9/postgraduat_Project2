from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from task2_1_prototype.contracts.models import parse_timestamp, utc_text
from task2_1_prototype.run_e2e_demo import check_objects, check_edges
from task2_1_prototype.services.api_gateway import create_gateway_app
from task2_1_prototype.services.connector_service.pipeline import invalid_examples
from task2_1_prototype.services.connector_service.validation import PROJECT
from task2_1_prototype.services.spatiotemporal_model_service.pipeline import read_lines


def hashes(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}


class Wp5GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.output = cls.root / "e2e"
        cls.command = [sys.executable, "-B", "-X", "utf8", "run_e2e_demo.py", "--output", str(cls.output)]
        result = subprocess.run(cls.command, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=240)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.stdout = result.stdout
        cls.summary = json.loads((cls.output / "e2e_summary.json").read_text(encoding="utf-8"))
        cls.source = cls.output / "wp1"
        cls.raw = read_lines(cls.output / "wp2/raw_ingest_messages.jsonl")
        cls.records = read_lines(cls.source / "transport_records.jsonl")
        cls.options = dict(source_dir=cls.source, catalog_file=cls.output / "wp3/semantic_objects.jsonl",
                           bootstrap_raw_file=cls.output / "wp2/raw_ingest_messages.jsonl", bootstrap_until=0)
        cls.input_hashes = [hashes(cls.output / name) for name in ("wp1", "wp2", "wp3", "wp4")]
        cls.full_app = create_gateway_app(cls.output / "gateway")
        cls.full = TestClient(cls.full_app)
        cls.full.__enter__()
        cls.addClassCleanup(cls.full.__exit__, None, None, None)

    def setUp(self):
        self.workspace = self.root / self._testMethodName
        self.app = create_gateway_app(self.workspace, **self.options)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.rt = self.app.state.runtime

    def signal(self, second=1):
        record = deepcopy(next(r for r in self.records if r["modality"] == "timeseries_signal" and r["protocol"] == "MQTT"))
        record["event_key"] = "test/" + str(second)
        record["timestamp"] = utc_text(parse_timestamp(self.rt.manager.epoch) + timedelta(seconds=second))
        record["context"]["sim_time_s"] = second
        return record

    def trigger(self, kind="fault", second=10, event_id=None, **kwargs):
        return self.client.post("/api/v1/events/trigger", json=dict(event_id=event_id or kind, kind=kind,
                                object_id="CNC_01", sim_time_s=second, **kwargs))

    def test_root_cli_runs_from_new_order_through_all_six_stages(self):
        s = self.summary
        self.assertEqual(s["status"], "completed")
        self.assertEqual((s["source_records"], s["raw_messages"], s["semantic_history"]), (2202, 2206, 2215))
        self.assertEqual((s["signal_samples"], s["signal_cycles"], s["image_count"], s["log_count"]), (360, 360, 9, 8))
        self.assertEqual((s["objects"], s["relations"], s["snapshots"]), (12, 19, 4))
        self.assertEqual((s["rejected_messages"], s["duplicate_messages"]), (7, 2202))
        self.assertIn("导师每周汇报五问", self.stdout)

    def test_all_ten_standard_routes_and_compatibility_routes_exist(self):
        paths = self.app.openapi()["paths"]
        self.assertEqual(len([p for p in paths if p.startswith("/api/v1/")]), 10)
        for p in ("/semantic/object", "/semantic/objects", "/semantic/objects/{object_id}/history", "/relations",
                  "/relations/infer", "/model/graph", "/model/impact/{object_id}", "/model/snapshots"):
            self.assertIn(p, paths)

    def test_objects_filters_and_strict_seven_field_responses(self):
        params = {"object_type": "equipment", "domain": "manufacturing", "level": "device"}
        response = self.full.get("/api/v1/objects", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 3)
        check_objects(response.json())
        self.assertEqual(len(self.full.get("/api/v1/objects", params={"object_id": "ORDER_A"}).json()), 1)
        self.assertEqual(self.full.get("/api/v1/objects", params={"object_type": "bad"}).status_code, 422)

    def test_states_and_history_filters_and_not_found(self):
        obj = self.full.get("/api/v1/states/CNC_01").json()
        check_objects(obj)
        scope = self.full_app.state.runtime.manager.scope
        stamp = utc_text(parse_timestamp(self.rt.manager.epoch) + timedelta(seconds=75))
        rows = self.full.get("/api/v1/states/CNC_01/history", params={"start": stamp, "end": stamp, "scope": scope}).json()
        self.assertTrue(rows)
        self.assertTrue(all(o["timestamp"] == stamp and o["context"]["sim_time_s"] == 75 for o in rows))
        check_objects(rows)
        self.assertEqual(self.full.get("/api/v1/states/missing").status_code, 404)
        self.assertEqual(self.full.get("/api/v1/states/CNC_01/history", params={"start": "nonsense"}).status_code, 422)

    def test_relation_filters_and_inference_have_no_side_effects(self):
        counts = self.rt.graph_store.counts()
        response = self.client.post("/api/v1/relations/infer", json={"as_of": 0})
        self.assertEqual(response.status_code, 200)
        check_edges(response.json())
        self.assertEqual(self.rt.graph_store.counts(), counts)
        rows = self.full.get("/api/v1/relations", params={"source": "CNC_01", "target": "OP20", "relation": "executes", "active": True, "as_of": 75}).json()
        self.assertEqual(len(rows), 1)
        check_edges(rows)
        self.assertEqual(self.full.get("/api/v1/relations", params={"source": "CNC_01", "relation": "executes", "active": True, "as_of": 360}).json(), [])

    def test_graph_and_impact_four_checkpoints(self):
        for time, count in ((59, 0), (75, 1), (95, 0), (360, 0)):
            graph = self.full.get("/api/v1/model/graph", params={"as_of": time}).json()
            check_objects(graph["nodes"])
            check_edges(graph["edges"])
            self.assertEqual(len(graph["risk_overlays"]), count)
            impact = self.full.get("/api/v1/model/impact/CNC_01", params={"as_of": time}).json()
            self.assertEqual(len(impact["active_impacts"]), count)
            if time == 75:
                self.assertEqual(impact["active_impacts"][0]["path"], ["CNC_01", "OP20", "WO023", "ORDER_A"])
                self.assertIsNone(impact["impact_events"][0]["end_s"])
            if time == 95:
                self.assertEqual(impact["impact_events"][0]["end_s"], 90)
        self.assertEqual(len(self.full.get("/api/v1/model/snapshots").json()), 4)

    def test_downstream_tree_and_separate_wall_clock(self):
        result = self.full.get("/api/v1/model/impact/ORDER_A", params={"direction": "downstream", "as_of": 75}).json()
        self.assertEqual(len(result["dependencies"]["objects"]), 12)
        graph = self.full.get("/api/v1/model/graph", params={"clock": "acquisition_wall_clock"}).json()
        self.assertEqual(next(o["state"]["status"] for o in graph["nodes"] if o["object_id"] == "WO023"), "released")

    def test_source_ingest_really_updates_semantics_and_graph(self):
        counts = self.rt.store.counts()
        response = self.client.post("/api/v1/objects/ingest", json=self.signal())
        self.assertEqual(response.status_code, 200, response.text)
        check_objects(response.json())
        self.assertEqual(self.rt.store.counts()["processed_messages"], counts["processed_messages"] + 1)
        self.assertEqual(self.rt.manager.current_second, 1)
        self.assertEqual(self.rt.graph_store.counts()["model_events"], self.rt.store.counts()["semantic_state_history"])

    def test_raw_batch_revalidated_and_replay_is_idempotent(self):
        batch = [r for r in self.raw if r["raw_context"].get("sim_time_s") == 1]
        first = self.client.post("/api/v1/objects/ingest", json=batch)
        self.assertEqual(first.status_code, 200, first.text)
        before = self.rt.store.counts(), self.rt.graph_store.counts()
        second = self.client.post("/api/v1/objects/ingest", json=batch)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(before, (self.rt.store.counts(), self.rt.graph_store.counts()))

    def test_raw_decoded_value_or_resolved_id_forgery_rejected(self):
        raw = deepcopy(next(r for r in self.raw if r["modality"] == "timeseries_signal"))
        for mutate in ("value", "identity", "message_id"):
            bad = deepcopy(raw)
            if mutate == "value":
                bad["raw_payload"]["temperature_c"] = 999
            elif mutate == "identity":
                bad["raw_context"]["_ingest"]["resolved_ids"]["object_id"] = "QC_01"
            else:
                bad["message_id"] = "FORGED"
            response = self.client.post("/api/v1/objects/ingest", json=bad)
            self.assertEqual(response.status_code, 422, response.text)

    def test_seven_dirty_frames_and_images_rejected_with_diagnostics(self):
        before = self.rt.store.counts(), self.rt.graph_store.counts()
        for bad in invalid_examples(self.source):
            self.assertEqual(self.client.post("/api/v1/objects/ingest", json=bad).status_code, 422)
        self.assertEqual(before, (self.rt.store.counts(), self.rt.graph_store.counts()))
        self.assertEqual(self.rt.session.summary()["counts"]["rejected"], 7)
        self.assertEqual(len(read_lines(self.workspace / "rejected_messages.jsonl")), 7)

    def test_image_path_escape_and_raw_crc_tampering_rejected(self):
        image = deepcopy(next(r for r in self.records if r["modality"] == "image"))
        image["payload"]["image_ref"] = "../escape.png"
        self.assertEqual(self.client.post("/api/v1/objects/ingest", json=image).status_code, 422)
        frame = deepcopy(next(r for r in self.raw if r["protocol"] == "PLC_FRAME"))
        frame["raw_payload"]["source_payload"]["frame_hex"] = "AA55"
        self.assertEqual(self.client.post("/api/v1/objects/ingest", json=frame).status_code, 422)

    def test_batch_failure_rolls_back_all_three_stages_and_allows_retry(self):
        good = self.signal()
        bad = deepcopy(good)
        bad["event_key"] = "bad-signal"
        bad["payload"].pop("temperature_c")
        counts = self.rt.store.counts(), self.rt.graph_store.counts()
        seen = dict(self.rt.session.seen)
        self.assertEqual(self.client.post("/api/v1/objects/ingest", json=[good, bad]).status_code, 422)
        self.assertEqual(counts, (self.rt.store.counts(), self.rt.graph_store.counts()))
        self.assertEqual(seen, self.rt.session.seen)
        self.assertEqual(self.client.post("/api/v1/objects/ingest", json=good).status_code, 200)

    def test_semantic_failure_rolls_back_connector_identity(self):
        bad = self.signal(0)
        bad["payload"]["temperature_c"] = 999
        before = dict(self.rt.session.seen)
        self.assertEqual(self.client.post("/api/v1/objects/ingest", json=bad).status_code, 422)
        self.assertEqual(self.rt.session.seen, before)
        self.assertEqual(self.client.post("/api/v1/objects/ingest", json=self.signal(0)).status_code, 200)

    def test_graph_failure_rolls_back_semantic_db_and_memory(self):
        before = self.rt.store.counts(), self.rt.manager.snapshot()
        with patch.object(self.rt.manager, "apply_event", side_effect=ValueError("injected graph failure")):
            response = self.client.post("/api/v1/objects/ingest", json=self.signal())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(before, (self.rt.store.counts(), self.rt.manager.snapshot()))

    def test_event_fault_then_recovery_uses_all_services(self):
        first = self.trigger()
        self.assertEqual(first.status_code, 200, first.text)
        a = first.json()
        self.assertEqual(a["pipeline"], ["WP2", "WP3", "WP4"])
        self.assertEqual(a["objects"][0]["state"]["status"], "fault")
        self.assertEqual(len(a["impact"]["active_impacts"]), 1)
        recovered = self.trigger("fault_cleared", 20).json()
        self.assertEqual(recovered["objects"][0]["state"]["status"], "running")
        self.assertEqual(recovered["impact"]["active_impacts"], [])
        self.assertEqual(recovered["impact"]["impact_events"][0]["end_s"], 20)
        self.assertEqual(self.rt.store.counts()["semantic_state_history"], self.rt.graph_store.counts()["model_events"])

    def test_recovery_with_abnormal_signal_does_not_clear_fault(self):
        self.trigger()
        hot = dict(spindle_speed_rpm=4000.0, temperature_c=99.0, vibration_mm_s=10.0, load_ratio=.98)
        response = self.trigger("fault_cleared", 20, signals=hot)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["objects"][0]["state"]["status"], "fault")
        self.assertEqual(len(response.json()["impact"]["active_impacts"]), 1)

    def test_event_idempotency_and_conflicting_event_reuse(self):
        a = self.trigger()
        counts = self.rt.store.counts(), self.rt.graph_store.counts()
        b = self.trigger()
        self.assertEqual(a.json(), b.json())
        self.assertEqual(counts, (self.rt.store.counts(), self.rt.graph_store.counts()))
        self.assertEqual(self.trigger(second=11).status_code, 422)

    def test_completed_device_and_old_online_times_refused(self):
        self.assertEqual(self.trigger(second=180).status_code, 422)
        self.trigger(second=10)
        self.assertEqual(self.trigger("fault_cleared", 5).status_code, 422)
        result = self.full.post("/api/v1/events/trigger", json=dict(event_id="no-finished", kind="fault", object_id="CNC_01", sim_time_s=365))
        self.assertEqual(result.status_code, 422)

    def test_business_updates_respect_order_wall_and_workorder_simulation_clocks(self):
        for identifier, state in (("ORDER_A", "cancelled"), ("WO023", "completed")):
            response = self.client.post("/api/v1/events/trigger", json=dict(event_id=identifier, kind="business_update",
                                        object_id=identifier, sim_time_s=10, status=state))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.client.get("/api/v1/states/" + identifier).json()["state"]["status"], state)
        self.assertFalse(self.rt.manager.relations(source="WO023", relation="maps_to")[0]["context"]["active"])

    def test_compatibility_semantic_write_also_updates_graph(self):
        raw = next(r for r in self.raw if r["modality"] == "timeseries_signal" and r["raw_context"].get("sim_time_s") == 1)
        response = self.client.post("/semantic/object", json=raw)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.rt.manager.current_second, 1)

    def test_concurrent_duplicate_http_ingest_commits_once(self):
        before = self.rt.store.counts()["processed_messages"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post("/api/v1/objects/ingest", json=self.signal()), range(4)))
        self.assertTrue(all(r.status_code == 200 for r in responses))
        self.assertEqual(self.rt.store.counts()["processed_messages"], before + 1)

    def test_reopen_restores_state_and_closes_lifespan_connections(self):
        path = self.root / "reopen_gateway"
        app = create_gateway_app(path, **self.options)
        with TestClient(app) as client:
            response = client.post("/api/v1/objects/ingest", json=self.signal())
            self.assertEqual(response.status_code, 200)
            counts = app.state.runtime.store.counts(), app.state.runtime.graph_store.counts()
        self.assertTrue(app.state.runtime.closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            app.state.runtime.store.counts()
        reopened = create_gateway_app(path)
        with TestClient(reopened) as client:
            self.assertEqual(counts, (reopened.state.runtime.store.counts(), reopened.state.runtime.graph_store.counts()))
            self.assertEqual(client.post("/api/v1/objects/ingest", json=self.signal()).status_code, 200)
            self.assertEqual(counts[0], reopened.state.runtime.store.counts())

    def test_input_and_legacy_workspaces_protected(self):
        for path in [self.output / name for name in ("wp1", "wp2", "wp3", "wp4")] + [PROJECT / "minimal_factory"]:
            with self.assertRaises(ValueError):
                with TestClient(create_gateway_app(path, **self.options)):
                    pass

    def test_invalid_time_enums_extra_fields_empty_batch_and_nan(self):
        self.assertEqual(self.client.get("/api/v1/model/graph", params={"as_of": "nan"}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/model/graph", params={"clock": "mixed"}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/objects/ingest", json=[]).status_code, 422)
        response = self.client.post("/api/v1/events/trigger", json=dict(event_id="bad", kind="fault", object_id="CNC_01", sim_time_s=5, eighth=True))
        self.assertEqual(response.status_code, 422)
        bad = self.signal()
        bad["payload"]["temperature_c"] = float("nan")
        content = json.dumps(bad)
        self.assertEqual(self.client.post("/api/v1/objects/ingest", content=content, headers={"Content-Type": "application/json"}).status_code, 422)

    def test_unknown_identity_and_missing_provenance_rejected(self):
        raw = deepcopy(self.raw[0])
        raw["raw_context"].pop("_ingest")
        for endpoint in ("/api/v1/objects/ingest", "/semantic/object"):
            self.assertEqual(self.client.post(endpoint, json=raw).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/model/impact/UNKNOWN").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/events/trigger", json=dict(event_id="unknown", kind="fault", object_id="UNKNOWN", sim_time_s=10)).status_code, 404)

    def test_offline_dashboard_has_all_pngs_graph_data_and_no_external_assets(self):
        text = (self.output / "e2e_dashboard.html").read_text(encoding="utf-8")
        class Collector(HTMLParser):
            def __init__(self):
                super().__init__()
                self.images, self.external = [], []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "img":
                    self.images.append(attrs["src"])
                if tag in ("script", "link") and (attrs.get("src") or attrs.get("href", "").startswith("http")):
                    self.external.append(attrs)
        doc = Collector()
        doc.feed(text)
        self.assertEqual(len(doc.images), 9)
        self.assertTrue(all(src.startswith("data:image/png;base64,") for src in doc.images))
        self.assertEqual(doc.external, [])
        payload = json.loads(re.search(r'<script id="e2e-data" type="application/json">(.*?)</script>', text, re.S)[1])
        self.assertEqual(payload["summary"], self.summary)
        self.assertEqual([s["as_of"] for s in payload["snapshots"]], [59, 75, 95, 360])

    def test_root_dashboard_route_and_mounted_live_branch(self):
        page = self.full.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn("制造—企业", page.text)
        graph = self.full.get("/demo/api/v1/model/graph")
        self.assertEqual(graph.status_code, 200)
        self.assertEqual(graph.json()["as_of"], 80)

    def test_weekly_five_answers_use_actual_counts_and_defer_docker(self):
        report = (self.output / "weekly_5q_report.md").read_text(encoding="utf-8")
        for label in ("①", "②", "③", "④", "⑤", "2202", "2206", "2215", "360", "Docker化按用户要求暂缓"):
            self.assertIn(label, report)
        self.assertFalse(self.summary["dockerized"])

    def test_subdirectory_cli_entry_and_existing_output_protection(self):
        command = [sys.executable, "-B", "-X", "utf8", "run_e2e_demo.py", "--output", str(self.output)]
        result = subprocess.run(command, cwd=PROJECT / "task2_1_prototype", capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FileExistsError", result.stderr)

    def test_z_all_upstream_generated_artifacts_remain_unchanged(self):
        self.assertEqual([hashes(self.output / name) for name in ("wp1", "wp2", "wp3", "wp4")], self.input_hashes)


if __name__ == "__main__":
    unittest.main()
