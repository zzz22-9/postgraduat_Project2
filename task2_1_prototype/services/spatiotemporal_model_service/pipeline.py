"""Replay WP3 semantic history into the local WP4 graph and export key time slices."""
import argparse
from collections import Counter
import json
from pathlib import Path

from task2_1_prototype.services.connector_service.pipeline import strict_json
from task2_1_prototype.services.semantic_object_service.models import json_text
from .graph_manager import GraphManager
from .neo4j_export import export_neo4j
from .storage import SQLiteGraphStore

RULES_FILE = Path(__file__).parents[2] / "config/relation_rules.json"


def read_lines(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [strict_json(line) for line in stream]


def run_batch(input_folder, output_folder, *, rules_file=None, repeat=1):
    source, output = Path(input_folder).resolve(), Path(output_folder).resolve()
    summary = strict_json((source / "semantic_summary.json").read_text(encoding="utf-8"))
    legacy = Path(__file__).parents[3] / "minimal_factory"
    for protected in (source, Path(summary["asset_root"]).resolve(), Path(summary["input_root"]).resolve(), legacy.resolve()):
        if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError("output must not overlap WP1/WP2/WP3 inputs or minimal_factory")
    if repeat < 1:
        raise ValueError("repeat must be positive")
    objects, history = read_lines(source / "semantic_objects.jsonl"), read_lines(source / "semantic_state_history.jsonl")
    if len(objects) != summary["semantic_objects"] or len(history) != summary["semantic_state_history"]:
        raise ValueError("WP3 object/history counts disagree with the source manifest")
    rules = strict_json(Path(rules_file or RULES_FILE).read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    store = SQLiteGraphStore(output / "spatiotemporal_model.sqlite3")
    try:
        manager = GraphManager(objects, store=store, rules=rules)
        accepted = sum(manager.replay(history) for _ in range(repeat))
        snapshots = [manager.snapshot(second, save=True) for second in (59, 75, 95, 360)]
        current = manager.snapshot()
        with store.transaction():
            store.save_edges(current["edges"])
        impact_report = {"assessment_kind": "diagnostic_risk_only", "mes_schedule_changed": False,
                         "checkpoints": [manager.impact("CNC_01", second) for second in (59, 75, 95, 360)],
                         "order_dependencies_at_75": manager.impact("ORDER_A", 75),
                         "work_order_dependencies_at_75": manager.impact("WO023", 75),
                         "risk_audit": store.risk_history()}
        with (output / "relations.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for edge in current["edges"]:
                stream.write(json_text(edge) + "\n")
        outputs = {"graph_snapshots.json": snapshots, "impact_report.json": impact_report,
                   "neo4j_export.json": export_neo4j(current)}
        for name, value in outputs.items():
            (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = {"status": "completed", "mode": "local_networkx_sqlite", "neo4j_connected": False,
                  "input_history_records": len(history), "accepted_events": accepted,
                  "duplicate_events": len(history) * repeat - accepted, "input_root": str(source),
                  "rules": rules, "nodes": len(current["nodes"]), "edges": len(current["edges"]),
                  "edge_types": dict(Counter(e["relation"] for e in current["edges"])),
                  "level_counts": current["level_counts"], "object_type_counts": current["object_type_counts"],
                  "checkpoint_active_impacts": {str(r["as_of"]): len(r["active_impacts"]) for r in impact_report["checkpoints"]},
                  "risk_episodes": [{k: r[k] for k in ("path", "start_s", "end_s", "status", "fault_codes", "closure_reason")}
                                    for r in store.risk_history()], "database_counts": store.counts(),
                  "scope": "WP4 only; WP5 gateway and run_e2e_demo not implemented"}
        (output / "spatiotemporal_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rules")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(run_batch(args.input, args.output, rules_file=args.rules, repeat=args.repeat), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
