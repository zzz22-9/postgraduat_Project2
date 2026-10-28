"""Read-only source adapters, explicit scenario binding, SQLite import and queries.

No ERP/MES writes or network services. All scenarios are teaching assumptions.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from synthetic_signals.generate_signals import intervals_from

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "unified_samples" / "samples_v1.sqlite3"
APP_ID = 1431131697


def js(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def object_id(scenario, kind, raw):
    return f"MM-V1:{scenario}:{kind}:{raw}" if raw is not None else None


def within(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Source path escapes its directory: {relative}")
    if not path.is_file():
        raise ValueError(f"Missing source file: {path}")
    return path


def load_sources(root=ROOT, config_path=MODULE / "scenarios.json"):
    config = read_json(config_path)
    if config["version"] != 1 or config["equipment_id"] != "E-ASM":
        raise ValueError("This version supports only the E-ASM teaching timeline")
    source_run = config["source_run_id"]
    base = within(root, f"data/erp.simulations/{source_run}/input.json").parent
    input_path, event_path = base / "input.json", base / "events.jsonl"
    image_dir = root / "synthetic_images" / "rendered_v1"
    signal_dir = root / "synthetic_signals" / "generated_v1"
    image_manifest = read_json(image_dir / "manifest.json")
    signal_manifest = read_json(signal_dir / "manifest.json")
    summaries = read_json(signal_dir / "summary.json")
    paths = [config_path, input_path, event_path, image_dir / "manifest.json",
             signal_dir / "manifest.json", signal_dir / "summary.json", MODULE / "schema.sql", Path(__file__)]
    if sha(event_path) != signal_manifest["source_events_sha256"] or signal_manifest["source_run_id"] != source_run:
        raise ValueError("Signal timeline source hash/run mismatch")
    all_events = read_lines(event_path)
    if any(e["run_id"] != source_run for e in all_events):
        raise ValueError("Mixed source runs")
    events = [e for e in all_events if e["equipment_id"] == config["equipment_id"]
              and e["event_type"] in ("processing_started", "processing_finished")]
    intervals = intervals_from(events)
    inputs = read_json(input_path)
    orders = {o["order_id"]: o for o in inputs["orders"]}
    for interval in intervals:
        order = orders[interval["order_id"]]
        if order["work_order_id"] != interval["work_order_id"]:
            raise ValueError("Order/work-order mismatch")
        op = next(p for p in order["operations"] if p["operation_id"] == interval["operation_id"])
        if op["equipment_id"] != config["equipment_id"]:
            raise ValueError("Process/equipment mismatch")
    image_interval = next(i for i in intervals if i["work_order_id"] == config["image_work_order_id"]
                          and i["operation_id"] == config["image_operation_id"])
    for time in config["image_observations"].values():
        if not image_interval["start_s"] <= time < image_interval["end_s"]:
            raise ValueError("Image time outside configured processing interval")
    if config["image_observations"]["before_s"] >= config["image_observations"]["after_s"]:
        raise ValueError("Image before/after times are reversed")
    images = {}
    for sample in image_manifest["samples"]:
        key = sample["sample_id"]
        if key in images:
            raise ValueError("Duplicate image sample ID")
        path = within(image_dir, sample["image_path"])
        if sha(path) != sample["image_sha256"]:
            raise ValueError(f"Image hash mismatch: {key}")
        images[key] = (sample, path)
        paths.append(path)
    signals = {}
    for name in {s["signal_source"] for s in config["scenarios"]}:
        path = within(signal_dir, name + ".jsonl")
        if sha(path) != summaries[name]["sha256"]:
            raise ValueError(f"Signal hash mismatch: {name}")
        rows = read_lines(path)
        if [r["sim_time_s"] for r in rows] != list(range(541)):
            raise ValueError("Expected 541 consecutive 1Hz samples")
        for row in rows:
            time = row["sim_time_s"]
            context = next((i for i in intervals if i["start_s"] <= time < i["end_s"]), None)
            if (row["source_run_id"] != source_run or row["equipment_id"] != config["equipment_id"]
                    or row["scenario"] != name or row["unit"] != "A" or row["synthetic"] is not True
                    or row["signal"] != "current_envelope" or not math.isfinite(row["value"]) or row["value"] < 0):
                raise ValueError("Invalid signal identity, unit or value")
            for field in ["order_id", "work_order_id", "operation_id"]:
                if row[field] != (context[field] if context else None):
                    raise ValueError(f"Signal business context disagrees with events: {field} at {time}")
            if row["equipment_state"] != ("processing" if context else "idle"):
                raise ValueError("Signal state disagrees with timeline")
        signals[name] = (rows, path)
        paths.append(path)
    ids = [s["id"] for s in config["scenarios"]]
    if len(set(ids)) != len(ids) or any(not s or ":" in s for s in ids):
        raise ValueError("Invalid scenario IDs")
    for scenario in config["scenarios"]:
        for state in ["unfastened", scenario["after_image_state"]]:
            for view in ["front", "side"]:
                if f"assembly_{state}_{view}" not in images:
                    raise ValueError("Missing image required by scenario")
    hashes = {str(p.resolve()): sha(p) for p in paths}
    fingerprint = hashlib.sha256(js(hashes).encode()).hexdigest()
    return dict(config=config, intervals=intervals, image_interval=image_interval, events=events,
                inputs=inputs, signals=signals, images=images, hashes=hashes, fingerprint=fingerprint,
                source_event_path=event_path, source_input_path=input_path)


def connect_read(db):
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    if con.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
        con.close()
        raise ValueError("Not a unified sample database")
    return con


def check(db):
    db = Path(db)
    with closing(connect_read(db)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database integrity check failed")
        for asset in con.execute("SELECT * FROM assets"):
            path = within(db.parent, asset["relative_path"])
            if sha(path) != asset["sha256"]:
                raise ValueError("Stored image hash mismatch")
        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ["scenarios", "objects", "states", "relations", "assets", "observations"]}
        counts["modalities"] = {r[0]: r[1] for r in con.execute("SELECT modality,COUNT(*) FROM observations GROUP BY modality")}
        return counts


def insert_object(con, scenario, raw, kind, domain, level, source, context, initial="unknown"):
    uid = object_id(scenario, kind, raw)
    con.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?)", (uid, scenario, raw, kind, domain, level, source, js(context)))
    con.execute("INSERT INTO states VALUES (?,?,?,?,?)", (uid, 0, -1, initial, js({"basis": "initial_teaching_assumption"})))
    return uid


def insert_relation(con, scenario, source, target, kind, basis, start=0, end=None):
    con.execute("INSERT INTO relations(scenario_id,source_id,target_id,relation_type,valid_from_s,valid_to_s,basis_json) VALUES (?,?,?,?,?,?,?)",
                (scenario, source, target, kind, start, end, js(basis)))


def insert_observation(con, scenario, key, modality, time, sequence, context, payload, provenance, value=None, asset=None):
    con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        f"MM-V1:{scenario}:{modality}:{key}", scenario, object_id(scenario, "Equipment", "E-ASM"),
        object_id(scenario, "Order", context.get("order_id")), object_id(scenario, "WorkOrder", context.get("work_order_id")),
        object_id(scenario, "Process", context.get("operation_id")), modality, time, sequence,
        value, "A" if modality == "signal" else None, asset, js(payload), js(provenance)))


def build(db=DEFAULT_DB, root=ROOT, config_path=MODULE / "scenarios.json"):
    db = Path(db).resolve()
    sources = load_sources(root, config_path)
    if db.exists():
        with closing(connect_read(db)) as con:
            row = con.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
            if row is None or row[0] != sources["fingerprint"]:
                raise ValueError("Inputs/schema changed; use a NEW --db path to preserve this dataset")
        return dict(status="unchanged", database=str(db), counts=check(db))
    db.parent.mkdir(parents=True, exist_ok=True)
    config = sources["config"]
    # Copy content-addressed images. Existing image data are never silently replaced.
    asset_dir = db.parent / "assets"
    asset_dir.mkdir(exist_ok=True)
    for sample, source_path in sources["images"].values():
        target = asset_dir / (sample["image_sha256"] + ".png")
        if target.exists():
            if sha(target) != sample["image_sha256"]:
                raise ValueError("Corrupt existing managed image")
        else:
            with target.open("xb") as stream:
                stream.write(source_path.read_bytes())
    # Publish only a fully checked database; hard-link creation never replaces an
    # existing target. The temporary DB is on the same filesystem as its target.
    temporary = db.with_name(db.name + ".building-" + uuid4().hex)
    try:
        _populate(temporary, sources)
        counts = check(temporary)
        os.link(temporary, db)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(status="created", database=str(db), counts=counts)


def _populate(db, sources):
    config = sources["config"]
    # Data inserts form one transaction; this file is never exposed mid-import.
    with db.open("xb"):
        pass
    with closing(sqlite3.connect(db)) as con:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript((MODULE / "schema.sql").read_text(encoding="utf-8"))
        with con:
            for sample, path in sources["images"].values():
                con.execute("INSERT INTO assets VALUES (?,?,?,?)", (sample["sample_id"], sample["image_sha256"],
                    "assets/" + sample["image_sha256"] + ".png", js({"original_file": str(path), "original_manifest_record": sample})))
            for scenario in config["scenarios"]:
                sid = scenario["id"]
                con.execute("INSERT INTO scenarios VALUES (?,?,?,?)", (sid, scenario["title"], config["source_run_id"], js(scenario)))
                previous = None
                for item in config["hierarchy"]:
                    uid = insert_object(con, sid, item["id"], item["type"], item["domain"], item["level"],
                                        "scenario_config", {"teaching_assumption": True}, "configured")
                    if previous:
                        insert_relation(con, sid, previous, uid, "contains", {"teaching_assumption": True})
                    previous = uid
                equipment = insert_object(con, sid, "E-ASM", "Equipment", "manufacturing", "device", "SimPy_replay",
                                          {"source_run_id": config["source_run_id"], "name": "组装工位"}, "idle")
                insert_relation(con, sid, previous, equipment, "contains", {"teaching_assumption": True})
                for interval in sources["intervals"]:
                    raw_order = next(o for o in sources["inputs"]["orders"] if o["order_id"] == interval["order_id"])
                    order = insert_object(con, sid, interval["order_id"], "Order", "enterprise", "enterprise", "SimPy_input",
                                          {"raw_input": raw_order, "source_file": str(sources["source_input_path"]), "scope": "assembly_only"})
                    work = insert_object(con, sid, interval["work_order_id"], "WorkOrder", "manufacturing", "factory", "SimPy_input",
                                         {"source_run_id": config["source_run_id"], "source_file": str(sources["source_input_path"]), "scope": "assembly_only"})
                    process = insert_object(con, sid, interval["operation_id"], "Process", "manufacturing", "device", "SimPy_replay",
                                            {"name": "组装", "source_run_id": config["source_run_id"]}, "waiting")
                    insert_relation(con, sid, order, work, "has_work_order", {"source": "input_snapshot"})
                    insert_relation(con, sid, work, process, "has_process", {"source": "input_snapshot"})
                    insert_relation(con, sid, process, equipment, "planned_on", {"source": "input_snapshot"})
                    for target, kind in [(process, "processing"), (work, "serves_work_order")]:
                        insert_relation(con, sid, equipment, target, kind, {"source": "processing_events", "interval": interval},
                                        interval["start_s"], interval["end_s"])
                for event in sources["events"]:
                    t, seq = event["sim_time_min"] * 60, event["sequence"]
                    started = event["event_type"] == "processing_started"
                    con.execute("INSERT INTO states VALUES (?,?,?,?,?)", (equipment, t, seq, "processing" if started else "idle",
                                js({"work_order_id": event["work_order_id"] if started else None, "source_event_sequence": seq})))
                    con.execute("INSERT INTO states VALUES (?,?,?,?,?)", (object_id(sid, "Process", event["operation_id"]), t, seq,
                                "processing" if started else "completed", js({"source_event_sequence": seq})))
                    insert_observation(con, sid, str(seq), "event", t, seq, event, event,
                        {"source_system": "SimPy_replay", "source_file": str(sources["source_event_path"]),
                         "source_sha256": sources["hashes"][str(sources["source_event_path"].resolve())], "source_run_id": config["source_run_id"],
                         "synthetic": True, "scope": "assembly_only_no_quality_events"})
                signal_rows, signal_path = sources["signals"][scenario["signal_source"]]
                for index, row in enumerate(signal_rows):
                    insert_observation(con, sid, row["sample_id"], "signal", row["sim_time_s"], 1000000 + index, row, row,
                        {"source_system": "synthetic_signal", "source_file": str(signal_path), "source_line": index + 1,
                         "source_sha256": sources["hashes"][str(signal_path.resolve())], "original_signal_run_id": row["run_id"],
                         "synthetic": True, "binding_basis": "synthetic_scenario_replay"}, value=row["value"])
                for stage, state in [("before_s", "unfastened"), ("after_s", scenario["after_image_state"])]:
                    for view in ["front", "side"]:
                        key = f"assembly_{state}_{view}"
                        sample, _ = sources["images"][key]
                        insert_observation(con, sid, key, "image", config["image_observations"][stage],
                            2000000 + (0 if view == "front" else 1), sources["image_interval"], sample,
                            {"source_system": "procedural_renderer", "synthetic": True, "binding_basis": config["image_binding_basis"],
                             "original_time": None, "assigned_time_s": config["image_observations"][stage],
                             "source_sha256": sample["image_sha256"], "annotation_is_algorithm_output": False}, asset=key)
            if any(sha(Path(path)) != expected for path, expected in sources["hashes"].items()):
                raise ValueError("Source changed during import")
            for key, value in {"fingerprint": sources["fingerprint"], "source_hashes": js(sources["hashes"]),
                               "config": js(config), "created_at": datetime.now(timezone.utc).isoformat(), "clock": "simulation_seconds"}.items():
                con.execute("INSERT INTO metadata VALUES (?,?)", (key, value))


def require_scenario(con, scenario):
    if not con.execute("SELECT 1 FROM scenarios WHERE scenario_id=?", (scenario,)).fetchone():
        raise ValueError(f"Unknown scenario: {scenario}")


def query(db=DEFAULT_DB, scenario="normal", equipment="E-ASM", work_order=None, start=0, end=540, modality=None, limit=50, offset=0):
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= 540 and 1 <= limit <= 2000 and offset >= 0):
        raise ValueError("Invalid query window/pagination")
    if modality not in (None, "image", "signal", "event"):
        raise ValueError("Invalid modality")
    clauses = ["o.scenario_id=?", "o.object_id=?", "o.time_s>=?", "o.time_s<=?"]
    params = [scenario, object_id(scenario, "Equipment", equipment), start, end]
    if work_order:
        clauses.append("o.work_order_object_id=?")
        params.append(object_id(scenario, "WorkOrder", work_order))
    if modality:
        clauses.append("o.modality=?")
        params.append(modality)
    where = " AND ".join(clauses)
    with closing(connect_read(db)) as con:
        require_scenario(con, scenario)
        total = con.execute("SELECT COUNT(*) FROM observations o WHERE " + where, params).fetchone()[0]
        counts = {row[0]: row[1] for row in con.execute("SELECT modality,COUNT(*) FROM observations o WHERE " + where + " GROUP BY modality", params)}
        rows = con.execute("SELECT o.*,a.relative_path FROM observations o LEFT JOIN assets a USING(asset_id) WHERE " + where
                           + " ORDER BY o.time_s,o.sequence,o.observation_id LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        result = []
        for row in rows:
            record = dict(row)
            for field in ["payload_json", "provenance_json"]:
                record[field.removesuffix("_json")] = json.loads(record.pop(field))
            result.append(record)
    return dict(scenario=scenario, total=total, counts=counts, limit=limit, offset=offset, records=result)


def snapshot(db=DEFAULT_DB, scenario="normal", at=180):
    if not math.isfinite(at) or not 0 <= at <= 540:
        raise ValueError("Snapshot time outside dataset")
    with closing(connect_read(db)) as con:
        require_scenario(con, scenario)
        objects = []
        for row in con.execute("SELECT * FROM objects WHERE scenario_id=? ORDER BY object_id", (scenario,)):
            state = con.execute("SELECT * FROM states WHERE object_id=? AND time_s<=? ORDER BY time_s DESC,sequence DESC LIMIT 1", (row["object_id"], at)).fetchone()
            obj = dict(row)
            obj["context"] = json.loads(obj.pop("context_json"))
            obj["state"] = state["state"]
            obj["state_context"] = json.loads(state["context_json"])
            obj["timestamp"] = {"clock": "simulation", "seconds": state["time_s"], "sequence": state["sequence"]}
            objects.append(obj)
        relations = [dict(r) for r in con.execute("SELECT * FROM relations WHERE scenario_id=? AND valid_from_s<=? AND (valid_to_s IS NULL OR ?<valid_to_s) ORDER BY relation_id", (scenario, at, at))]
        for rel in relations:
            rel["basis"] = json.loads(rel.pop("basis_json"))
    return dict(scenario=scenario, as_of_sim_time_s=at, clock="simulation_seconds_no_wall_clock_origin", objects=objects, relations=relations)


def overview(db=DEFAULT_DB):
    result = dict(counts=check(db), scenarios=[])
    with closing(connect_read(db)) as con:
        for row in con.execute("SELECT * FROM scenarios ORDER BY scenario_id"):
            sid = row["scenario_id"]
            stats = con.execute("SELECT COUNT(*),SUM(value>4),MIN(value),MAX(value) FROM observations WHERE scenario_id=? AND modality='signal'", (sid,)).fetchone()
            windows = [r[0] for r in con.execute("SELECT time_s FROM observations WHERE scenario_id=? AND modality='signal' AND value>4 ORDER BY time_s", (sid,))]
            result["scenarios"].append(dict(scenario_id=sid, title=row["title"], samples=stats[0], above_4A_count=stats[1], minimum_A=stats[2], maximum_A=stats[3], above_4A_sample_times_s=windows))
    result["rule_limitation"] = "value > 4 A is a teaching threshold; no visual inference or causal diagnosis"
    return result
