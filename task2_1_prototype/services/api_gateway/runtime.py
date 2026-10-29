"""Session-owned WP2 -> WP3 -> WP4 integration, with isolated local persistence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock

from task2_1_prototype.contracts.models import parse_timestamp, utc_text
from task2_1_prototype.services.connector_service.pipeline import load_batch, connectors_for, strict_json
from task2_1_prototype.services.connector_service.schema import SourceEnvelope
from task2_1_prototype.services.connector_service.validation import IngestSession, Rejected, canonical, PROJECT
from task2_1_prototype.services.semantic_object_service.mapping_engine import MappingEngine
from task2_1_prototype.services.semantic_object_service.models import validate_raw
from task2_1_prototype.services.semantic_object_service.storage import SQLiteStore
from task2_1_prototype.services.spatiotemporal_model_service import GraphManager, SQLiteGraphStore
from task2_1_prototype.services.spatiotemporal_model_service.pipeline import RULES_FILE, read_lines


def source_record(raw):
    """Rebuild the preserved source envelope; never trust client-supplied decoded values/IDs."""
    raw = validate_raw(raw)
    ctx = deepcopy(raw["raw_context"])
    meta = ctx.pop("_ingest")
    record = dict(sequence=meta["source_sequence"], source_system=raw["source_system"], protocol=raw["protocol"],
                  modality=raw["modality"], channel=meta["channel"], timestamp=meta["source_timestamp"],
                  object_hint=raw["object_hint"], payload=raw["raw_payload"]["source_payload"], context=ctx)
    if meta.get("event_key") is not None:
        record["event_key"] = meta["event_key"]
    return record


def resolve_config(workspace_dir, options):
    workspace = Path(workspace_dir).resolve()
    marker = workspace / "gateway_config.json"
    legacy = (PROJECT / "minimal_factory").resolve()
    if workspace == PROJECT or workspace.is_relative_to(legacy) or legacy.is_relative_to(workspace):
        raise ValueError("gateway workspace must be separate from the legacy project")
    for parent in (workspace, *workspace.parents):
        if any((parent / name).exists() for name in ("transport_records.jsonl", "raw_ingest_messages.jsonl",
                                                    "semantic_objects.jsonl", "spatiotemporal_summary.json")):
            raise ValueError("gateway cannot write inside WP1-WP4 artifact directories")
    saved = strict_json(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    config = dict(saved)
    for key, value in options.items():
        if value is not None:
            config[key] = float(value) if key == "bootstrap_until" else str(Path(value).resolve())
    for key in ("source_dir", "catalog_file"):
        if key not in config:
            raise ValueError(f"{key} is required for a new gateway workspace")
    config.setdefault("rules_file", str(RULES_FILE.resolve()))
    for key in ("source_dir", "catalog_file", "bootstrap_raw_file"):
        if config.get(key):
            path = Path(config[key]).resolve()
            folder = path if key == "source_dir" else path.parent
            if workspace == folder or workspace.is_relative_to(folder) or folder.is_relative_to(workspace):
                raise ValueError("gateway workspace and input artifacts must not overlap")
    if saved and config != saved:
        raise ValueError("gateway configuration is fixed; use a new workspace for another session")
    if not saved and workspace.exists() and any(workspace.iterdir()):
        raise ValueError("new gateway workspace must be empty")
    return workspace, config


class GatewayRuntime:
    def __init__(self, workspace_dir, **options):
        self.workspace, self.config = resolve_config(workspace_dir, options)
        self.lock = RLock()
        self.closed = False
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "gateway_config.json").write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
        self.source = Path(self.config["source_dir"])
        self.demo_config, bindings, batch = load_batch(self.source)
        self.raw_cache = {}
        self.session = IngestSession(batch, self.source, bindings, self._accepted, self._rejected)
        self.connectors = connectors_for(self.session, self.source / "demo_erp.sqlite3")
        self.store = SQLiteStore(self.workspace / "semantic" / "semantic_objects.sqlite3")
        self.graph_store = None
        try:
            self.engine = MappingEngine(self.store)
            self.store.connection.execute("CREATE TABLE IF NOT EXISTS gateway_requests (event_id TEXT PRIMARY KEY, request_json TEXT, response_json TEXT)")
            self.graph_store = SQLiteGraphStore(self.workspace / "graph" / "spatiotemporal_model.sqlite3")
            catalog = read_lines(self.config["catalog_file"])
            rules = strict_json(Path(self.config["rules_file"]).read_text(encoding="utf-8"))
            self.manager = GraphManager(catalog, store=self.graph_store, rules=rules)
            # Restore acquisition identities and reconcile committed semantic events after restart.
            for row in self.store.connection.execute("SELECT raw_json FROM processed_messages ORDER BY rowid").fetchall():
                self._validate(json.loads(row[0]))
            self.manager.replay(self._history_after(0))
            if not self.store.counts()["processed_messages"] and self.config.get("bootstrap_raw_file"):
                rows = read_lines(self.config["bootstrap_raw_file"])
                cutoff = self.config.get("bootstrap_until")
                if cutoff is not None:
                    rows = [r for r in rows if r["raw_context"].get("clock") != "simulation" or r["raw_context"]["sim_time_s"] <= cutoff]
                self.ingest(rows)
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            if self.graph_store is not None:
                self.graph_store.close()
            self.store.close()

    def _accepted(self, raw):
        self.raw_cache[raw["message_id"]] = raw

    def _rejected(self, diagnostic):
        with (self.workspace / "rejected_messages.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(canonical(diagnostic) + "\n")

    def _validate(self, value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        is_raw = isinstance(value, dict) and "message_id" in value
        raw = validate_raw(value) if is_raw else None
        try:
            record = source_record(raw) if is_raw else value
        except (KeyError, TypeError) as exc:
            raise Rejected("RAW_PROVENANCE", "Raw input requires preserved WP2 source_payload and _ingest provenance") from exc
        # The WP2 connector rechecks schema, identities, PNG assets and PLC CRC/decoding.
        normalized = SourceEnvelope.model_validate(record).model_dump(mode="json", exclude_none=True)
        protocol = normalized["protocol"]
        identity = [self.session.batch_id, normalized["source_system"], protocol, normalized["channel"],
                    normalized.get("event_key") or normalized["sequence"]]
        message_id = "MSG_" + hashlib.sha256(canonical(identity).encode()).hexdigest()
        rejection_count = len(self.session.rejections)
        result = self.connectors[protocol].ingest(normalized)
        if len(self.session.rejections) != rejection_count:
            last = self.session.rejections[-1]
            raise Rejected(last["code"], last["reason"])
        candidate = result.model_dump(mode="json") if result is not None else self.raw_cache[message_id]
        if raw is not None and canonical(candidate) != canonical(raw):
            raise Rejected("RAW_PROVENANCE", "RawIngestMessage differs from its independently revalidated WP2 source")
        if candidate["raw_context"]["_ingest"]["resolved_ids"]["object_id"] not in self.manager.catalog:
            raise Rejected("CATALOG", "register identities in a new session before ingesting unknown objects")
        return candidate

    def _history_after(self, cursor):
        return [json.loads(r[0]) for r in self.store.connection.execute(
            "SELECT object_json FROM semantic_state_history WHERE history_id>? ORDER BY history_id", (cursor,))]

    def ingest(self, body):
        """Batch failures roll back both local stores and session IDs; serialized per gateway."""
        with self.lock:
            values = body if isinstance(body, list) else [body]
            if not values or len(values) > 5000:
                raise ValueError("ingest requires 1 to 5000 messages")
            # A local checkpoint, not a distributed transaction. Restart reconciliation uses semantic history.
            backups = []
            saved = {key: deepcopy(getattr(self.session, key)) for key in ("seen", "counts", "by_protocol", "by_modality", "by_connector")}
            cached = self.raw_cache.copy()
            rejected_before = len(self.session.rejections)
            try:
                for store in (self.store, self.graph_store):
                    copy = sqlite3.connect(":memory:")
                    store.connection.backup(copy)
                    backups.append((store, copy))
                cursor = self.store.connection.execute("SELECT COALESCE(MAX(history_id),0) FROM semantic_state_history").fetchone()[0]
                messages = [self._validate(value) for value in values]
                results = self.engine.map_batch(messages)
                self.manager.replay(self._history_after(cursor))
                return results if isinstance(body, list) else results[0]
            except Exception as exc:
                for store, copy in backups:
                    copy.backup(store.connection)
                self.raw_cache = cached
                for key, value in saved.items():
                    setattr(self.session, key, value)
                self.session.counts["rejected"] += len(self.session.rejections) - rejected_before
                self.manager = GraphManager(store=self.graph_store)
                if len(self.session.rejections) == rejected_before:
                    self.session.reject("Gateway", exc, values[0])
                raise
            finally:
                for _, copy in backups:
                    copy.close()

    def map_batch(self, values):
        """WP3 router compatibility adapter; writes still go through all three services."""
        return self.ingest(list(values))

    def semantic_mapping(self, value):
        return self.ingest(value)

    def impact(self, object_id, as_of=None, direction="auto", clock="simulation"):
        with self.lock:
            result = self.manager.impact(object_id, as_of, direction, clock)
            cutoff = result["as_of"]
            events = []
            if clock == "simulation":
                for risk in self.graph_store.risk_history():
                    if object_id not in risk["path"] or risk["start_s"] > cutoff:
                        continue
                    item = deepcopy(risk)
                    if item["end_s"] is None or item["end_s"] > cutoff:
                        item.update(end_s=None, status="active", active=True)
                        item.pop("closure_reason", None)
                    events.append(item)
            result["impact_events"] = events
            return result

    def trigger(self, request):
        with self.lock:
            data = request.model_dump(mode="json")
            key, encoded = data["event_id"], canonical(data)
            old = self.store.connection.execute("SELECT request_json,response_json FROM gateway_requests WHERE event_id=?", (key,)).fetchone()
            if old:
                if old[0] != encoded:
                    raise ValueError("event_id already has different content")
                return json.loads(old[1])
            seconds, oid = data["sim_time_s"], data["object_id"]
            if seconds < self.manager.current_second:
                raise ValueError("online events must not precede the current simulation time; use an isolated replay session")
            if oid not in self.manager.catalog:
                raise KeyError(oid)
            timestamp = utc_text(parse_timestamp(self.manager.epoch) + timedelta(seconds=seconds))
            sequence = 100000 + int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
            obj = self.manager.catalog[oid]
            if data["kind"] == "business_update":
                if obj["object_type"] not in ("order", "work_order") or not data["status"]:
                    raise ValueError("business_update needs an order/work_order and a status")
                context = {k + "_id": obj["context"][k] for k in ("order", "work_order", "factory", "enterprise") if k in obj["context"]}
                context[obj["object_type"] + "_id"] = oid
                records = [dict(source_system="MES" if obj["object_type"] == "work_order" else "ERP", protocol="REST_API",
                                modality="structured_record", channel="/gateway/business", payload={"status_code": 200, "body": {"status": data["status"]}},
                                object_hint=dict(raw_id=oid, suggested_type=obj["object_type"], domain_hint=obj["domain"], level_hint=obj["level"]), context=context)]
            else:
                active = self.manager.relations(source=oid, relation="executes", active=True, as_of=seconds)
                if len(active) != 1:
                    raise ValueError("device fault/recovery requires exactly one active execution assignment")
                process = active[0]["target"]
                # Use dispatch capture context, never invent a work order in a PLC frame or graph node.
                template = next((r for r in read_lines(self.source / "transport_records.jsonl") if r["modality"] == "timeseries_signal"
                                 and r["context"].get("equipment_id") == oid and r["context"].get("process_id") == process), None)
                if template is None or not template["context"]["active_start_s"] <= seconds < template["context"]["active_end_s"]:
                    raise ValueError("event lies outside the captured device execution window")
                context = deepcopy(template["context"])
                device = next(d for d in self.demo_config["equipment"] if d["object_id"] == oid)
                fault = data["kind"] == "fault"
                signals = data["signals"] or (self.demo_config["fault"]["values"] if fault else device["normal"])
                code, severity = data["fault_code"], "ERROR" if fault else "INFO"
                line = f'[{timestamp}] [PLC-0x{device["address"]:02X}] [{severity}] Code {code}: {data["kind"]} on {oid} while executing {process} for {context["work_order_id"]} order {context["order_id"]}'
                log = {"log_message": line, "synthetic": True}
                if not fault:
                    log["state_action"] = "fault_cleared"
                base = dict(source_system="SIMPY_PLC", protocol="MQTT", object_hint=template["object_hint"], context=context)
                records = [dict(base, modality="timeseries_signal", channel=f"factory/{oid}/telemetry", payload=signals),
                           dict(base, modality="log_text", channel=f"factory/{oid}/logs", payload=log)]
            for index, record in enumerate(records):
                record.update(sequence=sequence + index, event_key=f"gateway/{key}/{index}", timestamp=timestamp)
                record["context"] = {**record["context"], "clock": "simulation", "simulation_epoch": self.manager.epoch,
                                     "run_id": self.manager.scope.split("/")[1], "sim_time_s": seconds, "sequence": sequence + index,
                                     "gateway_event_id": key, "synthetic": True}
                if data["kind"] == "business_update" and obj["object_type"] == "order":
                    # ERP current order states use the acquisition clock, as specified by WP3.
                    record["timestamp"] = utc_text(datetime.now(timezone.utc))
                    record["context"].update(clock="wall", timestamp_basis="acquisition_wall_clock", scheduled_sim_time_s=seconds)
                    record["context"].pop("sim_time_s", None)
            mapped = self.ingest(records)
            result = {"event_id": key, "pipeline": ["WP2", "WP3", "WP4"],
                      "objects": [self.store.latest(i) for i in sorted({o["object_id"] for o in mapped})],
                      "relations": self.manager.relations(as_of=seconds), "impact": self.impact(oid, seconds),
                      "source_records": records, "semantic_counts": self.store.counts(), "graph_counts": self.graph_store.counts()}
            self.store.connection.execute("INSERT INTO gateway_requests VALUES (?,?,?)", (key, encoded, canonical(result)))
            return result
