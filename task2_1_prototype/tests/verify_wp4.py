"""Reproduce WP4 acceptance against the real WP3 batch and anchored historical hashes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from fastapi.testclient import TestClient

from task2_1_prototype.contracts.models import RelationEdge, SemanticObject
from task2_1_prototype.services.relation_service.relation_engine import VALIDATORS
from task2_1_prototype.services.spatiotemporal_model_service import GraphManager, SQLiteGraphStore
from task2_1_prototype.services.spatiotemporal_model_service.api import create_app

PROJECT = Path(__file__).parents[2]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes(folder):
    return {p.relative_to(folder).as_posix(): digest(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wp1", required=True)
    parser.add_argument("--wp2", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence-prefix", required=True)
    args = parser.parse_args()
    wp1, wp2, wp3, output, prefix = map(Path, (args.wp1, args.wp2, args.input, args.output, args.evidence_prefix))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    original = PROJECT / "minimal_factory/data/erp.db"

    def inputs():
        return {"wp1": hashes(wp1), "wp2": hashes(wp2), "wp3": hashes(wp3), "original_erp": digest(original).upper()}

    before = inputs()
    assert before["original_erp"] == "D98A81A49CB950AC8DF38B4B164856D48295E10EDF44BE564A080048D4F0424A"
    historical = json.loads((PROJECT / "md/agent_reports/evidence/2026-10-28_wp3_acceptance.json").read_text(encoding="utf-8"))
    for key in ("wp1", "wp2", "original_erp"):
        assert before[key] == historical["input_hashes_after"][key], f"historical {key} mismatch"
    assert before["wp3"] == historical["artifact_hashes"], "historical WP3 mismatch"
    evidence = {"executed_at_utc": datetime.now(timezone.utc).isoformat(), "commands": [],
                "historical_input_hashes_match_wp3_acceptance": True, "input_hashes_before": before}

    def execute(name, arguments, expected_tests=None, legacy=False):
        command = [sys.executable, "-B", "-X", "utf8", *arguments]
        env = os.environ.copy()
        if legacy:
            env["PYTHONPATH"] = str(PROJECT / "minimal_factory")
        result = subprocess.run(command, cwd=PROJECT, env=env, encoding="utf-8", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        log = prefix.with_name(prefix.name + "_" + name + ".txt")
        log.write_text(result.stdout, encoding="utf-8")
        item = dict(name=name, argv=command, exit_code=result.returncode, log=str(log))
        if legacy:
            item["PYTHONPATH"] = env["PYTHONPATH"]
        if expected_tests:
            match = re.search(r"Ran (\d+) tests in ([\d.]+)s", result.stdout)
            item["tests"] = int(match[1]) if match else None
            item["seconds"] = float(match[2]) if match else None
            assert item["tests"] == expected_tests, result.stdout
        evidence["commands"].append(item)
        print(f'{name}: exit={result.returncode}, tests={item.get("tests", "n/a")}', flush=True)
        if result.returncode:
            raise RuntimeError(f"acceptance failed; see {log}")

    for package, expected in (("wp4", 28), ("wp3", 33), ("wp2", 29), ("wp1", 24)):
        execute(package + "_tests", ["-m", "unittest", "discover", "-s", "task2_1_prototype/tests", "-p", f"test_{package}*.py", "-v"], expected)
    execute("legacy_regression", ["-m", "unittest", "discover", "-s", "minimal_factory/tests", "-v"], 44, legacy=True)
    execute("cli", ["-m", "task2_1_prototype.services.spatiotemporal_model_service.pipeline", "--input", str(wp3),
                    "--output", str(output), "--repeat", "2"])
    summary = json.loads((output / "spatiotemporal_summary.json").read_text(encoding="utf-8"))
    assert summary["accepted_events"] == summary["duplicate_events"] == summary["input_history_records"] == 2215
    assert (summary["nodes"], summary["edges"]) == (12, 19)
    assert summary["level_counts"] == {"device": 3, "enterprise": 2, "factory": 3, "line": 4}
    assert summary["object_type_counts"] == {"equipment": 3, "resource": 4, "process": 3, "order": 1, "work_order": 1}
    assert summary["edge_types"] == {"executes": 3, "belongs_to": 3, "maps_to": 1, "located_in": 9,
                                      "part_of": 1, "managed_by": 1, "uses_resource": 1}
    snapshots = json.loads((output / "graph_snapshots.json").read_text(encoding="utf-8"))
    assert [s["as_of"] for s in snapshots] == [59, 75, 95, 360]
    validated = {"nodes": 0, "edges": 0, "exported_relations": 0}
    samples = []
    for snapshot in snapshots:
        identifiers = {o["object_id"] for o in snapshot["nodes"]}
        for obj in snapshot["nodes"]:
            assert len(obj) == 7
            SemanticObject.model_validate(obj)
            VALIDATORS["SemanticObject"].validate(obj)
            validated["nodes"] += 1
        for edge in snapshot["edges"]:
            assert len(edge) == 7 and edge["source"] in identifiers and edge["target"] in identifiers
            RelationEdge.model_validate(edge)
            VALIDATORS["RelationEdge"].validate(edge)
            validated["edges"] += 1
        nodes = {o["object_id"]: o for o in snapshot["nodes"]}
        samples.append({"as_of": snapshot["as_of"], "statuses": {key: obj["state"].get("status") for key, obj in nodes.items()},
                        "active_execution": [{"source": e["source"], "target": e["target"]} for e in snapshot["edges"]
                                             if e["relation"] == "executes" and e["context"]["active"]],
                        "risk_paths": [r["path"] for r in snapshot["risk_overlays"]]})
    relations = [json.loads(line) for line in (output / "relations.jsonl").read_text(encoding="utf-8").splitlines()]
    for edge in relations:
        assert len(edge) == 7 and edge["source"] in identifiers and edge["target"] in identifiers
        RelationEdge.model_validate(edge)
        VALIDATORS["RelationEdge"].validate(edge)
        validated["exported_relations"] += 1
    report = json.loads((output / "impact_report.json").read_text(encoding="utf-8"))
    assert [len(c["active_impacts"]) for c in report["checkpoints"]] == [0, 1, 0, 0]
    risk = report["checkpoints"][1]["active_impacts"][0]
    assert risk["path"] == ["CNC_01", "OP20", "WO023", "ORDER_A"] and risk["fault_codes"] == ["0x84"]
    assert len(report["risk_audit"]) == 1
    audit = report["risk_audit"][0]
    assert (audit["start_s"], audit["end_s"], audit["status"], audit["closure_reason"]) == (60, 90, "resolved", "fault_cleared")
    assert report["mes_schedule_changed"] is False
    assert set(report["order_dependencies_at_75"]["dependencies"]["object_ids"]) == identifiers
    assert samples[-1]["active_execution"] == []
    for oid in ("OP20", "OP30", "OP40", "WO023"):
        assert samples[-1]["statuses"][oid] == "completed"
    store = SQLiteGraphStore(output / "spatiotemporal_model.sqlite3")
    try:
        manager = GraphManager(store=store)
        evidence["reopened_database_counts"] = store.counts()
        assert store.counts() == {"semantic_catalog": 12, "model_events": 2215, "relation_edges": 19, "graph_snapshots": 4, "impact_events": 1}
        assert store.risk_history()[0]["end_s"] == 90
        with TestClient(create_app(manager)) as client:
            evidence["api_checks"] = []
            for second, count in ((59, 0), (75, 1), (95, 0), (360, 0)):
                response = client.get("/model/impact/CNC_01", params={"as_of": second})
                assert response.status_code == 200 and len(response.json()["active_impacts"]) == count
                evidence["api_checks"].append({"path": f"/model/impact/CNC_01?as_of={second}", "status_code": response.status_code, "active_impacts": count})
    finally:
        store.close()
    after = inputs()
    assert before == after, "ERP/WP1/WP2/WP3 input files changed"
    evidence.update(all_inputs_unchanged=True, input_hashes_after=after, artifact_hashes=hashes(output),
                    spatiotemporal_summary=summary, independently_double_validated=validated,
                    checkpoint_samples=samples, total_tests=sum(c.get("tests", 0) for c in evidence["commands"]))
    shutil.copyfile(output / "relations.jsonl", prefix.with_name(prefix.name + "_relations.jsonl"))
    prefix.with_name(prefix.name + "_checkpoints.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prefix.with_name(prefix.name + "_acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("WP4: 158 tests, 12 nodes, 19 edges, risk [60,90); ERP and all 34 historical files unchanged", flush=True)


if __name__ == "__main__":
    main()
