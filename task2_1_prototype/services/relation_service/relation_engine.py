"""Evidence-based seven-field relations. No state mutation or numeric process-name guessing."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from task2_1_prototype.contracts.models import SemanticObject, RelationEdge, parse_timestamp, require_json, utc_text

SCHEMAS = Path(__file__).parents[2] / "contracts/schemas"
VALIDATORS = {name: Draft202012Validator(json.loads((SCHEMAS / (name + ".schema.json")).read_text(encoding="utf-8")),
                                        format_checker=FormatChecker()) for name in ("SemanticObject", "RelationEdge")}


class RelationError(ValueError):
    pass


def validated_object(value):
    if isinstance(value, SemanticObject):
        value = value.model_dump(mode="json")
    result = SemanticObject.model_validate(value).model_dump(mode="json")
    VALIDATORS["SemanticObject"].validate(result)
    return result


def validated_edge(value):
    result = RelationEdge.model_validate(value).model_dump(mode="json")
    VALIDATORS["RelationEdge"].validate(result)
    return result


def finite_time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise RelationError("as_of must be a finite nonnegative simulation second")
    return float(value)


def relation_inference(objects: list[dict | SemanticObject], context: dict | None = None) -> list[dict]:
    if not isinstance(objects, list):
        raise RelationError("objects must be a list of seven-field SemanticObjects")
    context = deepcopy(context) if context is not None else {}
    require_json(context)
    nodes = {}
    for value in objects:
        obj = validated_object(value)
        if obj["object_id"] in nodes:
            raise RelationError("duplicate object_id in snapshot")
        nodes[obj["object_id"]] = obj
    batches = {o["context"]["batch_id"] for o in nodes.values() if o["context"].get("batch_id")}
    if len(batches) > 1:
        raise RelationError("mixed batches require separate graph namespaces")
    batch = next(iter(batches), "explicit_snapshot")
    seconds = context.get("as_of")
    if seconds is not None:
        seconds = finite_time(seconds)
    epoch = context.get("simulation_epoch")
    if "timestamp" in context:
        timestamp = utc_text(parse_timestamp(context["timestamp"]))
    elif seconds is not None and epoch:
        timestamp = utc_text(parse_timestamp(epoch) + timedelta(seconds=seconds))
    else:
        sim = [o["timestamp"] for o in nodes.values() if o["context"].get("clock_scope", "").startswith("simulation/")]
        timestamp = max((utc_text(parse_timestamp(t)) for t in (sim or [o["timestamp"] for o in nodes.values()])), default=None)
    if not nodes:
        return []
    windows = context.get("execution_windows", {})
    edges = {}

    def target(identifier, kind=None, level=None, organization=False):
        obj = nodes.get(identifier)
        if obj is None:
            raise RelationError(f"explicit relation target does not exist: {identifier}")
        if kind and obj["object_type"] != kind or level and obj["level"] != level:
            raise RelationError(f"wrong target type/level: {identifier}")
        if organization and obj["context"].get("resource_kind") != "organization":
            raise RelationError(f"target is not an organization: {identifier}")
        return obj

    def activity(obj):
        if obj["object_type"] == "process" and obj["object_id"] in windows and seconds is not None:
            intervals = windows[obj["object_id"]]
            for interval in intervals:
                start = finite_time(interval["start_s"])
                end = interval.get("end_s")
                if end is not None and finite_time(end) < start:
                    raise RelationError("execution window ends before it starts")
                if interval["work_order"] != obj["context"].get("work_order") or interval["equipment"] != obj["context"].get("equipment"):
                    raise RelationError("execution window has a different process assignment")
                if start <= seconds and (end is None or seconds < end):
                    return True, interval.get("phase", "normal")
            return False, "completed" if any(i.get("end_s") is not None and i["end_s"] <= seconds for i in intervals) else "not_started"
        status = obj["state"].get("status")
        active = status in ("running", "fault", "overload", "warning")
        return active, "completed" if status == "completed" else obj["context"].get("phase", "normal") if active else "not_started"

    def add(source, relation, dest, active=True, phase="structural", evidence="semantic_context", **details):
        target(source)
        target(dest)
        key = (source, relation, dest)
        identifier = "REL_" + hashlib.sha256(json.dumps([batch, *key], ensure_ascii=False).encode()).hexdigest()
        ctx = {"batch_id": batch, "active": bool(active), "phase": phase, "evidence_source": evidence,
               "clock_scope": context.get("clock_scope", "selected_semantic_snapshot"), **details}
        if seconds is not None:
            ctx["as_of_s"] = seconds
        edge = validated_edge(dict(relation_id=identifier, source=source, relation=relation, target=dest,
                                   weight=1.0 if active else 0.0, timestamp=timestamp, context=ctx))
        if key in edges and edges[key] != edge:
            raise RelationError("conflicting evidence for the same relation")
        edges[key] = edge

    for obj in nodes.values():
        oid, kind, ctx = obj["object_id"], obj["object_type"], obj["context"]
        if kind == "process":
            work, device = ctx.get("work_order"), ctx.get("equipment")
            if ctx.get("process_instance_key") != [work, oid] or not work or not device:
                raise RelationError("process requires an explicit [work_order, process] key and equipment")
            parent = target(work, "work_order")
            target(device, "equipment")
            if ctx.get("order") and parent["context"].get("order") != ctx["order"]:
                raise RelationError("process and work order disagree on order identity")
            active, phase = activity(obj)
            details = {"work_order": work, "process_instance_key": [work, oid]}
            add(device, "executes", oid, active, phase, **details)
            add(oid, "belongs_to", work, active, phase, **details)
        elif kind == "work_order":
            if ctx.get("order"):
                target(ctx["order"], "order")
                active, phase = activity(obj)
                add(oid, "maps_to", ctx["order"], active, phase, work_order=oid)
        if kind == "resource" and ctx.get("resource_kind") == "organization":
            if obj["level"] in ("line", "factory") and ctx.get("parent"):
                parent_level, relation = ("factory", "part_of") if obj["level"] == "line" else ("enterprise", "managed_by")
                target(ctx["parent"], "resource", parent_level, True)
                add(oid, relation, ctx["parent"])
        location = {"equipment": "line", "process": "line", "work_order": "factory", "order": "enterprise"}.get(kind)
        if location and ctx.get(location):
            target(ctx[location], "resource", location, True)
            add(oid, "located_in", ctx[location])
    for rule in context.get("organization_bindings", []):
        source, dest = target(rule["source"]), target(rule["target"], "resource", organization=True)
        # Explicit configuration is used only when semantic context has not already supplied this edge.
        if (source["object_id"], "located_in", dest["object_id"]) not in edges:
            add(source["object_id"], "located_in", dest["object_id"], evidence="configured_organization_rule", rule_id=rule["rule_id"])
    for rule in context.get("material_bindings", []):
        source, resource = target(rule["source"]), target(rule["target"], "resource")
        if source["object_type"] not in ("process", "work_order") or resource["context"].get("resource_kind") != "material":
            raise RelationError("material bindings require process/work_order and material resource")
        work = source["context"].get("work_order") if source["object_type"] == "process" else source["object_id"]
        if rule.get("work_order") != work or not rule.get("rule_id"):
            raise RelationError("configured BOM rule requires the exact parent work order and rule_id")
        active, phase = activity(source)
        add(source["object_id"], "uses_resource", resource["object_id"], active, phase,
            evidence="configured_bom_rule", rule_id=rule["rule_id"], work_order=work,
            configured_rule=deepcopy(rule), actual_consumption_asserted=False)
    return [edges[key] for key in sorted(edges)]
