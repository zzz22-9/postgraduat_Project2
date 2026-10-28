"""Risk overlays and dependency traversal; never mutate SemanticObject business states."""
from copy import deepcopy
import hashlib
import json

BUSINESS = {"executes", "belongs_to", "maps_to"}
RISK = {"equipment": "fault", "process": "blocked_risk", "work_order": "delay_risk", "order": "delivery_risk"}


def analyze_impact(graph, object_id, direction="auto"):
    if object_id not in graph:
        raise KeyError(object_id)
    if direction not in ("auto", "upstream", "downstream"):
        raise ValueError("direction must be auto/upstream/downstream")
    origin = graph.nodes[object_id]["semantic"]
    if direction == "auto":
        direction = "upstream" if origin["object_type"] == "equipment" else "downstream"
    result = dict(object_id=object_id, direction=direction, assessment_kind="diagnostic_risk_only",
                  active_impacts=[], cycles_detected=[], semantic_states_modified=False)
    if direction == "downstream":
        queue, visited, found_edges = [object_id], set(), {}
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            candidates = []
            for source, _, _, data in graph.in_edges(current, keys=True, data=True):
                edge = data["relation"]
                if edge["relation"] in BUSINESS:
                    candidates.append((source, edge))
            for _, dest, _, data in graph.out_edges(current, keys=True, data=True):
                edge = data["relation"]
                if edge["relation"] in {"uses_resource", "located_in", "part_of", "managed_by"}:
                    candidates.append((dest, edge))
            for dest, edge in candidates:
                found_edges[edge["relation_id"]] = deepcopy(edge)
                if dest not in visited:
                    queue.append(dest)
        result["dependencies"] = {"object_ids": sorted(visited), "objects": [deepcopy(graph.nodes[n]["semantic"]) for n in sorted(visited)],
                                  "relations": [found_edges[k] for k in sorted(found_edges)], "includes_inactive_historical_assignments": True}
        return result
    if origin["object_type"] != "equipment" or origin["state"].get("status") not in ("fault", "overload") or origin["state"].get("fault_latched") is False:
        return result
    stack = [(object_id, [object_id], [])]
    expansions = 0
    while stack:
        current, path, relations = stack.pop()
        expansions += 1
        if expansions > 10000:
            raise ValueError("impact traversal limit exceeded; inspect graph branching")
        obj = graph.nodes[current]["semantic"]
        if obj["object_type"] == "order":
            fingerprint = json.dumps([origin["context"].get("batch_id"), path, origin["state"].get("fault_started_at")])
            codes = origin["state"].get("active_fault_codes", origin["state"].get("log", {}).get("fault_codes", []))
            steps = []
            for node_id in path:
                node = graph.nodes[node_id]["semantic"]
                steps.append(dict(object_id=node_id, object_type=node["object_type"], observed_status=node["state"].get("status"),
                                  risk_status=RISK.get(node["object_type"], "dependency_risk")))
            result["active_impacts"].append(dict(impact_id="RISK_" + hashlib.sha256(fingerprint.encode()).hexdigest(),
                                                 path=path, relations=deepcopy(relations), steps=steps,
                                                 fault_codes=[c for c in codes if c.lower() != "0x00"],
                                                 fault_started_at=origin["state"].get("fault_started_at"), active=True))
            continue
        for _, dest, _, data in graph.out_edges(current, keys=True, data=True):
            edge = data["relation"]
            if edge["relation"] not in BUSINESS or edge["context"].get("active") is not True or edge["weight"] <= 0:
                continue
            if dest in path:
                result["cycles_detected"].append(path + [dest])
                continue
            stack.append((dest, path + [dest], relations + [edge]))
    result["active_impacts"].sort(key=lambda item: item["path"])
    return result
