"""Run WP3, WP2, WP1, legacy suites and the real WP2->WP3 CLI; preserve acceptance evidence."""
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

from task2_1_prototype.services.semantic_object_service.models import validate_object
from task2_1_prototype.services.semantic_object_service.storage import SQLiteStore

PROJECT = Path(__file__).parents[2]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes(folder):
    return {p.relative_to(folder).as_posix(): digest(p) for p in folder.rglob("*") if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wp1", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence-prefix", required=True)
    args = parser.parse_args()
    wp1, wp2, output, prefix = map(Path, (args.wp1, args.input, args.output, args.evidence_prefix))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    original = PROJECT / "minimal_factory/data/erp.db"
    before = {"wp1": hashes(wp1), "wp2": hashes(wp2), "original_erp": digest(original).upper()}
    assert before["original_erp"] == "D98A81A49CB950AC8DF38B4B164856D48295E10EDF44BE564A080048D4F0424A"
    # Historical evidence anchors the existing input snapshots, not only this run's before/after comparison.
    historical_path = PROJECT / "md/agent_reports/evidence/2026-10-28_wp2_acceptance.json"
    historical = json.loads(historical_path.read_text(encoding="utf-8"))
    assert before["wp1"] == historical["source_files_sha256"]
    assert before["wp2"] == historical["artifact_hashes"]
    evidence = {"executed_at_utc": datetime.now(timezone.utc).isoformat(), "commands": [],
                "historical_input_hashes_match_wp2_acceptance": True, "input_hashes_before": before}

    def execute(name, arguments, expected_tests=None, legacy=False):
        command = [sys.executable, "-B", "-X", "utf8", *arguments]
        env = os.environ.copy()
        if legacy:
            env["PYTHONPATH"] = str(PROJECT / "minimal_factory")
        result = subprocess.run(command, cwd=PROJECT, env=env, encoding="utf-8", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
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
            print(result.stdout)
            raise RuntimeError(f"acceptance failed; see {log}")

    for package, expected in (("wp3", 33), ("wp2", 29), ("wp1", 24)):
        execute(package + "_tests", ["-m", "unittest", "discover", "-s", "task2_1_prototype/tests", "-p", f"test_{package}*.py", "-v"], expected)
    execute("legacy_regression", ["-m", "unittest", "discover", "-s", "minimal_factory/tests", "-v"], 44, legacy=True)
    execute("cli", ["-m", "task2_1_prototype.services.semantic_object_service.pipeline", "--input", str(wp2),
                    "--output", str(output), "--repeat", "2"])
    summary = json.loads((output / "semantic_summary.json").read_text(encoding="utf-8"))
    assert summary["input_messages"] == summary["processed_messages"] == summary["duplicate_deliveries"] == 2206
    assert summary["signal_cycles"] == summary["complete_cycles"] == summary["cycles_with_all_three_protocols"] == 360
    assert summary["objects_by_type"] == {"equipment": 3, "process": 3, "work_order": 1, "order": 1, "resource": 4}
    assert len(summary["objects_by_level"]) == 4
    assert [(i["object_id"], i["start_s"], i["end_s"]) for i in summary["fault_intervals"]] == [("CNC_01", 60, 90)]
    evidence["semantic_summary"] = summary
    for name in ("semantic_objects", "semantic_state_history"):
        with (output / (name + ".jsonl")).open(encoding="utf-8") as stream:
            rows = [validate_object(json.loads(line)) for line in stream]
        assert len(rows) == summary[name]
        evidence[name + "_independently_double_validated"] = len(rows)
    # Reopen the persistent store and verify query visibility, not merely exported files.
    store = SQLiteStore(output / "semantic_objects.sqlite3")
    try:
        evidence["reopened_database_counts"] = store.counts()
        cnc = store.latest("CNC_01")
        assert cnc["state"]["fault_count"] == cnc["state"]["recovery_count"] == 1
        assert not cnc["state"]["fault_latched"]
        events = {}
        for item in store.query(history=True, object_id="CNC_01"):
            if item["context"]["sim_time_s"] in (59, 60, 89, 90):
                events[item["context"]["sim_time_s"]] = item
        assert [events[t]["state"]["status"] for t in (59, 60, 89, 90)] == ["running", "fault", "fault", "running"]
        prefix.with_name(prefix.name + "_fault_samples.json").write_text(json.dumps(list(events.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        store.close()
    after = {"wp1": hashes(wp1), "wp2": hashes(wp2), "original_erp": digest(original).upper()}
    assert before == after, "existing ERP/WP1/WP2 inputs changed"
    evidence["all_inputs_unchanged"] = True
    evidence["input_hashes_after"] = after
    evidence["artifact_hashes"] = hashes(output)
    shutil.copyfile(output / "semantic_objects.jsonl", prefix.with_name(prefix.name + "_objects.jsonl"))
    prefix.with_name(prefix.name + "_acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("WP3: 12 objects, 360 cycles, fault [60,90), 2206 processed; original ERP/WP1/WP2 hashes unchanged", flush=True)


if __name__ == "__main__":
    main()
