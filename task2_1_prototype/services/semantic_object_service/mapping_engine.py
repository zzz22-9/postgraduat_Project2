"""Stateful semantic mapping over validated WP2 messages, isolated from source transports."""
from copy import deepcopy
from datetime import timedelta
import hashlib

from task2_1_prototype.contracts.models import RawIngestMessage, parse_timestamp, utc_text
from .fusion import DEFAULT_RULES, apply_log, apply_signal, image_state
from .models import MappingError, json_text, validate_object, validate_raw
from .storage import SQLiteStore

TYPE_LOCATION = {"equipment": ("manufacturing", "device"), "process": ("manufacturing", "line"),
                 "work_order": ("manufacturing", "factory"), "order": ("enterprise", "enterprise")}
EVENT_STATUS = {"resource_requested": "waiting", "resource_acquired": "ready", "processing_started": "running",
                "processing_finished": "completed", "order_started": "running", "order_completed": "completed"}


def identity(raw):
    ctx = raw["raw_context"]
    ingest = ctx.get("_ingest")
    if not isinstance(ingest, dict) or not isinstance(ingest.get("resolved_ids"), dict):
        raise MappingError("PROVENANCE", "WP2 _ingest.resolved_ids provenance is required")
    ids = ingest["resolved_ids"]
    if not isinstance(ingest.get("batch_id"), str) or not ingest["batch_id"]:
        raise MappingError("PROVENANCE", "batch_id is required")
    if type(ingest.get("source_sequence")) is not int or ingest["source_sequence"] < 0:
        raise MappingError("PROVENANCE", "source_sequence must be a nonnegative integer")
    if not isinstance(ids.get("object_id"), str) or not ids["object_id"]:
        raise MappingError("IDENTITY", "resolved object_id is required")
    kind = raw["object_hint"]["suggested_type"]
    if kind + "_id" in ids and ids[kind + "_id"] != ids["object_id"]:
        raise MappingError("IDENTITY", "primary object conflicts with resolved kind ID")
    if kind == "process" or raw["modality"] != "structured_record":
        for key in ("equipment_id", "process_id", "work_order_id", "order_id"):
            if not isinstance(ids.get(key), str) or not ids[key]:
                raise MappingError("PROCESS_SCOPE", f"field/process data requires resolved {key}")
        if ids.get("process_instance_key") != [ids["work_order_id"], ids["process_id"]]:
            raise MappingError("PROCESS_SCOPE", "process_instance_key must retain its parent work order")
    return ids, ingest


def clock_scope(raw):
    ctx = raw["raw_context"]
    if ctx.get("clock") == "simulation":
        seconds = ctx.get("sim_time_s")
        if type(seconds) not in (int, float) or seconds < 0 or not ctx.get("run_id"):
            raise MappingError("SIMULATION_CLOCK", "simulation requires run_id and nonnegative sim_time_s")
        if not isinstance(ctx.get("simulation_epoch"), str):
            raise MappingError("SIMULATION_CLOCK", "simulation_epoch is required")
        epoch = parse_timestamp(ctx["simulation_epoch"])
        if epoch + timedelta(seconds=seconds) != parse_timestamp(raw["timestamp"]):
            raise MappingError("SIMULATION_CLOCK", "timestamp disagrees with epoch + sim_time_s")
        return "simulation/" + str(ctx["run_id"]) + "/" + utc_text(epoch)
    return "reference" if ctx.get("snapshot_kind") == "configuration" else "wall"


def new_context(raw, ids, ingest, scope):
    ctx = raw["raw_context"]
    result = {"batch_id": ingest["batch_id"], "clock_scope": scope, "source_sequence": ingest["source_sequence"],
              "timestamp_basis": ctx.get("timestamp_basis", "simulation_epoch" if scope.startswith("simulation/") else "source_wall_clock"),
              "last_message_id": raw["message_id"], "raw_context": deepcopy(ctx)}
    for key in ("enterprise", "factory", "line", "equipment", "process", "work_order", "order"):
        value = ids.get(key + "_id", ctx.get(key + "_id"))
        if value:
            result[key] = value
    if ids.get("process_instance_key"):
        result["process_instance_key"] = deepcopy(ids["process_instance_key"])
    for key in ("resource_kind", "parent", "legacy_ids", "run_id", "simulation_epoch", "sim_time_s", "sequence", "phase", "rework_id"):
        if key in ctx:
            result[key] = deepcopy(ctx[key])
    return result


def apply_business(raw, obj):
    state, payload, kind = obj["state"], raw["raw_payload"], obj["object_type"]
    protected_status = state.get("status") if state.get("fault_latched") else None
    business = payload.get("work_order", payload) if kind == "work_order" else payload
    for field in ("status", "quantity", "product_id", "product_name", "due_date", "priority", "progress", "planned_date",
                  "name", "unit", "created_at", "released_at", "completed_at", "accepted_quantity", "rejected_quantity", "wait_min"):
        if field in business:
            state[field] = deepcopy(business[field])
    if isinstance(business.get("operations"), list):
        state["operation_count"] = len(business["operations"])
    event = payload.get("event_type", payload.get("event"))
    if event:
        state["last_event"] = event
        if event in EVENT_STATUS:
            state["status"] = EVENT_STATUS[event]
        if event == "quality_result":
            state["inspection_result"] = "pass" if payload.get("rejected_quantity") == 0 else "reject"
    if kind == "resource":
        obj["context"]["resource_kind"] = raw["raw_context"].get("resource_kind", "material")
        state.setdefault("status", "available" if obj["context"]["resource_kind"] == "material" else "configured")
    if protected_status:
        state["status"] = protected_status


class MappingEngine:
    def __init__(self, store=None, rules=None):
        self.store = store if store is not None else SQLiteStore()
        self.rules = {**DEFAULT_RULES, **(rules or {})}
        if set(self.rules) != set(DEFAULT_RULES) or type(self.rules["window_size"]) is not int or self.rules["window_size"] < 1:
            raise ValueError("invalid signal rules/window size")
        for name in ("temperature", "vibration", "load"):
            warning, fault = self.rules[name + "_warning"], self.rules[name + "_fault"]
            if type(warning) not in (int, float) or type(fault) not in (int, float) or not 0 <= warning < fault:
                raise ValueError("thresholds must be finite nonnegative numbers ordered warning < fault")
        with self.store.transaction():
            self.store.bind("mapping_rules", self.rules)

    def semantic_mapping(self, raw_data: dict | RawIngestMessage) -> dict:
        with self.store.transaction():
            return self._map(raw_data)

    def map_batch(self, raw_messages):
        """Atomic batch; all outputs/processed IDs/history are rolled back on any invalid input."""
        with self.store.transaction():
            return [self._map(raw) for raw in raw_messages]

    def _map(self, raw_data):
        raw = validate_raw(raw_data)
        fingerprint = hashlib.sha256(json_text(raw).encode()).hexdigest()
        previous = self.store.processed(raw["message_id"])
        if previous:
            if previous[0] != fingerprint:
                raise MappingError("IDENTITY_CONFLICT", "message_id was already processed with different content")
            return previous[1]
        raw["timestamp"] = utc_text(parse_timestamp(raw["timestamp"]))
        ids, ingest = identity(raw)
        scope = clock_scope(raw)
        self.store.bind("batch_id", ingest["batch_id"])
        if scope.startswith("simulation/"):
            self.store.bind("simulation_scope", scope)
        kind = raw["object_hint"]["suggested_type"]
        if raw["modality"] != "structured_record" and kind != "equipment":
            raise MappingError("OBJECT_MODALITY", "field modalities must target resolved equipment")
        targets = [(ids["object_id"], kind)]
        if raw["modality"] == "image":
            targets.append((ids["process_id"], "process"))
        response = None
        for object_id, object_type in targets:
            result = self._update(raw, ids, ingest, scope, object_id, object_type)
            if response is None:
                response = result
        self.store.remember(raw, fingerprint, response)
        return response

    def _update(self, raw, ids, ingest, scope, object_id, kind):
        domain, level = TYPE_LOCATION.get(kind, (raw["object_hint"]["domain_hint"], raw["object_hint"]["level_hint"]))
        self.store.bind("identity/" + object_id, [kind, domain, level])
        if kind == "process":
            self.store.bind("process_parent/" + object_id, ids["work_order_id"])
        tracks = self.store.tracks(object_id)
        previous = tracks.get(scope)
        late = bool(previous and raw["timestamp"] < previous["timestamp"])
        if previous and raw["modality"] != "timeseries_signal":
            source = previous["context"].get("sources", {}).get(raw["protocol"] + "/" + raw["modality"])
            if source and (raw["timestamp"], ingest["source_sequence"]) < (source["timestamp"], source["source_sequence"]):
                late = True
        # Older events are projected from past state, never from a future snapshot.
        base = self.store.before(object_id, scope, raw["timestamp"]) if late else previous
        state = deepcopy(base["state"]) if base else {"status": "unknown"}
        context = deepcopy(base["context"]) if base else {}
        context.pop("history", None)
        context.pop("clock_states", None)
        context.update(new_context(raw, ids, ingest, scope))
        sources = context.setdefault("sources", {})
        sources[raw["protocol"] + "/" + raw["modality"]] = dict(message_id=raw["message_id"], timestamp=raw["timestamp"],
                                                                source_sequence=ingest["source_sequence"], source_system=raw["source_system"])
        obj = dict(object_id=object_id, object_type=kind, domain=domain, level=level,
                   state=state, context=context, timestamp=raw["timestamp"])
        if raw["modality"] == "timeseries_signal":
            apply_signal(self.store, raw, obj, scope, self.rules)
        elif raw["modality"] == "image":
            state["visual_inspection"] = image_state(raw["raw_payload"])
        elif raw["modality"] == "log_text":
            apply_log(raw, state)
        else:
            apply_business(raw, obj)
        obj = validate_object(obj)
        applied = False
        if not late:
            self.store.put_track(object_id, scope, obj)
            tracks[scope] = obj
            # Never compare simulation timestamps with acquisition wall-clock timestamps.
            def priority(track):
                clock = track["context"]["clock_scope"]
                preferred = clock == "wall" if kind == "order" else clock.startswith("simulation/")
                return (int(preferred), int(clock != "reference"), clock)
            chosen = max(tracks.values(), key=priority)
            applied = chosen["context"]["clock_scope"] == scope
            current = deepcopy(chosen)
            current["context"]["clock_states"] = {key: {"timestamp": track["timestamp"], "state": track["state"],
                                                          "timestamp_basis": track["context"]["timestamp_basis"]}
                                                   for key, track in sorted(tracks.items())}
            self.store.put_latest(validate_object(current))
        history = deepcopy(obj)
        history["context"]["history"] = dict(message_id=raw["message_id"], late=late, applied_to_latest=applied, clock_scope=scope)
        history = validate_object(history)
        self.store.add_history(history, raw, scope, late, applied)
        return history


def semantic_mapping(raw_data: dict | RawIngestMessage) -> dict:
    """One-message standalone projection. For stateful streams use MappingEngine.semantic_mapping."""
    store = SQLiteStore()
    try:
        return MappingEngine(store).semantic_mapping(raw_data)
    finally:
        store.close()
