"""NetworkX state graph with isolated clocks, event replay and retrospective time slices."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import hashlib
from threading import RLock

import networkx as nx

from task2_1_prototype.contracts.models import parse_timestamp, utc_text
from task2_1_prototype.services.relation_service.relation_engine import finite_time, relation_inference, validated_object
from task2_1_prototype.services.semantic_object_service.models import json_text
from .impact_engine import analyze_impact
from .storage import SQLiteGraphStore

STATIC_KEYS = ("batch_id", "enterprise", "factory", "line", "equipment", "process", "work_order", "order",
               "process_instance_key", "resource_kind", "parent", "legacy_ids")


class GraphManager:
    def __init__(self, objects=None, *, store=None, rules=None):
        self.store = store if store is not None else SQLiteGraphStore()
        self.lock = RLock()
        objects = self.store.catalog() if objects is None else objects
        catalog = [validated_object(o) for o in objects]
        if not catalog or len({o["object_id"] for o in catalog}) != len(catalog):
            raise ValueError("a nonempty unique semantic identity catalog is required")
        self.catalog = {o["object_id"]: o for o in catalog}
        epochs = {utc_text(parse_timestamp(o["context"]["simulation_epoch"])) for o in catalog if o["context"].get("simulation_epoch")}
        scopes = {o["context"]["clock_scope"] for o in catalog if o["context"].get("clock_scope", "").startswith("simulation/")}
        if len(epochs) != 1 or len(scopes) != 1:
            raise ValueError("one explicit simulation epoch/run scope is required per graph database")
        self.epoch, self.scope = next(iter(epochs)), next(iter(scopes))
        self.rules = deepcopy(rules if rules is not None else self.store.metadata("rules") or {})
        with self.store.transaction():
            self.store.save_catalog(catalog)
            self.store.bind("rules", self.rules)
            self.store.bind("simulation_scope", self.scope)
        self._reload()

    def _reference(self, obj, timestamp):
        result = {k: deepcopy(obj[k]) for k in ("object_id", "object_type", "domain", "level")}
        status = "not_started" if obj["object_type"] in ("process", "equipment", "work_order") else "reference_only"
        if obj["context"].get("resource_kind") == "organization":
            status = "configured"
        result["state"] = {"status": status}
        result["context"] = {key: deepcopy(obj["context"][key]) for key in STATIC_KEYS if key in obj["context"]}
        result["context"].update(clock_scope="reference", timestamp_basis="model_identity_projection",
                                  model_projection={"kind": "catalog_identity_only", "dynamic_state_observed": False,
                                                    "catalog_source_timestamp": obj["timestamp"]})
        result["timestamp"] = timestamp
        return validated_object(result)

    def _clock(self, obj):
        scope = obj["context"].get("clock_scope", "")
        if scope.startswith("simulation/"):
            if scope != self.scope:
                raise ValueError("event belongs to a different simulation scope")
            seconds = finite_time(obj["context"]["sim_time_s"])
            if parse_timestamp(self.epoch) + timedelta(seconds=seconds) != parse_timestamp(obj["timestamp"]):
                raise ValueError("semantic event time differs from epoch + sim_time_s")
            return "simulation"
        return "reference" if scope == "reference" else "acquisition_wall_clock"

    @staticmethod
    def _window_event(windows, obj):
        if obj["object_type"] != "process":
            return
        ctx, status = obj["context"], obj["state"].get("status")
        intervals = windows.setdefault(obj["object_id"], [])
        active = status in ("running", "fault", "warning", "overload")
        if active and (not intervals or intervals[-1]["end_s"] is not None):
            intervals.append(dict(start_s=ctx["sim_time_s"], end_s=None, work_order=ctx["work_order"],
                                  equipment=ctx["equipment"], phase=ctx.get("phase", "normal"),
                                  evidence_source="semantic_lifecycle", start_message_id=ctx.get("last_message_id")))
        elif not active and intervals and intervals[-1]["end_s"] is None:
            intervals[-1].update(end_s=ctx["sim_time_s"], end_message_id=ctx.get("last_message_id"))

    def _reload(self):
        self.events, self.tracks, self.windows = [], {}, {}
        self.current_second = 0.0
        self.graph = nx.MultiDiGraph()
        for oid, obj in self.catalog.items():
            self.graph.add_node(oid, semantic=self._reference(obj, self.epoch))
        for obj, late in self.store.events():
            self._apply_memory(obj, late, refresh=False)
        self._refresh(persist=False)

    def _relation_context(self, seconds, timestamp, clock, windows):
        return {**deepcopy(self.rules), "as_of": seconds if clock == "simulation" else None,
                "timestamp": timestamp, "simulation_epoch": self.epoch,
                "clock_scope": self.scope if clock == "simulation" else clock,
                "execution_windows": windows if clock == "simulation" else {}}

    def _refresh(self, persist):
        timestamp = utc_text(parse_timestamp(self.epoch) + timedelta(seconds=self.current_second))
        edges = relation_inference([data["semantic"] for _, data in self.graph.nodes(data=True)],
                                   self._relation_context(self.current_second, timestamp, "simulation", self.windows))
        # Node attributes are updated individually; relations are reconciled by stable identity.
        wanted = {e["relation_id"] for e in edges}
        for source, target, key in list(self.graph.edges(keys=True)):
            if key not in wanted:
                self.graph.remove_edge(source, target, key)
        for edge in edges:
            self.graph.add_edge(edge["source"], edge["target"], key=edge["relation_id"], relation=edge)
        if persist:
            self.store.save_edges(edges)
            impacts, states = [], {}
            for oid, data in self.graph.nodes(data=True):
                obj = data["semantic"]
                if obj["object_type"] == "equipment":
                    impacts.extend(analyze_impact(self.graph, oid)["active_impacts"])
                    states[oid] = obj["state"].get("status")
            self.store.sync_risks(impacts, self.current_second, states)

    @staticmethod
    def _topology_signature(obj):
        return (obj["state"].get("status"), obj["state"].get("fault_latched"), obj["state"].get("fault_started_at"),
                tuple(obj["state"].get("active_fault_codes", [])), tuple((key, json_text(obj["context"].get(key))) for key in STATIC_KEYS))

    def _apply_memory(self, obj, late, refresh=True):
        self.events.append((deepcopy(obj), late))
        if late:
            return
        oid, clock = obj["object_id"], self._clock(obj)
        self.tracks[(oid, clock)] = deepcopy(obj)
        if clock != "simulation":
            return
        self.current_second = max(self.current_second, obj["context"]["sim_time_s"])
        self._window_event(self.windows, obj)
        previous = self.graph.nodes[oid]["semantic"]
        changed = self._topology_signature(previous) != self._topology_signature(obj)
        self.graph.nodes[oid]["semantic"] = deepcopy(obj)
        if refresh and changed:
            self._refresh(persist=True)

    def apply_event(self, value):
        obj = validated_object(value)
        obj["timestamp"] = utc_text(parse_timestamp(obj["timestamp"]))
        oid = obj["object_id"]
        with self.lock:
            if oid not in self.catalog:
                raise ValueError("register a complete semantic identity catalog before streaming state updates")
            base = self.catalog[oid]
            for field in ("object_type", "domain", "level"):
                if base[field] != obj[field]:
                    raise ValueError("event changed an object's fixed type/domain/level")
            if obj["context"].get("batch_id") != base["context"].get("batch_id"):
                raise ValueError("event belongs to another batch")
            if obj["object_type"] == "process" and obj["context"].get("process_instance_key") != base["context"].get("process_instance_key"):
                raise ValueError("process event changed its work-order-scoped identity")
            clock = self._clock(obj)
            previous = self.tracks.get((oid, clock))
            late = bool(obj["context"].get("history", {}).get("late")) or bool(previous and obj["timestamp"] < previous["timestamp"])
            context = obj["context"]
            identity = [oid, clock, context.get("history", {}).get("message_id", context.get("last_message_id", obj["timestamp"])), context.get("source_sequence")]
            event_id = hashlib.sha256(json_text(identity).encode()).hexdigest()
            try:
                with self.store.transaction():
                    if not self.store.append_event(event_id, obj, late):
                        return False
                    self._apply_memory(obj, late)
                return True
            except BaseException:
                self._reload()
                raise

    def replay(self, events):
        return sum(self.apply_event(event) for event in events)

    def _cut(self, as_of, clock):
        if clock == "simulation":
            seconds = self.current_second if as_of is None else finite_time(float(as_of) if isinstance(as_of, str) else as_of)
            return seconds, utc_text(parse_timestamp(self.epoch) + timedelta(seconds=seconds))
        if clock != "acquisition_wall_clock":
            raise ValueError("clock must be simulation or acquisition_wall_clock")
        latest = max((obj["timestamp"] for obj, late in self.events if not late and self._clock(obj) == clock), default=self.epoch)
        return None, utc_text(parse_timestamp(as_of if as_of is not None else latest))

    def snapshot(self, as_of=None, *, clock="simulation", save=False):
        with self.lock:
            seconds, timestamp = self._cut(as_of, clock)
            selected = {oid: self._reference(obj, timestamp) for oid, obj in self.catalog.items()}
            windows = {}
            # Stable event-time ordering; arrival order breaks ties after WP3 has fused same-time observations.
            ordered = sorted(enumerate(self.events), key=lambda pair: (pair[1][0]["timestamp"], pair[0]))
            for _, (obj, late) in ordered:
                if late or self._clock(obj) != clock:
                    continue
                if clock == "simulation":
                    self._window_event(windows, obj)
                if obj["timestamp"] <= timestamp:
                    selected[obj["object_id"]] = deepcopy(obj)
            edges = relation_inference(list(selected.values()), self._relation_context(seconds, timestamp, clock, windows))
            graph = nx.MultiDiGraph()
            for oid, obj in selected.items():
                graph.add_node(oid, semantic=obj)
            for edge in edges:
                graph.add_edge(edge["source"], edge["target"], key=edge["relation_id"], relation=edge)
            overlays = []
            for oid, obj in selected.items():
                if obj["object_type"] == "equipment":
                    overlays.extend(analyze_impact(graph, oid)["active_impacts"])
            result = dict(clock=clock, as_of=seconds if clock == "simulation" else timestamp, timestamp=timestamp,
                          nodes=[selected[k] for k in sorted(selected)], edges=edges,
                          level_counts=dict(Counter(o["level"] for o in selected.values())),
                          object_type_counts=dict(Counter(o["object_type"] for o in selected.values())),
                          risk_overlays=overlays, revision=len(self.events), mode="local_networkx_sqlite",
                          reference_policy="catalog identities only; absent dynamic states are not_started/reference_only")
            if save:
                identifier = "SNAP_" + hashlib.sha256(json_text([clock, timestamp, len(self.events)]).encode()).hexdigest()
                result["snapshot_id"] = identifier
                with self.store.transaction():
                    self.store.save_snapshot(identifier, result, len(self.events))
            return result

    def impact(self, object_id, as_of=None, direction="auto", clock="simulation"):
        snapshot = self.snapshot(as_of, clock=clock)
        graph = nx.MultiDiGraph()
        for obj in snapshot["nodes"]:
            graph.add_node(obj["object_id"], semantic=obj)
        for edge in snapshot["edges"]:
            graph.add_edge(edge["source"], edge["target"], key=edge["relation_id"], relation=edge)
        result = analyze_impact(graph, object_id, direction)
        result.update(clock=clock, as_of=snapshot["as_of"], timestamp=snapshot["timestamp"])
        result["historical_impacts"] = [r for r in self.store.risk_history() if clock == "simulation" and
                                        r["end_s"] is not None and r["end_s"] <= snapshot["as_of"] and object_id in r["path"]]
        return result

    def relations(self, *, source=None, target=None, relation=None, active=None, as_of=None, clock="simulation"):
        return [e for e in self.snapshot(as_of, clock=clock)["edges"] if
                (source is None or e["source"] == source) and (target is None or e["target"] == target)
                and (relation is None or e["relation"] == relation) and (active is None or e["context"]["active"] == active)]
