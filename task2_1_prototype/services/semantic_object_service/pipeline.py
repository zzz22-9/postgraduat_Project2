"""WP3 CLI: consume only WP2 RawIngestMessage JSONL and export seven-field objects/history."""
import argparse
from collections import Counter
import json
from pathlib import Path

from .mapping_engine import MappingEngine
from .models import json_text, validate_object
from .storage import SQLiteStore
from task2_1_prototype.services.connector_service.pipeline import strict_json


def ingest_stream(records, engine):
    for record in records:
        yield engine.semantic_mapping(record)


def fault_timeline(history):
    # Keep the final same-time projection after all available protocols/modalities arrived.
    moments = {}
    for obj in history:
        ctx = obj["context"]
        if obj["object_type"] != "equipment" or "sim_time_s" not in ctx or ctx["history"]["late"]:
            continue
        moments[(obj["object_id"], obj["timestamp"])] = obj
    transitions, intervals, open_fault = [], [], {}
    previous = {}
    for (device, timestamp), obj in sorted(moments.items()):
        status, seconds = obj["state"]["status"], obj["context"]["sim_time_s"]
        if previous.get(device) != status:
            transitions.append(dict(object_id=device, timestamp=timestamp, sim_time_s=seconds, status=status,
                                    fault_count=obj["state"].get("fault_count", 0), recovery_count=obj["state"].get("recovery_count", 0)))
        if status in ("fault", "overload") and device not in open_fault:
            open_fault[device] = dict(object_id=device, start_s=seconds, start_timestamp=timestamp, end_s=None, end_timestamp=None)
        if status not in ("fault", "overload") and device in open_fault:
            interval = open_fault.pop(device)
            interval.update(end_s=seconds, end_timestamp=timestamp)
            intervals.append(interval)
        previous[device] = status
    return {"transitions": transitions, "fault_intervals": intervals + list(open_fault.values())}


def summarize(store):
    objects, history = store.query(), store.query(history=True)
    counts = store.counts()
    cycles = store.cycles()
    return {**counts, "objects_by_type": dict(Counter(o["object_type"] for o in objects)),
            "objects_by_level": dict(Counter(o["level"] for o in objects)),
            "cycles_by_device": dict(Counter(row["object_id"] for row in cycles)),
            "complete_cycles": sum(c["complete"] for c in cycles),
            "cycles_with_all_three_protocols": sum(c["protocols"] == ["MQTT", "OPC_UA", "PLC_FRAME"] for c in cycles),
            **fault_timeline(history)}


def run_batch(input_folder, output_folder, *, repeat=1):
    source, output = Path(input_folder).resolve(), Path(output_folder).resolve()
    if repeat < 1:
        raise ValueError("repeat must be positive")
    connector_summary = strict_json((source / "connector_summary.json").read_text(encoding="utf-8"))
    assets = Path(connector_summary["asset_root"]).resolve()
    legacy = Path(__file__).parents[3] / "minimal_factory"
    for protected in (source, assets, legacy.resolve()):
        if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError("output must not overlap source assets or the legacy project")
    # No transport_records/rejected_messages are opened by WP3.
    path = source / "raw_ingest_messages.jsonl"
    output.mkdir(parents=True, exist_ok=False)
    store = SQLiteStore(output / "semantic_objects.sqlite3")
    try:
        engine = MappingEngine(store)
        input_count = 0
        for _ in range(repeat):
            with path.open(encoding="utf-8") as stream:
                responses = engine.map_batch(strict_json(line) for line in stream)
                input_count = len(responses)
        if input_count != connector_summary["counts"]["accepted"]:
            raise ValueError("raw message count differs from WP2 accepted count")
        objects, history = store.query(), store.query(history=True)
        for name, values in (("semantic_objects.jsonl", objects), ("semantic_state_history.jsonl", history)):
            with (output / name).open("w", encoding="utf-8", newline="\n") as stream:
                for obj in values:
                    stream.write(json_text(validate_object(obj)) + "\n")
        summary = {"status": "completed", "input_messages": input_count, "deliveries": input_count * repeat,
                   "duplicate_deliveries": input_count * repeat - store.counts()["processed_messages"],
                   "asset_root": str(assets), "input_root": str(source), "rules": engine.rules,
                   "scope": "WP3 semantic objects only; WP4 relations/graphs not implemented", **summarize(store)}
        (output / "semantic_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(run_batch(args.input, args.output, repeat=args.repeat), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
