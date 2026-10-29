"""Six-suite local acceptance, fresh root E2E CLI, and anchored WP1-WP4 hash preservation."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from PIL import Image

from task2_1_prototype.contracts.models import RawIngestMessage, SemanticObject, RelationEdge
from task2_1_prototype.services.connector_service.validation import PROJECT
from task2_1_prototype.services.semantic_object_service.models import VALIDATORS as OBJECT_VALIDATORS
from task2_1_prototype.services.relation_service.relation_engine import VALIDATORS as RELATION_VALIDATORS


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes(folder):
    return {p.relative_to(folder).as_posix(): digest(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence-prefix", default="md/agent_reports/evidence/2026-10-28_wp5")
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT / output
    prefix = Path(args.evidence_prefix)
    if not prefix.is_absolute():
        prefix = PROJECT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    artifacts = PROJECT / "task2_1_prototype/artifacts"
    protected = {"wp1": artifacts / "wp1_smoke_20261028", "wp2": artifacts / "wp2_smoke_20261028",
                 "wp3": artifacts / "wp3_smoke_20261028", "wp4": artifacts / "wp4_acceptance_20261028"}

    def input_hashes():
        return {**{key: hashes(path) for key, path in protected.items()},
                "original_erp": digest(PROJECT / "minimal_factory/data/erp.db").upper()}

    before = input_hashes()
    prior = json.loads((PROJECT / "md/agent_reports/evidence/2026-10-28_wp4_acceptance.json").read_text(encoding="utf-8"))
    for name in ("wp1", "wp2", "wp3", "original_erp"):
        assert before[name] == prior["input_hashes_after"][name], f"historical {name} mismatch"
    assert before["wp4"] == prior["artifact_hashes"], "historical WP4 mismatch"
    assert before["original_erp"] == "D98A81A49CB950AC8DF38B4B164856D48295E10EDF44BE564A080048D4F0424A"
    evidence = dict(executed_at=datetime.now(timezone(timedelta(hours=8))).isoformat(), commands=[], input_hashes_before=before,
                    historical_hashes_match_wp4_acceptance=True)

    def execute(name, arguments, tests=None, legacy=False):
        argv = [sys.executable, "-B", "-X", "utf8", *arguments]
        env = os.environ.copy()
        if legacy:
            env["PYTHONPATH"] = str(PROJECT / "minimal_factory")
        result = subprocess.run(argv, cwd=PROJECT, env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=600)
        log = prefix.with_name(prefix.name + "_" + name + ".txt")
        log.write_text(result.stdout, encoding="utf-8")
        entry = dict(name=name, argv=argv, exit_code=result.returncode, log=str(log.relative_to(PROJECT)))
        if legacy:
            entry["PYTHONPATH"] = env["PYTHONPATH"]
        if tests:
            match = re.search(r"Ran (\d+) tests in ([\d.]+)s", result.stdout)
            entry.update(tests=int(match[1]) if match else None, seconds=float(match[2]) if match else None)
            assert entry["tests"] == tests, result.stdout
        evidence["commands"].append(entry)
        print(f'{name}: exit={result.returncode}, tests={entry.get("tests", "n/a")}', flush=True)
        if result.returncode:
            raise RuntimeError(f"acceptance failed; see {log}")

    for wp, count in (("wp5", 31), ("wp4", 28), ("wp3", 33), ("wp2", 29), ("wp1", 24)):
        execute(wp + "_tests", ["-m", "unittest", "discover", "-s", "task2_1_prototype/tests", "-p", f"test_{wp}*.py", "-v"], count)
    execute("legacy_regression", ["-m", "unittest", "discover", "-s", "minimal_factory/tests", "-v"], 44, legacy=True)
    execute("e2e_cli", ["run_e2e_demo.py", "--output", str(output)])
    summary = json.loads((output / "e2e_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed" and summary["dockerized"] is False
    assert summary["api_standard_routes"] == 10 and len(summary["api_checks"]) == 38
    assert [c["active_impacts"] for c in summary["checkpoints"]] == [0, 1, 0, 0]
    assert [c["status"] for c in summary["online_event_summary"]] == ["fault", "running"]
    validated = {}
    for file, model, validator in (
            ("wp2/raw_ingest_messages.jsonl", RawIngestMessage, OBJECT_VALIDATORS["RawIngestMessage"]),
            ("wp3/semantic_objects.jsonl", SemanticObject, OBJECT_VALIDATORS["SemanticObject"]),
            ("wp3/semantic_state_history.jsonl", SemanticObject, OBJECT_VALIDATORS["SemanticObject"]),
            ("wp4/relations.jsonl", RelationEdge, RELATION_VALIDATORS["RelationEdge"])):
        rows = [json.loads(line) for line in (output / file).read_text(encoding="utf-8").splitlines()]
        for row in rows:
            model.model_validate(row)
            validator.validate(row)
            assert len(row) == (8 if model is RawIngestMessage else 7)
        validated[file] = len(rows)
    snapshots = json.loads((output / "wp4/graph_snapshots.json").read_text(encoding="utf-8"))
    for snapshot in snapshots:
        ids = {node["object_id"] for node in snapshot["nodes"]}
        for node in snapshot["nodes"]:
            SemanticObject.model_validate(node)
            OBJECT_VALIDATORS["SemanticObject"].validate(node)
        for edge in snapshot["edges"]:
            RelationEdge.model_validate(edge)
            RELATION_VALIDATORS["RelationEdge"].validate(edge)
            assert edge["source"] in ids and edge["target"] in ids
    dashboard = (output / "e2e_dashboard.html").read_text(encoding="utf-8")
    embedded = json.loads(re.search(r'<script id="e2e-data" type="application/json">(.*?)</script>', dashboard, re.S)[1])
    assert embedded == {"summary": summary, "snapshots": snapshots}
    images = re.findall(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', dashboard)
    assert len(images) == summary["image_count"] == 9
    for image in images:
        with Image.open(BytesIO(base64.b64decode(image))) as decoded:
            decoded.verify()
    after = input_hashes()
    assert before == after, "original ERP or historical WP1-WP4 artifacts changed"
    evidence.update(input_hashes_after=after, all_inputs_unchanged=True, preserved_artifact_files=sum(len(after[k]) for k in protected),
                    total_tests=sum(c.get("tests", 0) for c in evidence["commands"]), independent_contract_validation=validated,
                    dashboard_data_matches_outputs=True, dashboard_decodable_pngs=len(images), summary=summary,
                    output_root=str(output), artifact_hashes=hashes(output))
    for name, suffix in (("e2e_summary.json", "e2e_summary.json"), ("weekly_5q_report.md", "weekly_5q_report.md"),
                         ("online_event_evidence.json", "online_events.json")):
        shutil.copyfile(output / name, prefix.with_name(prefix.name + "_" + suffix))
    prefix.with_name(prefix.name + "_acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f'WP5: {evidence["total_tests"]} tests passed; {evidence["preserved_artifact_files"]} historical files and original ERP unchanged; 10 standard routes verified.', flush=True)


if __name__ == "__main__":
    main()
