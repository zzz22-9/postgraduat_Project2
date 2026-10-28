"""SQLite persistence. SQL is confined here so another storage adapter can implement the port."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Protocol

from task2_1_prototype.contracts.models import parse_timestamp, utc_text
from .models import MappingError, json_text


class SemanticStore(Protocol):
    """Transaction-scoped storage port used by MappingEngine; PostgreSQL can implement this port."""
    def transaction(self): ...
    def bind(self, key, value): ...
    def processed(self, message_id): ...
    def remember(self, raw, fingerprint, response): ...
    def latest(self, object_id): ...
    def tracks(self, object_id): ...
    def put_track(self, object_id, scope, track): ...
    def put_latest(self, obj): ...
    def before(self, object_id, scope, timestamp): ...
    def add_history(self, obj, raw, scope, late, applied): ...
    def cycle(self, object_id, scope, timestamp): ...
    def put_cycle(self, object_id, scope, timestamp, cycle): ...
    def window(self, object_id, scope, timestamp, size): ...
    def cycle_count(self, object_id, scope, timestamp): ...


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.lock = RLock()
        if str(path) != ":memory:":
            path = Path(path).resolve()
            legacy = Path(__file__).parents[3] / "minimal_factory"
            if path.name != "semantic_objects.sqlite3" or path.is_relative_to(legacy.resolve()):
                raise ValueError("use a dedicated semantic_objects.sqlite3 outside minimal_factory")
            for parent in path.parents:
                if (parent / "transport_records.jsonl").exists() or (parent / "raw_ingest_messages.jsonl").exists():
                    raise ValueError("semantic database must not be created inside WP1/WP2 source directories")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                # Inspect existing DB read-only before any journal/DDL can touch it.
                probe = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                try:
                    marker = probe.execute("SELECT value FROM semantic_metadata WHERE key='schema_version'").fetchone()
                    if marker != ('"WP3.1"',):
                        raise ValueError("not a compatible WP3 database")
                finally:
                    probe.close()
        self.connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS semantic_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS semantic_objects (
                object_id TEXT PRIMARY KEY, object_type TEXT, domain TEXT, level TEXT, timestamp TEXT, object_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS semantic_tracks (
                object_id TEXT, scope TEXT, track_json TEXT NOT NULL, PRIMARY KEY(object_id, scope));
            CREATE TABLE IF NOT EXISTS semantic_state_history (
                history_id INTEGER PRIMARY KEY, object_id TEXT, object_type TEXT, domain TEXT, level TEXT,
                timestamp TEXT, scope TEXT, message_id TEXT, late INTEGER, applied INTEGER, object_json TEXT NOT NULL,
                UNIQUE(message_id, object_id));
            CREATE INDEX IF NOT EXISTS history_lookup ON semantic_state_history(object_id, scope, timestamp, history_id);
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, raw_json TEXT NOT NULL, response_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signal_cycles (
                object_id TEXT, scope TEXT, timestamp TEXT, cycle_json TEXT NOT NULL, PRIMARY KEY(object_id, scope, timestamp));
        """)
        with self.transaction():
            self.bind("schema_version", "WP3.1")

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
        encoded = json_text(value)
        found = self.connection.execute("SELECT value FROM semantic_metadata WHERE key=?", (key,)).fetchone()
        if found and found[0] != encoded:
            raise MappingError("SCOPE_CONFLICT", f"database has a different {key}; use a separate output database")
        self.connection.execute("INSERT OR IGNORE INTO semantic_metadata VALUES (?,?)", (key, encoded))

    def processed(self, message_id):
        row = self.connection.execute("SELECT fingerprint,response_json FROM processed_messages WHERE message_id=?", (message_id,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def remember(self, raw, fingerprint, response):
        self.connection.execute("INSERT INTO processed_messages VALUES (?,?,?,?)",
                                (raw["message_id"], fingerprint, json_text(raw), json_text(response)))

    def latest(self, object_id):
        with self.lock:
            row = self.connection.execute("SELECT object_json FROM semantic_objects WHERE object_id=?", (object_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def tracks(self, object_id):
        return {r[0]: json.loads(r[1]) for r in self.connection.execute(
            "SELECT scope,track_json FROM semantic_tracks WHERE object_id=?", (object_id,))}

    def put_track(self, object_id, scope, track):
        self.connection.execute("INSERT OR REPLACE INTO semantic_tracks VALUES (?,?,?)", (object_id, scope, json_text(track)))

    def put_latest(self, obj):
        self.connection.execute("INSERT OR REPLACE INTO semantic_objects VALUES (?,?,?,?,?,?)",
                                (*[obj[k] for k in ("object_id", "object_type", "domain", "level", "timestamp")], json_text(obj)))

    def before(self, object_id, scope, timestamp):
        row = self.connection.execute("SELECT object_json FROM semantic_state_history WHERE object_id=? AND scope=? "
                                      "AND timestamp<=? ORDER BY timestamp DESC,history_id DESC LIMIT 1",
                                      (object_id, scope, timestamp)).fetchone()
        return json.loads(row[0]) if row else None

    def add_history(self, obj, raw, scope, late, applied):
        self.connection.execute("INSERT INTO semantic_state_history "
                                "(object_id,object_type,domain,level,timestamp,scope,message_id,late,applied,object_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (*[obj[k] for k in ("object_id", "object_type", "domain", "level", "timestamp")],
                                 scope, raw["message_id"], int(late), int(applied), json_text(obj)))

    def cycle(self, object_id, scope, timestamp):
        row = self.connection.execute("SELECT cycle_json FROM signal_cycles WHERE object_id=? AND scope=? AND timestamp=?",
                                      (object_id, scope, timestamp)).fetchone()
        return json.loads(row[0]) if row else None

    def put_cycle(self, object_id, scope, timestamp, cycle):
        self.connection.execute("INSERT OR REPLACE INTO signal_cycles VALUES (?,?,?,?)",
                                (object_id, scope, timestamp, json_text(cycle)))

    def window(self, object_id, scope, timestamp, size):
        return [json.loads(r[0]) for r in self.connection.execute(
            "SELECT cycle_json FROM signal_cycles WHERE object_id=? AND scope=? AND timestamp<=? ORDER BY timestamp DESC LIMIT ?",
            (object_id, scope, timestamp, size))]

    def cycle_count(self, object_id, scope, timestamp):
        return self.connection.execute("SELECT COUNT(*) FROM signal_cycles WHERE object_id=? AND scope=? AND timestamp<=?",
                                       (object_id, scope, timestamp)).fetchone()[0]

    def query(self, *, history=False, object_id=None, object_type=None, domain=None, level=None,
              start=None, end=None, scope=None):
        table = "semantic_state_history" if history else "semantic_objects"
        conditions, params = [], []
        for key, value in (("object_id", object_id), ("object_type", object_type), ("domain", domain), ("level", level)):
            if value is not None:
                conditions.append(key + "=?")
                params.append(value)
        if start and end and parse_timestamp(start) > parse_timestamp(end):
            raise ValueError("start must not be after end")
        for operator, value in ((">=", start), ("<=", end)):
            if value:
                conditions.append("timestamp" + operator + "?")
                params.append(utc_text(parse_timestamp(value)))
        if scope:
            if not history:
                raise ValueError("scope filter is only supported for history")
            conditions.append("scope=?")
            params.append(scope)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        ordering = "timestamp,history_id" if history else "object_id"
        with self.lock:
            return [json.loads(r[0]) for r in self.connection.execute(f"SELECT object_json FROM {table}{where} ORDER BY {ordering}", params)]

    def counts(self):
        with self.lock:
            return {name: self.connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                    for name in ("semantic_objects", "semantic_state_history", "processed_messages", "signal_cycles")}

    def cycles(self):
        with self.lock:
            return [{"object_id": row[0], "scope": row[1], **json.loads(row[2])} for row in self.connection.execute(
                "SELECT object_id,scope,cycle_json FROM signal_cycles ORDER BY object_id,scope,timestamp")]


def open_store(path, backend="SQLITE"):
    if backend != "SQLITE":
        raise NotImplementedError("PostgreSQL must implement SemanticStore; no live PostgreSQL adapter is shipped in WP3")
    return SQLiteStore(path)
