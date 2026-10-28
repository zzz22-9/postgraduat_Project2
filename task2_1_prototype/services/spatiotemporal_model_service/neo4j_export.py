"""Offline parameterized Cypher export only; no Neo4j network/driver side effects."""
from collections import defaultdict

from task2_1_prototype.services.relation_service.relation_engine import validated_edge, validated_object
from task2_1_prototype.services.semantic_object_service.models import json_text

LABELS = {name: name.upper() for name in ("executes", "belongs_to", "maps_to", "uses_resource", "located_in", "part_of", "managed_by")}


def export_neo4j(snapshot):
    nodes = [validated_object(o) for o in snapshot["nodes"]]
    identifiers = {o["object_id"] for o in nodes}
    rows = [{"object_id": o["object_id"], "object_type": o["object_type"], "domain": o["domain"], "level": o["level"],
             "state_json": json_text(o["state"]), "context_json": json_text(o["context"]), "timestamp": o["timestamp"]} for o in nodes]
    statements = [{"statement": "UNWIND $nodes AS row MERGE (n:SemanticObject {object_id: row.object_id}) SET n += row",
                   "parameters": {"nodes": rows}}]
    groups = defaultdict(list)
    for value in snapshot["edges"]:
        edge = validated_edge(value)
        if edge["source"] not in identifiers or edge["target"] not in identifiers:
            raise ValueError("cannot export dangling relation")
        groups[edge["relation"]].append({**{k: edge[k] for k in ("relation_id", "source", "target", "weight", "timestamp")},
                                         "context_json": json_text(edge["context"])})
    for relation, edges in sorted(groups.items()):
        statements.append({"statement": "UNWIND $edges AS row MATCH (s:SemanticObject {object_id: row.source}) "
                           "MATCH (t:SemanticObject {object_id: row.target}) "
                           f"MERGE (s)-[r:{LABELS[relation]} {{relation_id: row.relation_id}}]->(t) "
                           "SET r.weight=row.weight, r.timestamp=row.timestamp, r.context_json=row.context_json",
                           "parameters": {"edges": edges}})
    return {"mode": "offline_parameterized_export", "neo4j_connected": False, "statements": statements}
