"""Dedicated local graph persistence and a storage port for later adapters."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Protocol

from task2_1_prototype.services.semantic_object_service.models import json_text


class GraphStore(Protocol):
    def transaction(self): ...
    def close(self): ...
    def bind(self, key, value): ...
    def metadata(self, key): ...
    def catalog(self): ...
    def save_catalog(self, objects): ...
    def append_event(self, event_id, obj, late): ...
    def events(self): ...
    def save_edges(self, edges): ...
    def save_snapshot(self, identifier, snapshot, revision): ...
    def snapshots(self): ...
    def sync_risks(self, impacts, seconds, states): ...
    def risk_history(self): ...
    def counts(self): ...


class SQLiteGraphStore:
    def __init__(self, path=":memory:"):
        self.lock = RLock()
        if str(path) != ":memory:":
            path = Path(path).resolve()
            legacy = Path(__file__).parents[3] / "minimal_factory"
            if path.name != "spatiotemporal_model.sqlite3" or path.is_relative_to(legacy.resolve()):
                raise ValueError("use a dedicated spatiotemporal_model.sqlite3 outside minimal_factory")
            for parent in path.parents:
                if any((parent / name).exists() for name in ("transport_records.jsonl", "raw_ingest_messages.jsonl", "semantic_objects.jsonl")):
                    raise ValueError("graph database must be outside WP1/WP2/WP3 input directories")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                probe = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                try:
                    if probe.execute("SELECT value FROM graph_metadata WHERE key='schema_version'").fetchone() != ('"WP4.1"',):
                        raise ValueError("not a WP4 graph database")
                finally:
                    probe.close()
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS graph_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS semantic_catalog (object_id TEXT PRIMARY KEY,object_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_events (arrival INTEGER PRIMARY KEY,event_id TEXT UNIQUE,object_json TEXT NOT NULL,late INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS relation_edges (relation_id TEXT PRIMARY KEY,edge_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS graph_snapshots (snapshot_id TEXT PRIMARY KEY,revision INTEGER,snapshot_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS impact_events (impact_id TEXT PRIMARY KEY,risk_json TEXT NOT NULL);
        """)
        with self.transaction():
            self.bind("schema_version", "WP4.1")

    def close(self):
        with self.lock:
            self.connection.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def bind(self, key, value):
        text = json_text(value)
        row = self.connection.execute("SELECT value FROM graph_metadata WHERE key=?", (key,)).fetchone()
        if row and row[0] != text:
            raise ValueError(f"graph metadata conflict: {key}")
        self.connection.execute("INSERT OR IGNORE INTO graph_metadata VALUES (?,?)", (key, text))

    def catalog(self):
        return [json.loads(r[0]) for r in self.connection.execute("SELECT object_json FROM semantic_catalog ORDER BY object_id")]

    def metadata(self, key):
        row = self.connection.execute("SELECT value FROM graph_metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_catalog(self, objects):
        self.bind("catalog", sorted(objects, key=lambda o: o["object_id"]))
        self.connection.executemany("INSERT OR IGNORE INTO semantic_catalog VALUES (?,?)", [(o["object_id"], json_text(o)) for o in objects])

    def append_event(self, event_id, obj, late):
        value = json_text(obj)
        old = self.connection.execute("SELECT object_json FROM model_events WHERE event_id=?", (event_id,)).fetchone()
        if old:
            if old[0] != value:
                raise ValueError("graph event identity has conflicting contents")
            return False
        self.connection.execute("INSERT INTO model_events(event_id,object_json,late) VALUES (?,?,?)", (event_id, value, int(late)))
        return True

    def events(self):
        return [(json.loads(r[0]), bool(r[1])) for r in self.connection.execute("SELECT object_json,late FROM model_events ORDER BY arrival")]

    def save_edges(self, edges):
        ids = {e["relation_id"] for e in edges}
        for row in self.connection.execute("SELECT relation_id FROM relation_edges").fetchall():
            if row[0] not in ids:
                self.connection.execute("DELETE FROM relation_edges WHERE relation_id=?", (row[0],))
        self.connection.executemany("INSERT OR REPLACE INTO relation_edges VALUES (?,?)", [(e["relation_id"], json_text(e)) for e in edges])

    def save_snapshot(self, identifier, snapshot, revision):
        self.connection.execute("INSERT OR REPLACE INTO graph_snapshots VALUES (?,?,?)", (identifier, revision, json_text(snapshot)))

    def snapshots(self):
        with self.lock:
            return [dict(snapshot_id=r[0], revision=r[1], clock=json.loads(r[2])["clock"], as_of=json.loads(r[2])["as_of"])
                    for r in self.connection.execute("SELECT * FROM graph_snapshots ORDER BY rowid")]

    def sync_risks(self, impacts, seconds, states):
        current = {item["impact_id"]: item for item in impacts}
        previous = {row[0]: json.loads(row[1]) for row in self.connection.execute("SELECT * FROM impact_events")}
        for key, item in current.items():
            old = previous.get(key)
            if old and old["end_s"] is not None:
                raise ValueError("a closed risk episode cannot reopen under the same fault identity")
            risk = dict(item, start_s=old["start_s"] if old else seconds, end_s=None,
                        status="active", assessment_kind="diagnostic_risk_only", mes_schedule_changed=False)
            self.connection.execute("INSERT OR REPLACE INTO impact_events VALUES (?,?)", (key, json_text(risk)))
        for key, old in previous.items():
            if key not in current and old["end_s"] is None:
                origin = old["path"][0]
                old.update(end_s=seconds, status="resolved", active=False,
                           closure_reason="fault_cleared" if states.get(origin) not in ("fault", "overload") else "execution_inactive")
                self.connection.execute("UPDATE impact_events SET risk_json=? WHERE impact_id=?", (json_text(old), key))

    def risk_history(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.connection.execute("SELECT risk_json FROM impact_events ORDER BY rowid")]

    def counts(self):
        with self.lock:
            return {name: self.connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                    for name in ("semantic_catalog", "model_events", "relation_edges", "graph_snapshots", "impact_events")}
