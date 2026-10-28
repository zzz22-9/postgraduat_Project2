"""Batch CLI and reusable stream orchestration for WP2 (stops before semantic mapping)."""
import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import struct

from .connectors import DatabaseConnector, MqttConnector, OpcUaAndPlcConnector, RestApiConnector
from .validation import Bindings, IngestSession, PROJECT, Rejected, canonical
from task2_1_prototype.simulators.protocol_emulator import corrupt_frame, crc16, write_json
from task2_1_prototype.contracts.models import utc_text


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)


def load_batch(folder):
    folder = Path(folder).resolve()
    config, registry, snapshot = [strict_json((folder / name).read_text(encoding="utf-8"))
                                  for name in ("config.json", "id_registry.json", "input.json")]
    batch_id = hashlib.sha256(canonical([config, registry, snapshot]).encode()).hexdigest()
    return config, Bindings(config, registry, snapshot), batch_id


def connectors_for(session, database_path):
    rest, db, mqtt, field = (RestApiConnector(session), DatabaseConnector(session, database_path),
                             MqttConnector(session), OpcUaAndPlcConnector(session))
    return {"REST_API": rest, "SQLITE": db, "POSTGRESQL": db,
            "MQTT": mqtt, "OPC_UA": field, "PLC_FRAME": field}


def ingest_stream(records, connectors, session, verify_database=False):
    """Yield RawIngestMessage models in arrival order; diagnostics are available on session."""
    for record in records:
        if not isinstance(record, dict) or record.get("protocol") not in connectors:
            session.reject("Pipeline", Rejected("PROTOCOL", "unknown or missing protocol"), record)
            continue
        connector = connectors[record["protocol"]]
        message = (connector.ingest_verified_row(record) if verify_database and record["protocol"] == "SQLITE"
                   else connector.ingest(record))
        if message is not None:
            yield message


def read_jsonl(path, session):
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                yield strict_json(line)
            except ValueError as exc:
                session.reject("Pipeline", Rejected("JSON_PARSE", str(exc)), line.rstrip(),
                               f"{Path(path).name}:{line_number}")


def supplementary_inputs(config, output, connectors, session):
    database = connectors["SQLITE"]
    captured_at = utc_text(datetime.now(timezone.utc))

    def order_metadata(row):
        return dict(sequence=0, event_key=f'db-snapshot/{captured_at}/orders/{row["order_id"]}', source_system="ERP",
                    modality="structured_record", channel="orders", timestamp=captured_at,
                    context={"order_id": row["order_id"], "db_ref": "demo_erp.sqlite3",
                             "snapshot_kind": "current_database", "timestamp_basis": "acquisition_wall_clock"},
                    object_hint=dict(raw_id=row["order_id"], suggested_type="order",
                                     domain_hint="enterprise", level_hint="enterprise"))
    list(database.snapshot("orders", order_metadata))
    # Organization rows are configuration reference data in a NEW database, not a source DB migration.
    path = output / "organization_reference.sqlite3"
    nodes = [n for n in config["hierarchy"] if n["object_type"] == "resource"]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE organizations (object_id TEXT PRIMARY KEY, domain TEXT, level TEXT, context_json TEXT)")
        connection.executemany("INSERT INTO organizations VALUES (?,?,?,?)",
                               [(n["object_id"], n["domain"], n["level"], canonical(n["context"])) for n in nodes])
        connection.commit()
    organization_db = DatabaseConnector(session, path)

    def organization_metadata(row):
        return dict(sequence=0, event_key=f'config/organizations/{row["object_id"]}', source_system="ERP",
                    modality="structured_record", channel="organizations", timestamp=config["simulation_epoch"],
                    context={**strict_json(row["context_json"]), "db_ref": path.name,
                             "snapshot_kind": "configuration", "timestamp_basis": "simulation_epoch"},
                    object_hint=dict(raw_id=row["object_id"], suggested_type="resource",
                                     domain_hint=row["domain"], level_hint=row["level"]))
    list(organization_db.snapshot("organizations", organization_metadata))


def invalid_examples(folder):
    """Explicit diagnostic fixtures, never mutate the original records or assets."""
    first_plc = first_image = None
    with (folder / "transport_records.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = strict_json(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("protocol") == "PLC_FRAME" and first_plc is None:
                first_plc = record
            if record.get("modality") == "image" and first_image is None:
                first_image = record
    if first_plc:
        frame = bytes.fromhex(first_plc["payload"]["frame_hex"])
        for kind in ("truncated", "crc", "unknown_address", "header", "function", "length"):
            if kind in ("truncated", "crc", "unknown_address"):
                corrupt = corrupt_frame(frame, kind)
            else:
                body = bytearray(frame[:-2])
                body[{"header": 0, "function": 3, "length": 4}[kind]] ^= 1
                corrupt = bytes(body) + struct.pack("<H", crc16(body))
            record = deepcopy(first_plc)
            record["event_key"] = "diagnostic/" + kind
            record["payload"] = {"frame_hex": corrupt.hex(" ")}
            yield record
    if first_image:
        record = deepcopy(first_image)
        record["event_key"] = "diagnostic/image_hash"
        record["payload"]["sha256"] = "0" * 64
        yield record


def run_batch(input_folder, output_folder, *, exercise_invalid=False, repeat=1):
    source, output = Path(input_folder).resolve(), Path(output_folder).resolve()
    if repeat < 1:
        raise ValueError("repeat must be positive")
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("input and output directories must not overlap")
    if output.is_relative_to((PROJECT / "minimal_factory").resolve()):
        raise ValueError("output cannot be inside minimal_factory")
    config, bindings, batch_id = load_batch(source)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "raw_ingest_messages.jsonl").open("w", encoding="utf-8", newline="\n") as accepted, \
         (output / "rejected_messages.jsonl").open("w", encoding="utf-8", newline="\n") as rejected:
        session = IngestSession(batch_id, source, bindings,
                                lambda value: accepted.write(canonical(value) + "\n"),
                                lambda value: rejected.write(canonical(value) + "\n"))
        connectors = connectors_for(session, source / "demo_erp.sqlite3")
        original_counts = []
        for _ in range(repeat):
            before = session.counts.copy()
            for _message in ingest_stream(read_jsonl(source / "transport_records.jsonl", session),
                                          connectors, session, verify_database=True):
                pass
            original_counts.append(dict(session.counts - before))
        before = session.counts.copy()
        supplementary_inputs(config, output, connectors, session)
        supplements = dict(session.counts - before)
        before = session.counts.copy()
        if exercise_invalid:
            list(ingest_stream(invalid_examples(source), connectors, session))
        diagnostic_fixtures = dict(session.counts - before)
        summary = session.summary()
        summary.update(batch_id=batch_id, asset_root=str(source), source_batches=original_counts,
                       supplemental=supplements, diagnostic_fixtures=diagnostic_fixtures,
                       source_sha256=hashlib.sha256((source / "transport_records.jsonl").read_bytes()).hexdigest(),
                       limitations=["MQTT/OPC use WP1 in-process mocks", "PostgreSQL adapter tested with DB-API double; no live server",
                                    "No WP3 semantic_mapping implemented", "Deduplication is session scoped"],
                       status="completed" if not any(c.get("rejected", 0) for c in original_counts)
                       and not supplements.get("rejected", 0) else "completed_with_source_rejections")
    write_json(output / "connector_summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--exercise-invalid", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    summary = run_batch(args.input, args.output, exercise_invalid=args.exercise_invalid, repeat=args.repeat)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
