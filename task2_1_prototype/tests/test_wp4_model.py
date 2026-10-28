from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import networkx as nx

from task2_1_prototype.contracts.models import SemanticObject, RelationEdge, parse_timestamp, utc_text
from task2_1_prototype.services.relation_service import relation_inference, RelationError
from task2_1_prototype.services.relation_service.relation_engine import VALIDATORS
from task2_1_prototype.services.spatiotemporal_model_service import GraphManager, SQLiteGraphStore, analyze_impact
from task2_1_prototype.services.spatiotemporal_model_service.api import create_app
from task2_1_prototype.services.spatiotemporal_model_service.neo4j_export import export_neo4j
from task2_1_prototype.services.spatiotemporal_model_service.pipeline import run_batch, RULES_FILE
from task2_1_prototype.services.semantic_object_service.pipeline import run_batch as semantic_batch
from task2_1_prototype.services.connector_service.pipeline import run_batch as connector_batch
from task2_1_prototype.simulators.simpy_factory import run_demo


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def hashes(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}


def graph_from(snapshot):
    graph = nx.MultiDiGraph()
    for obj in snapshot["nodes"]:
        graph.add_node(obj["object_id"], semantic=deepcopy(obj))
    for edge in snapshot["edges"]:
        graph.add_edge(edge["source"], edge["target"], key=edge["relation_id"], relation=deepcopy(edge))
    return graph


class Wp4ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.wp1, cls.wp2, cls.wp3 = (cls.root / name for name in ("wp1", "wp2", "wp3"))
        run_demo(cls.wp1)
        connector_batch(cls.wp1, cls.wp2)
        semantic_batch(cls.wp2, cls.wp3)
        cls.input_hashes = [hashes(p) for p in (cls.wp1, cls.wp2, cls.wp3)]
        cls.objects = lines(cls.wp3 / "semantic_objects.jsonl")
        cls.history = lines(cls.wp3 / "semantic_state_history.jsonl")
        cls.rules = json.loads(RULES_FILE.read_text(encoding="utf-8"))
        cls.full_store = SQLiteGraphStore()
        cls.addClassCleanup(cls.full_store.close)
        cls.full = GraphManager(cls.objects, store=cls.full_store, rules=cls.rules)
        cls.full.replay(cls.history)

    def setUp(self):
        self.store = SQLiteGraphStore()
        self.addCleanup(self.store.close)
        self.manager = GraphManager(self.objects, store=self.store, rules=self.rules)

    def milestones(self, end=360):
        return [deepcopy(o) for o in self.history if o["context"].get("sim_time_s") in (0, 60, 90, 180, 300, 360)
                and o["context"]["sim_time_s"] <= end]

    def event(self, object_id, second):
        return deepcopy([o for o in self.history if o["object_id"] == object_id and o["context"].get("sim_time_s") == second][-1])

    def retime(self, event, seconds, suffix):
        event = deepcopy(event)
        event["timestamp"] = utc_text(parse_timestamp(self.manager.epoch) + timedelta(seconds=seconds))
        event["context"]["sim_time_s"] = seconds
        event["context"]["history"]["message_id"] += "/" + suffix
        event["context"]["last_message_id"] = event["context"]["history"]["message_id"]
        return event

    def check_snapshot(self, snapshot):
        identifiers = {n["object_id"] for n in snapshot["nodes"]}
        for obj in snapshot["nodes"]:
            self.assertEqual(len(obj), 7)
            SemanticObject.model_validate(obj)
            VALIDATORS["SemanticObject"].validate(obj)
        for edge in snapshot["edges"]:
            self.assertEqual(set(edge), {"relation_id", "source", "relation", "target", "weight", "timestamp", "context"})
            RelationEdge.model_validate(edge)
            VALIDATORS["RelationEdge"].validate(edge)
            self.assertIn(edge["source"], identifiers)
            self.assertIn(edge["target"], identifiers)

    def test_every_edge_and_node_in_all_four_slices_pass_frozen_contracts(self):
        for second in (59, 75, 95, 360):
            snapshot = self.full.snapshot(second)
            self.assertEqual(len(snapshot["nodes"]), 12)
            self.assertEqual(len(snapshot["edges"]), 19)
            self.assertEqual(set(snapshot["level_counts"]), {"enterprise", "factory", "line", "device"})
            self.check_snapshot(snapshot)

    def test_business_and_organization_chains_and_business_locations(self):
        edges = {(e["source"], e["relation"], e["target"]) for e in self.full.snapshot(75)["edges"]}
        expected = {("CNC_01", "executes", "OP20"), ("QC_01", "executes", "OP30"), ("MOVE_01", "executes", "OP40"),
                    ("WO023", "maps_to", "ORDER_A"), ("LINE_01", "part_of", "FAC_01"), ("FAC_01", "managed_by", "ENT_01"),
                    ("WO023", "located_in", "FAC_01"), ("ORDER_A", "located_in", "ENT_01")}
        expected |= {(p, "belongs_to", "WO023") for p in ("OP20", "OP30", "OP40")}
        expected |= {(e, "located_in", "LINE_01") for e in ("CNC_01", "QC_01", "MOVE_01", "OP20", "OP30", "OP40")}
        self.assertTrue(expected <= edges)

    def test_standalone_function_accepts_model_instances_and_does_not_mutate_input(self):
        original = deepcopy(self.objects)
        edges = relation_inference([SemanticObject.model_validate(o) for o in self.objects], self.rules)
        self.assertEqual(len(edges), 19)
        self.assertEqual(self.objects, original)
        self.assertTrue(all(len(e) == 7 for e in edges))

    def test_relation_id_stable_across_time_and_weight_changes(self):
        before = {e["relation_id"]: e for e in self.full.relations(as_of=75)}
        after = {e["relation_id"]: e for e in self.full.relations(as_of=360)}
        self.assertEqual(set(before), set(after))
        key = next(k for k, e in before.items() if e["source"] == "CNC_01" and e["relation"] == "executes")
        self.assertEqual((before[key]["weight"], after[key]["weight"]), (1.0, 0.0))

    def test_no_bom_evidence_means_no_material_edge(self):
        edges = relation_inference(self.objects)
        self.assertFalse(any(e["relation"] == "uses_resource" for e in edges))
        configured = relation_inference(self.objects, self.rules)
        material = [e for e in configured if e["relation"] == "uses_resource"]
        self.assertEqual(len(material), 1)
        self.assertEqual((material[0]["source"], material[0]["target"]), ("OP20", "RES_MAT_01"))
        self.assertEqual(material[0]["context"]["evidence_source"], "configured_bom_rule")
        self.assertFalse(material[0]["context"]["actual_consumption_asserted"])

    def test_invalid_bom_parent_unknown_target_and_malformed_object_rejected(self):
        rules = deepcopy(self.rules)
        rules["material_bindings"][0]["work_order"] = "WO_OTHER"
        with self.assertRaises(RelationError):
            relation_inference(self.objects, rules)
        with self.assertRaises(RelationError):
            relation_inference([o for o in self.objects if o["object_id"] != "ORDER_A"])
        bad = deepcopy(self.objects)
        bad[0]["eighth_field"] = True
        with self.assertRaises(ValueError):
            relation_inference(bad)
        with self.assertRaises(RelationError):
            relation_inference(self.objects + [self.objects[0]])

    def test_raw_messages_and_nonfinite_context_are_not_semantic_input(self):
        raw = lines(self.wp2 / "raw_ingest_messages.jsonl")[0]
        with self.assertRaises(ValueError):
            relation_inference([raw])
        with self.assertRaises(ValueError):
            relation_inference(self.objects, {"as_of": float("nan")})
        bad = deepcopy(self.objects)
        bad[0]["state"]["bad"] = float("inf")
        with self.assertRaises(ValueError):
            relation_inference(bad)

    def test_half_open_execution_windows_exact_boundaries(self):
        cases = [(0, {"OP20"}), (75, {"OP20"}), (179.999, {"OP20"}), (180, {"OP30"}),
                 (299.999, {"OP30"}), (300, {"OP40"}), (360, set())]
        for second, expected in cases:
            with self.subTest(second=second):
                edges = self.full.relations(relation="executes", as_of=second)
                self.assertEqual({e["target"] for e in edges if e["context"]["active"]}, expected)
                self.assertEqual({e["target"] for e in edges if e["weight"] == 1.0}, expected)
        cnc = self.full.relations(source="CNC_01", relation="executes", as_of=180)[0]
        self.assertEqual(cnc["context"]["phase"], "completed")

    def test_future_nodes_do_not_leak_final_states_into_early_slices(self):
        nodes = {o["object_id"]: o for o in self.full.snapshot(59)["nodes"]}
        for identifier in ("OP30", "OP40", "QC_01", "MOVE_01"):
            self.assertEqual(nodes[identifier]["state"], {"status": "not_started"})
            self.assertFalse(nodes[identifier]["context"]["model_projection"]["dynamic_state_observed"])
        self.assertEqual(nodes["ORDER_A"]["state"]["status"], "reference_only")
        self.assertEqual(nodes["WO023"]["state"]["status"], "running")

    def test_four_fault_slices_and_code_and_risk_statuses(self):
        results = [self.full.impact("CNC_01", second) for second in (59, 75, 95, 360)]
        self.assertEqual([len(r["active_impacts"]) for r in results], [0, 1, 0, 0])
        fault = results[1]["active_impacts"][0]
        self.assertEqual(fault["path"], ["CNC_01", "OP20", "WO023", "ORDER_A"])
        self.assertEqual(fault["fault_codes"], ["0x84"])
        self.assertEqual([step["risk_status"] for step in fault["steps"]], ["fault", "blocked_risk", "delay_risk", "delivery_risk"])
        self.assertEqual(fault["steps"][1]["observed_status"], "running")

    def test_fault_interval_boundaries_and_resolved_audit(self):
        for second, count in ((59.999, 0), (60, 1), (89.999, 1), (90, 0)):
            self.assertEqual(len(self.full.impact("CNC_01", second)["active_impacts"]), count)
        audit = self.full_store.risk_history()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["start_s"], audit[0]["end_s"], audit[0]["status"]), (60, 90, "resolved"))
        self.assertEqual(audit[0]["closure_reason"], "fault_cleared")
        self.assertFalse(audit[0]["mes_schedule_changed"])
        self.assertEqual(self.full.impact("CNC_01", 59)["historical_impacts"], [])
        self.assertEqual(len(self.full.impact("CNC_01", 95)["historical_impacts"]), 1)

    def test_risk_overlay_does_not_modify_semantic_business_states(self):
        snapshot = self.full.snapshot(75)
        before = deepcopy(snapshot["nodes"])
        graph = graph_from(snapshot)
        analyze_impact(graph, "CNC_01")
        self.assertEqual([graph.nodes[o["object_id"]]["semantic"] for o in before], before)
        nodes = {o["object_id"]: o for o in snapshot["nodes"]}
        self.assertEqual(nodes["OP20"]["state"]["status"], "running")
        self.assertEqual(nodes["WO023"]["state"]["status"], "running")
        self.assertNotIn("risk_status", nodes["ORDER_A"]["state"])

    def test_new_device_fault_after_completion_cannot_reactivate_finished_chain(self):
        graph = graph_from(self.full.snapshot(360))
        graph.nodes["CNC_01"]["semantic"]["state"].update(status="fault", fault_latched=True)
        self.assertEqual(analyze_impact(graph, "CNC_01")["active_impacts"], [])

    def test_order_and_work_order_downstream_dependency_completeness(self):
        expected = {"WO023", "OP20", "OP30", "OP40", "CNC_01", "QC_01", "MOVE_01", "RES_MAT_01", "LINE_01", "FAC_01", "ENT_01"}
        for identifier in ("ORDER_A", "WO023"):
            response = self.full.impact(identifier, 75)
            self.assertEqual(response["direction"], "downstream")
            self.assertTrue(expected <= set(response["dependencies"]["object_ids"]))
        completed = self.full.impact("ORDER_A", 360)
        self.assertIn("CNC_01", completed["dependencies"]["object_ids"])

    def test_artificial_cycle_is_detected_and_traversals_terminate(self):
        graph = graph_from(self.full.snapshot(75))
        original = next(e for e in self.full.snapshot(75)["edges"] if e["relation"] == "maps_to")
        cycle = deepcopy(original)
        cycle.update(relation_id="MANUAL_CYCLE", target="OP20")
        graph.add_edge("WO023", "OP20", key=cycle["relation_id"], relation=cycle)
        result = analyze_impact(graph, "CNC_01")
        self.assertEqual(len(result["active_impacts"]), 1)
        self.assertTrue(result["cycles_detected"])
        reverse = analyze_impact(graph, "ORDER_A", "downstream")
        self.assertLessEqual(len(reverse["dependencies"]["object_ids"]), len(graph))

    def test_multi_work_order_process_instances_never_cross(self):
        objects = deepcopy(self.full.snapshot(75)["nodes"])
        by_id = {o["object_id"]: o for o in objects}
        for source, new in (("ORDER_A", "ORDER_B"), ("WO023", "WO024"), ("CNC_01", "CNC_02"), ("OP20", "OP20_2")):
            obj = deepcopy(by_id[source])
            obj["object_id"] = new
            obj["context"].update(order="ORDER_B", work_order="WO024", equipment="CNC_02", process="OP20_2", process_instance_key=["WO024", "OP20_2"])
            objects.append(obj)
        edges = relation_inference(objects, {"timestamp": self.full.snapshot(75)["timestamp"]})
        pairs = {(e["source"], e["relation"], e["target"]) for e in edges}
        self.assertIn(("OP20_2", "belongs_to", "WO024"), pairs)
        self.assertIn(("CNC_02", "executes", "OP20_2"), pairs)
        self.assertIn(("WO024", "maps_to", "ORDER_B"), pairs)
        self.assertNotIn(("OP20_2", "belongs_to", "WO023"), pairs)
        self.assertNotIn(("CNC_01", "executes", "OP20_2"), pairs)
        objects[-1]["context"]["process_instance_key"] = ["WO023", "OP20_2"]
        with self.assertRaises(RelationError):
            relation_inference(objects)

    def test_late_flag_and_unflagged_old_event_do_not_roll_back_current_graph(self):
        self.manager.replay(self.milestones(90))
        before = self.manager.snapshot()
        for explicit_late in (True, False):
            late = self.retime(self.event("CNC_01", 60), 65, "late" + str(explicit_late))
            late["context"]["history"]["late"] = explicit_late
            self.manager.apply_event(late)
            after = self.manager.snapshot()
            self.assertEqual(after["nodes"], before["nodes"])
            self.assertEqual(after["edges"], before["edges"])
        self.assertEqual(len(self.store.risk_history()), 1)
        self.assertEqual(self.store.risk_history()[0]["end_s"], 90)

    def test_acquisition_wall_clock_is_separate_from_simulation(self):
        sim = {o["object_id"]: o for o in self.full.snapshot(360)["nodes"]}
        wall = {o["object_id"]: o for o in self.full.snapshot(clock="acquisition_wall_clock")["nodes"]}
        self.assertEqual(sim["WO023"]["state"]["status"], "completed")
        self.assertEqual(wall["WO023"]["state"]["status"], "released")
        self.assertEqual(wall["ORDER_A"]["state"]["status"], "dispatched")
        self.assertEqual(wall["CNC_01"]["state"]["status"], "not_started")
        self.assertEqual(self.full.impact("CNC_01", clock="acquisition_wall_clock")["active_impacts"], [])

    def test_incremental_replay_and_stable_event_idempotency(self):
        records = self.milestones(90)
        self.assertEqual(self.manager.replay(records), len(records))
        counts = self.store.counts()
        audit = self.store.risk_history()
        self.assertEqual(self.manager.replay(records), 0)
        self.assertEqual(self.store.counts(), counts)
        self.assertEqual(self.store.risk_history(), audit)
        self.assertIsInstance(self.manager.graph, nx.MultiDiGraph)

    def test_concurrent_duplicate_event_is_persisted_once(self):
        event = self.event("WO023", 0)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.manager.apply_event, [event] * 12))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.counts()["model_events"], 1)

    def test_transaction_rollback_keeps_sqlite_and_memory_consistent(self):
        self.manager.replay(self.milestones(90))
        before, counts = self.manager.snapshot(), self.store.counts()
        bad = self.retime(self.event("OP20", 0), 100, "missing-equipment")
        bad["context"]["equipment"] = "NONEXISTENT"
        with self.assertRaises(RelationError):
            self.manager.apply_event(bad)
        self.assertEqual(self.store.counts(), counts)
        self.assertEqual(self.manager.snapshot(), before)

    def test_database_reopen_preserves_snapshots_risk_audit_and_current_state(self):
        path = self.root / "reopen" / "spatiotemporal_model.sqlite3"
        store = SQLiteGraphStore(path)
        try:
            manager = GraphManager(self.objects, store=store, rules=self.rules)
            manager.replay(self.milestones())
            for t in (59, 75, 95, 360):
                manager.snapshot(t, save=True)
            original = manager.snapshot()
            counts = store.counts()
        finally:
            store.close()
        store = SQLiteGraphStore(path)
        try:
            manager = GraphManager(store=store)
            self.assertEqual(manager.snapshot(), original)
            self.assertEqual(store.counts(), counts)
            self.assertEqual(len(store.snapshots()), 4)
            self.assertEqual(store.risk_history()[0]["end_s"], 90)
            self.assertEqual(manager.replay(self.milestones()), 0)
        finally:
            store.close()

    def test_neo4j_export_parameterized_and_offline(self):
        snapshot = self.full.snapshot(75)
        exported = export_neo4j(snapshot)
        self.assertFalse(exported["neo4j_connected"])
        self.assertEqual(len(exported["statements"]), 8)
        self.assertEqual(len(exported["statements"][0]["parameters"]["nodes"]), 12)
        for entry in exported["statements"]:
            self.assertTrue("$nodes" in entry["statement"] or "$edges" in entry["statement"])
            for name in ("CNC_01", "ORDER_A", "0x84"):
                self.assertNotIn(name, entry["statement"])
        snapshot["edges"][0]["target"] = "MISSING"
        with self.assertRaises(ValueError):
            export_neo4j(snapshot)

    def test_fastapi_relation_filters_and_inference_contract(self):
        with TestClient(create_app(self.full)) as client:
            response = client.get("/relations", params={"source": "CNC_01", "relation": "executes", "active": True, "as_of": 75})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()), 1)
            self.assertEqual(len(response.json()[0]), 7)
            self.assertEqual(client.get("/relations", params={"source": "CNC_01", "relation": "executes", "active": True, "as_of": 360}).json(), [])
            response = client.post("/relations/infer", json={"objects": self.objects, "context": self.rules})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()), 19)
            self.assertEqual(client.post("/relations/infer", json={"objects": [{"payload": {}}]}).status_code, 422)

    def test_fastapi_graph_impact_snapshots_and_invalid_queries(self):
        for t in (59, 75, 95, 360):
            self.full.snapshot(t, save=True)
        with TestClient(create_app(self.full)) as client:
            for t, expected in ((59, 0), (75, 1), (95, 0), (360, 0)):
                result = client.get("/model/impact/CNC_01", params={"as_of": t}).json()
                self.assertEqual(len(result["active_impacts"]), expected)
            self.assertEqual(len(client.get("/model/snapshots").json()), 4)
            graph = client.get("/model/graph", params={"as_of": 75})
            self.assertEqual(graph.status_code, 200)
            self.check_snapshot(graph.json())
            self.assertEqual(client.get("/model/impact/missing").status_code, 404)
            self.assertEqual(client.get("/model/graph", params={"as_of": "nan"}).status_code, 422)
            self.assertEqual(client.get("/model/impact/CNC_01", params={"direction": "bad"}).status_code, 422)

    def test_protected_source_directories_and_bad_times(self):
        for folder in (self.wp1, self.wp2, self.wp3):
            with self.assertRaises(ValueError):
                SQLiteGraphStore(folder / "spatiotemporal_model.sqlite3")
        for time in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.full.snapshot(time)
        with self.assertRaises(ValueError):
            self.full.snapshot(75, clock="mixed")

    def test_cli_batch_outputs_and_duplicate_event_replay(self):
        output = self.root / "wp4"
        summary = run_batch(self.wp3, output, repeat=2)
        self.assertEqual((summary["nodes"], summary["edges"]), (12, 19))
        self.assertEqual((summary["accepted_events"], summary["duplicate_events"]), (2215, 2215))
        self.assertEqual(summary["checkpoint_active_impacts"], {"59.0": 0, "75.0": 1, "95.0": 0, "360.0": 0})
        self.assertEqual(len(summary["risk_episodes"]), 1)
        snapshots = json.loads((output / "graph_snapshots.json").read_text(encoding="utf-8"))
        for snapshot in snapshots:
            self.check_snapshot(snapshot)
        self.assertEqual(len(lines(output / "relations.jsonl")), 19)
        self.assertTrue((output / "spatiotemporal_model.sqlite3").is_file())
        with self.assertRaises(FileExistsError):
            run_batch(self.wp3, output)
        with self.assertRaises(ValueError):
            run_batch(self.wp3, self.wp3 / "nested")

    def test_z_wp1_wp2_wp3_source_artifacts_unchanged(self):
        self.assertEqual([hashes(p) for p in (self.wp1, self.wp2, self.wp3)], self.input_hashes)


if __name__ == "__main__":
    unittest.main()
