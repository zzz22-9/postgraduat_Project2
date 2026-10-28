"""Four local acquisition adapters. Network clients/connections are caller-owned inputs."""
from abc import ABC, abstractmethod
from contextlib import closing
from copy import deepcopy
import sqlite3

from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError

from task2_1_prototype.contracts.models import parse_timestamp
from task2_1_prototype.simulators.protocol_emulator import SIGNALS, decode_plc_frame, topic_matches
from .schema import SourceEnvelope
from .validation import Rejected, protect_database, safe_asset


class BaseConnector(ABC):
    protocols = ()

    def __init__(self, session):
        self.session = session

    def ingest(self, source, *, locator=None):
        try:
            record = SourceEnvelope.model_validate(source).model_dump(mode="json", exclude_none=True)
            if record["protocol"] not in self.protocols:
                raise Rejected("PROTOCOL", "protocol is not supported by this connector")
            resolved = self.session.bindings.validate(record)
            self.session.modality_check(record)
            payload = self.extract(record, resolved)
            # Original payload remains exact even when transport fields are decoded/unwrapped.
            if "source_payload" in payload:
                raise Rejected("RESERVED_KEY", "payload.source_payload is reserved")
            payload["source_payload"] = deepcopy(record["payload"])
            return self.session.accept(type(self).__name__, record, payload, resolved)
        except (Rejected, ValidationError, SchemaError, KeyError, TypeError, ValueError) as exc:
            return self.session.reject(type(self).__name__, exc, source, locator)

    def ingest_many(self, records):
        for record in records:
            message = self.ingest(record)
            if message is not None:
                yield message

    @abstractmethod
    def extract(self, record, resolved):
        raise NotImplementedError


class RestApiConnector(BaseConnector):
    protocols = ("REST_API",)

    def extract(self, record, resolved):
        payload = record["payload"]
        status = payload["status_code"]
        if type(status) is not int or not 200 <= status < 300:
            raise Rejected("HTTP_STATUS", "REST response is not successful")
        if not isinstance(payload["body"], dict):
            raise Rejected("REST_BODY", "one JSON business object is required per message")
        return deepcopy(payload["body"])

    def fetch(self, client_or_callback, path, metadata):
        """GET using a configured TestClient/http client or callback(path)->response."""
        response = (client_or_callback.get(path) if hasattr(client_or_callback, "get")
                    else client_or_callback(path))
        try:
            payload = {"status_code": response.status_code, "body": response.json()}
        except (ValueError, TypeError) as exc:
            return self.session.reject(type(self).__name__, Rejected("REST_JSON", str(exc)), metadata, path)
        return self.ingest({**metadata, "protocol": "REST_API", "channel": path, "payload": payload})


class DatabaseConnector(BaseConnector):
    protocols = ("SQLITE", "POSTGRESQL")
    TABLE_KEYS = {"orders": "order_id", "inventory": "resource_id", "organizations": "object_id"}

    def __init__(self, session, sqlite_path=None, connection_factory=None):
        super().__init__(session)
        self.sqlite_path = protect_database(sqlite_path) if sqlite_path else None
        self.connection_factory = connection_factory

    def read_rows(self, table, protocol="SQLITE"):
        if table not in self.TABLE_KEYS:
            raise Rejected("DATABASE_TABLE", "only orders/inventory/organizations may be read")
        # Identifiers come only from this hard-coded whitelist, never from arbitrary SQL input.
        query = f'SELECT * FROM "{table}" ORDER BY "{self.TABLE_KEYS[table]}"'
        if protocol == "SQLITE":
            if self.sqlite_path is None:
                raise Rejected("DATABASE_CONFIG", "SQLite path not configured")
            path = protect_database(self.sqlite_path)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.row_factory = sqlite3.Row
                return [dict(row) for row in connection.execute(query).fetchall()]
        if protocol != "POSTGRESQL" or self.connection_factory is None:
            raise Rejected("DATABASE_CONFIG", "POSTGRESQL requires an explicit DB-API connection_factory")
        connection = self.connection_factory()
        try:
            connection.set_session(readonly=True, autocommit=False)
            with closing(connection.cursor()) as cursor:
                cursor.execute(query)
                names = [column[0] for column in cursor.description]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def extract(self, record, resolved):
        payload = record["payload"]
        table = payload["table"]
        if table not in self.TABLE_KEYS or not isinstance(payload["row"], dict):
            raise Rejected("DATABASE_TABLE", "invalid table or database row")
        row = payload["row"]
        if self.TABLE_KEYS[table] not in row:
            raise Rejected("DATABASE_KEY", "database primary key is missing")
        if record["object_hint"]["raw_id"] != row[self.TABLE_KEYS[table]]:
            raise Rejected("DATABASE_KEY", "row primary key disagrees with object hint")
        return deepcopy(row)

    def snapshot(self, table, metadata_factory, protocol="SQLITE"):
        """Caller supplies timestamp and stable poll/event key; each read closes the connection."""
        for row in self.read_rows(table, protocol):
            metadata = metadata_factory(deepcopy(row))
            result = self.ingest({**metadata, "protocol": protocol, "payload": {"table": table, "row": row}})
            if result is not None:
                yield result

    def ingest_verified_row(self, source):
        """Verify a saved DB transport record against the current read-only source table."""
        try:
            table, row = source["payload"]["table"], source["payload"]["row"]
            if row not in self.read_rows(table, source["protocol"]):
                raise Rejected("DATABASE_SNAPSHOT", "saved row differs from the current database snapshot")
        except (Rejected, KeyError, TypeError) as exc:
            return self.session.reject(type(self).__name__, exc, source)
        return self.ingest(source)


class MqttConnector(BaseConnector):
    protocols = ("MQTT",)

    def __init__(self, session, topic_filters=("mes/#", "factory/#")):
        super().__init__(session)
        self.topic_filters = tuple(topic_filters)
        for pattern in self.topic_filters:
            parts = pattern.split("/")
            if not pattern or any(("#" in p and (p != "#" or i != len(parts)-1))
                                  or ("+" in p and p != "+") for i, p in enumerate(parts)):
                raise ValueError("invalid MQTT topic filter")
        self.queues = []

    def extract(self, record, resolved):
        topic = record["channel"]
        if "+" in topic or "#" in topic or not any(topic_matches(p, topic) for p in self.topic_filters):
            raise Rejected("MQTT_TOPIC", "topic does not match connector subscriptions")
        if record["modality"] == "timeseries_signal" and any(
            type(record["payload"].get(k)) not in (int, float) for k in SIGNALS
        ):
            raise Rejected("SIGNAL_FIELDS", "telemetry requires four numeric signals")
        return deepcopy(record["payload"])

    def subscribe(self, broker):
        self.queues.extend(broker.subscribe(pattern) for pattern in self.topic_filters)
        return self

    def receive(self, delivery):
        try:
            if delivery["topic"] != delivery["payload"]["channel"]:
                raise Rejected("MQTT_TOPIC", "delivered topic differs from source channel")
            return self.ingest(delivery["payload"])
        except (Rejected, KeyError, TypeError) as exc:
            return self.session.reject(type(self).__name__, exc, delivery)

    def drain(self):
        for queue in self.queues:
            while queue:
                result = self.receive(queue.popleft())
                if result is not None:
                    yield result


class OpcUaAndPlcConnector(BaseConnector):
    protocols = ("OPC_UA", "PLC_FRAME")

    def extract(self, record, resolved):
        if record["modality"] != "timeseries_signal":
            raise Rejected("MODALITY", "OPC/PLC branch requires timeseries_signal")
        payload = record["payload"]
        if record["protocol"] == "OPC_UA":
            required = {"NodeId", "Value", "StatusCode", "SourceTimestamp"}
            if not required <= payload.keys():
                raise Rejected("OPC_FIELDS", "OPC node requires NodeId/Value/StatusCode/SourceTimestamp")
            if payload["StatusCode"] != "Good" and not (type(payload["StatusCode"]) is int and payload["StatusCode"] == 0):
                raise Rejected("OPC_QUALITY", "OPC node quality is not Good")
            if parse_timestamp(payload["SourceTimestamp"]) != parse_timestamp(record["timestamp"]):
                raise Rejected("OPC_TIMESTAMP", "node and capture timestamps disagree")
            node = payload["NodeId"]
            if not isinstance(node, str) or not node.startswith("ns=2;s=") or "." not in node:
                raise Rejected("OPC_NODE", "unsupported named node format")
            device, metric = node[7:].rsplit(".", 1)
            if (device != resolved.get("equipment_id") or metric not in SIGNALS or record["channel"] != node
                    or type(payload["Value"]) not in (int, float)):
                raise Rejected("OPC_NODE", "node device/metric/value does not match scheduling context")
            return {**deepcopy(payload), metric: payload["Value"]}
        try:
            frame = bytes.fromhex(payload["frame_hex"])
            decoded = decode_plc_frame(frame, self.session.bindings.addresses)
        except (KeyError, ValueError, TypeError) as exc:
            raise Rejected("PLC_FRAME", str(exc)) from exc
        device = self.session.bindings.addresses[decoded["address"]]
        if device != resolved.get("equipment_id") or record["channel"] != f'plc://0x{decoded["address"]:02X}':
            raise Rejected("PLC_CONTEXT", "decoded address disagrees with capture/scheduling context")
        if "binary_file" in payload:
            binary = safe_asset(self.session.asset_root, payload["binary_file"])
            offset, length = payload["offset"], payload["length"]
            if type(offset) is not int or offset < 0 or type(length) is not int or length != 18:
                raise Rejected("PLC_BINARY", "invalid binary offset/length")
            with binary.open("rb") as stream:
                stream.seek(offset)
                if stream.read(length) != frame:
                    raise Rejected("PLC_BINARY", "hex frame differs from binary file bytes")
        return dict(**decoded["signals"], control_cmd=decoded["control"], heartbeat_seq=decoded["heartbeat"],
                    device_address=decoded["address"], function_code=decoded["function_code"],
                    plc_raw_frame=frame.hex(" ").upper())

    def read_node(self, store, node_id, metadata):
        try:
            node = store.read(node_id)
        except KeyError as exc:
            return self.session.reject(type(self).__name__, Rejected("OPC_MISSING", str(exc)), metadata, node_id)
        return self.ingest({**metadata, "protocol": "OPC_UA", "channel": node_id, "payload": node})

    def read_frame(self, frame, metadata):
        payload = {"frame_hex": frame.hex(" ") if isinstance(frame, bytes) else frame}
        return self.ingest({**metadata, "protocol": "PLC_FRAME", "payload": payload})
