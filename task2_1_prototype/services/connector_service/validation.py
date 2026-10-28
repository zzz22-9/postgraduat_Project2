"""Acquisition integrity only: no semantic status or relationship inference."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PureWindowsPath
from threading import RLock
from collections import Counter

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image

from task2_1_prototype.config import IdRegistry
from task2_1_prototype.contracts.models import RawIngestMessage, parse_timestamp, utc_text

SCHEMA = Path(__file__).parents[2] / "contracts/schemas/RawIngestMessage.schema.json"
PROJECT = Path(__file__).parents[3]


class Rejected(ValueError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def safe_asset(root, relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise Rejected("ASSET_PATH", "asset path must be a nonempty relative path")
    windows = PureWindowsPath(relative)
    path = Path(relative.replace("\\", "/"))
    if windows.drive or windows.root or path.is_absolute() or ".." in path.parts or ":" in relative:
        raise Rejected("ASSET_PATH", "absolute, parent, drive and alternate-stream paths are forbidden")
    root = Path(root).resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise Rejected("ASSET_PATH", "asset escapes the batch root")
    if not resolved.is_file():
        raise Rejected("ASSET_MISSING", f"missing asset: {relative}")
    return resolved


def protect_database(path):
    path = Path(path).resolve()
    legacy = PROJECT / "minimal_factory"
    if path.is_relative_to(legacy.resolve()):
        raise Rejected("PROTECTED_DATABASE", "legacy minimal_factory databases cannot be connected")
    original = legacy / "data/erp.db"
    if path.exists() and original.exists() and path.samefile(original):
        raise Rejected("PROTECTED_DATABASE", "database is an alias of the original ERP database")
    if not path.is_file():
        raise Rejected("DATABASE_MISSING", "read-only database must already exist")
    return path


class Bindings:
    """Resolve registered IDs with a work-order-scoped process key."""
    def __init__(self, config, records, snapshot=None):
        self.config = deepcopy(config)
        self.registry = IdRegistry(config)
        for row in records:
            self.registry.bind(row["kind"], row["raw_id"], row["object_id"], row["work_order"])
        self.addresses = {e["address"]: e["object_id"] for e in config["equipment"]}
        self.work_orders = {w["work_order_id"]: w for w in (snapshot or {}).get("orders", [])}

    def resolve(self, kind, value, work_order=None):
        matches = [(k, v) for k, v in self.registry.entries.items()
                   if k[0] == kind and (value == k[2] or value == v)
                   and (kind != "process" or k[1] == work_order)]
        if not matches or len({v for _, v in matches}) != 1:
            raise Rejected("ID_CONTEXT", f"unregistered/ambiguous {kind} ID in parent context: {value}")
        return matches[0][1]

    def raw_work_order(self, value):
        for (kind, _, raw), target in self.registry.entries.items():
            if kind == "work_order" and value in (raw, target):
                return raw
        raise Rejected("ID_CONTEXT", "work-order identity is not registered")

    def validate(self, record):
        context = record["context"]
        if "_ingest" in context:
            raise Rejected("RESERVED_KEY", "context._ingest is reserved for connector provenance")
        if "sequence" in context and (type(context["sequence"]) is not int or context["sequence"] < 0):
            raise Rejected("CONTEXT", "context sequence must be a nonnegative integer")
        legacy = context.get("legacy_ids", {})
        if not isinstance(legacy, dict):
            raise Rejected("CONTEXT", "legacy_ids must be an object")
        field_data = record["modality"] != "structured_record"
        if field_data and any(not context.get(k) for k in ("equipment_id", "process_id", "work_order_id", "order_id")):
            raise Rejected("CONTEXT", "field data requires equipment/process/work_order/order scheduling context")
        parent = self.raw_work_order(context["work_order_id"]) if context.get("work_order_id") else None
        resolved = {}
        for kind in ("order", "work_order", "equipment", "process"):
            key = kind + "_id"
            raw_key = "operation_id" if kind == "process" else key
            value = context.get(key)
            original = legacy.get(raw_key)
            if not value and not original:
                continue
            if kind == "process" and not parent:
                raise Rejected("ID_CONTEXT", "process requires a registered parent work order")
            target = self.resolve(kind, value or original, parent)
            if original and self.resolve(kind, original, parent) != target:
                raise Rejected("ID_CONTEXT", f"legacy {kind} does not match canonical identity")
            resolved[key] = target
        if parent in self.work_orders:
            work = self.work_orders[parent]
            if resolved.get("order_id") and resolved["order_id"] != self.resolve("order", work["order_id"]):
                raise Rejected("ID_CONTEXT", "work order belongs to a different order")
            if resolved.get("process_id"):
                operations = [op for op in work["operations"]
                              if self.resolve("process", op["operation_id"], parent) == resolved["process_id"]]
                if len(operations) != 1 or (resolved.get("equipment_id") and
                    self.resolve("equipment", operations[0]["equipment_id"]) != resolved["equipment_id"]):
                    raise Rejected("ID_CONTEXT", "process/equipment does not match dispatch snapshot")
        hint = record["object_hint"]
        kind = hint["suggested_type"]
        if kind != "resource":
            hint_target = self.resolve(kind, hint["raw_id"], parent)
            if kind + "_id" in resolved and resolved[kind + "_id"] != hint_target:
                raise Rejected("ID_CONTEXT", "object hint disagrees with scheduling context")
            resolved["object_id"] = hint_target
        else:
            resolved["object_id"] = hint["raw_id"]
        if resolved.get("process_id"):
            resolved["process_instance_key"] = [resolved["work_order_id"], resolved["process_id"]]
        return resolved


class IngestSession:
    """Thread-safe, session-scoped idempotency; sinks can stream without storing output."""
    def __init__(self, batch_id, asset_root, bindings, accepted_sink=None, rejected_sink=None):
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError("stable batch_id is required")
        self.batch_id, self.asset_root, self.bindings = batch_id, Path(asset_root).resolve(), bindings
        self.accepted_sink, self.rejected_sink = accepted_sink, rejected_sink
        self.validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")),
                                             format_checker=FormatChecker())
        self.lock = RLock()
        self.seen = {}
        self.rejections = []
        self.counts = Counter()
        self.by_protocol, self.by_modality, self.by_connector = Counter(), Counter(), Counter()

    def reject(self, connector, error, original, locator=None):
        with self.lock:
            summary = repr(original)[:1200]
            sequence = original.get("sequence") if isinstance(original, dict) else None
            if type(sequence) is not int:
                sequence = None
            diagnostic = dict(connector=connector, code=getattr(error, "code", "INVALID_MESSAGE"),
                              reason=str(error)[:1200], sequence=sequence,
                              original_summary=summary, locator=locator)
            if self.rejected_sink:
                self.rejected_sink(diagnostic)
            self.rejections.append(diagnostic)
            self.counts["rejected"] += 1
        return None

    def modality_check(self, record):
        payload, modality = record["payload"], record["modality"]
        if modality == "image":
            path = safe_asset(self.asset_root, payload.get("image_ref"))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if payload.get("sha256") != digest:
                raise Rejected("IMAGE_HASH", "PNG SHA-256 mismatch")
            try:
                with Image.open(path) as image:
                    if image.format != "PNG":
                        raise ValueError("not a PNG")
                    image.verify()
                with Image.open(path) as image:
                    image.load()
            except (ValueError, OSError, SyntaxError) as exc:
                raise Rejected("IMAGE_DECODE", str(exc)) from exc
        if modality == "log_text" and not isinstance(payload.get("log_message"), str):
            raise Rejected("LOG_PAYLOAD", "original log_message is required")

    def accept(self, connector, record, payload, resolved):
        identity = [self.batch_id, record["source_system"], record["protocol"], record["channel"],
                    record.get("event_key") or record["sequence"]]
        message_id = "MSG_" + hashlib.sha256(canonical(identity).encode()).hexdigest()
        fingerprint = hashlib.sha256(canonical(record).encode()).hexdigest()
        context = deepcopy(record["context"])
        context["_ingest"] = dict(batch_id=self.batch_id, source_sequence=record["sequence"],
                                  channel=record["channel"], source_timestamp=record["timestamp"],
                                  event_key=record.get("event_key"), resolved_ids=resolved)
        candidate = dict(message_id=message_id, source_system=record["source_system"], protocol=record["protocol"],
                         modality=record["modality"], timestamp=utc_text(parse_timestamp(record["timestamp"])),
                         object_hint=record["object_hint"], raw_payload=payload, raw_context=context)
        message = RawIngestMessage.model_validate(candidate)
        self.validator.validate(message.model_dump(mode="json"))
        with self.lock:
            if message_id in self.seen:
                if self.seen[message_id] != fingerprint:
                    raise Rejected("IDENTITY_CONFLICT", "same source identity has different content")
                self.counts["duplicates"] += 1
                return None
            if self.accepted_sink:
                self.accepted_sink(message.model_dump(mode="json"))
            self.seen[message_id] = fingerprint
            self.counts["accepted"] += 1
            self.by_protocol[record["protocol"]] += 1
            self.by_modality[record["modality"]] += 1
            self.by_connector[connector] += 1
        return message

    def summary(self):
        with self.lock:
            return dict(counts={k: self.counts[k] for k in ("accepted", "duplicates", "rejected")},
                        by_protocol=dict(self.by_protocol), by_modality=dict(self.by_modality),
                        by_connector=dict(self.by_connector),
                        pydantic_validated=self.counts["accepted"], json_schema_validated=self.counts["accepted"])
