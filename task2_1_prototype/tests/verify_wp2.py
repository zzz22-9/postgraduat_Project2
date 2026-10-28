"""Reproducible WP2 acceptance: suites, CLI, immutable source hashes, exported evidence.

Run from the project root with -m task2_1_prototype.tests.verify_wp2.
The output directory must be new. Evidence files use the supplied prefix.
"""
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

from task2_1_prototype.contracts import RawIngestMessage
from task2_1_prototype.services.connector_service.validation import PROJECT, SCHEMA
from jsonschema import Draft202012Validator, FormatChecker


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence-prefix", required=True)
    args = parser.parse_args()
    source, output = Path(args.input), Path(args.output)
    prefix = Path(args.evidence_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"executed_at_utc": datetime.now(timezone.utc).isoformat(), "commands": []}
    original = PROJECT / "minimal_factory/data/erp.db"
    before = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*") if p.is_file()}
    evidence["original_erp_sha256_before"] = digest(original).upper()
    assert evidence["original_erp_sha256_before"] == "D98A81A49CB950AC8DF38B4B164856D48295E10EDF44BE564A080048D4F0424A"

    def execute(name, arguments, expected_tests=None, legacy=False):
        command = [sys.executable, "-B", "-X", "utf8", *arguments]
        env = os.environ.copy()
        if legacy:
            env["PYTHONPATH"] = str(PROJECT / "minimal_factory")
        result = subprocess.run(command, cwd=PROJECT, env=env, text=True, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        log = prefix.with_name(prefix.name + "_" + name + ".txt")
        log.write_text(result.stdout, encoding="utf-8")
        record = {"name": name, "argv": command, "exit_code": result.returncode, "log": str(log)}
        if legacy:
            record["PYTHONPATH"] = env["PYTHONPATH"]
        if expected_tests:
            match = re.search(r"Ran (\d+) tests in ([\d.]+)s", result.stdout)
            record["tests"] = int(match[1]) if match else None
            record["seconds"] = float(match[2]) if match else None
            assert record["tests"] == expected_tests, result.stdout
        evidence["commands"].append(record)
        print(f'{name}: exit={result.returncode}, tests={record.get("tests", "n/a")}', flush=True)
        if result.returncode:
            print(result.stdout)
            raise RuntimeError(f"{name} failed; see {log}")

    execute("tests", ["-m", "unittest", "discover", "-s", "task2_1_prototype/tests", "-p", "test_wp2*.py", "-v"], 29)
    execute("wp1_regression", ["-m", "unittest", "discover", "-s", "task2_1_prototype/tests", "-p", "test_wp1*.py", "-v"], 24)
    execute("legacy_regression", ["-m", "unittest", "discover", "-s", "minimal_factory/tests", "-v"], 44, legacy=True)
    execute("cli", ["-m", "task2_1_prototype.services.connector_service.pipeline", "--input", str(source),
                    "--output", str(output), "--exercise-invalid", "--repeat", "2"])
    summary = json.loads((output / "connector_summary.json").read_text(encoding="utf-8"))
    assert summary["counts"] == {"accepted": 2206, "duplicates": 2202, "rejected": 7}
    assert summary["source_batches"] == [{"accepted": 2202}, {"duplicates": 2202}]
    evidence["connector_summary"] = summary
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker())
    raw = [json.loads(line) for line in (output / "raw_ingest_messages.jsonl").read_text(encoding="utf-8").splitlines()]
    records = [json.loads(line) for line in (source / "transport_records.jsonl").read_text(encoding="utf-8").splitlines()]
    for message in raw:
        RawIngestMessage.model_validate(message)
        validator.validate(message)
    for record, message in zip(records, raw[:len(records)]):
        assert message["raw_payload"]["source_payload"] == record["payload"]
        assert {k: v for k, v in message["raw_context"].items() if k != "_ingest"} == record["context"]
    after = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*") if p.is_file()}
    assert before == after, "WP1 source files changed"
    evidence["original_erp_sha256_after"] = digest(original).upper()
    assert evidence["original_erp_sha256_after"] == evidence["original_erp_sha256_before"]
    evidence["source_files_unchanged"] = True
    evidence["source_files_sha256"] = before
    evidence["all_messages_double_validated"] = len(raw)
    evidence["source_payloads_and_contexts_preserved"] = len(records)
    evidence["artifact_hashes"] = {p.name: digest(p) for p in output.iterdir() if p.is_file()}
    samples = {}
    for message in raw:
        samples.setdefault(message["protocol"] + "/" + message["modality"], message)
    prefix.with_name(prefix.name + "_samples.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(output / "rejected_messages.jsonl", prefix.with_name(prefix.name + "_rejected.jsonl"))
    prefix.with_name(prefix.name + "_acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Accepted {len(raw)}; original ERP and {len(before)} source files unchanged; evidence: {prefix}", flush=True)


if __name__ == "__main__":
    main()
