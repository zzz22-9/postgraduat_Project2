from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import shutil
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from task2_1_prototype.contracts import RawIngestMessage
from task2_1_prototype.services.connector_service import (
    Bindings, DatabaseConnector, IngestSession, MqttConnector, OpcUaAndPlcConnector, RestApiConnector,
)
from task2_1_prototype.services.connector_service.pipeline import (
    connectors_for, ingest_stream, load_batch, read_jsonl, run_batch,
)
from task2_1_prototype.services.connector_service.validation import PROJECT, Rejected
from task2_1_prototype.simulators.legacy_bridge import load_legacy
from task2_1_prototype.simulators.protocol_emulator import (
    MemoryMqttBroker, OpcUaNodeStore, corrupt_frame, crc16, decode_plc_frame, encode_plc_frame,
)
from task2_1_prototype.simulators.simpy_factory import run_demo


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Wp2ConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.batch = cls.root / "wp1"
        cls.original_db = PROJECT / "minimal_factory/data/erp.db"
        cls.original_hash = sha(cls.original_db)
        run_demo(cls.batch)
        cls.config, cls.bindings, cls.batch_id = load_batch(cls.batch)
        cls.records = lines(cls.batch / "transport_records.jsonl")
        cls.source_hashes = {p.relative_to(cls.batch).as_posix(): sha(p) for p in cls.batch.rglob("*") if p.is_file()}

    def setUp(self):
        self.session = self.new_session()
        self.connectors = connectors_for(self.session, self.batch / "demo_erp.sqlite3")

    def new_session(self):
        return IngestSession(self.batch_id, self.batch, self.bindings)

    def record(self, protocol=None, modality=None):
        return deepcopy(next(r for r in self.records if (protocol is None or r["protocol"] == protocol)
                             and (modality is None or r["modality"] == modality)))

    def metadata(self, record):
        return {k: deepcopy(v) for k, v in record.items() if k != "payload"}

    def test_full_real_2202_payloads_contexts_and_both_contract_validators(self):
        self.assertEqual(len(self.records), 2202)
        results = list(ingest_stream(self.records, self.connectors, self.session, verify_database=True))
        self.assertEqual(len(results), 2202, self.session.rejections[:2])
        self.assertEqual(self.session.counts["rejected"], 0)
        for source, message in zip(self.records, results):
            raw = message.model_dump(mode="json")
            self.assertEqual(len(raw), 8)
            RawIngestMessage.model_validate(raw)
            self.session.validator.validate(raw)
            self.assertEqual(raw["raw_payload"]["source_payload"], source["payload"])
            self.assertEqual({k: v for k, v in raw["raw_context"].items() if k != "_ingest"}, source["context"])
            self.assertEqual(raw["raw_context"]["_ingest"]["source_sequence"], source["sequence"])
        self.assertEqual(set(self.session.by_modality), {"timeseries_signal", "image", "log_text", "structured_record"})

    def test_rest_real_erp_and_mes_asgi_get(self):
        _, create_app, _, _, _, _ = load_legacy()
        db = self.root / "rest.sqlite3"
        shutil.copyfile(self.batch / "demo_erp.sqlite3", db)
        record = self.record("REST_API")
        rest = self.connectors["REST_API"]
        with TestClient(create_app(db)) as client:
            result = rest.fetch(client, "/orders/" + record["object_hint"]["raw_id"], self.metadata(record))
            self.assertEqual(result.raw_payload["status"], "dispatched")
            record = self.record("MQTT", "structured_record")
            result = rest.fetch(client, "/mes/work-orders/" + record["object_hint"]["raw_id"], self.metadata(record))
            self.assertEqual(result.source_system, "MES")
            self.assertEqual(result.protocol, "REST_API")
            self.assertEqual(result.raw_payload["status"], "released")

    def test_rest_callback_error_status_and_invalid_json(self):
        class Response:
            status_code = 503
            def json(self):
                return {"error": "unavailable"}
        rest = self.connectors["REST_API"]
        self.assertIsNone(rest.fetch(lambda _: Response(), "/orders", self.metadata(self.record("REST_API"))))
        self.assertEqual(self.session.rejections[-1]["code"], "HTTP_STATUS")
        with patch.object(Response, "json", side_effect=ValueError("invalid JSON")):
            self.assertIsNone(rest.fetch(lambda _: Response(), "/orders", self.metadata(self.record("REST_API"))))
        self.assertEqual(self.session.rejections[-1]["code"], "REST_JSON")

    def test_sqlite_real_read_only_tables_and_material_snapshot(self):
        connector = self.connectors["SQLITE"]
        db_hash = sha(self.batch / "demo_erp.sqlite3")
        self.assertEqual(len(connector.read_rows("orders")), 1)
        self.assertEqual(connector.read_rows("inventory")[0]["resource_id"], "RES_MAT_01")
        metadata = self.metadata(self.record("SQLITE"))
        messages = list(connector.snapshot("inventory", lambda _: metadata))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_payload["quantity"], 100)
        # Actual connection arguments and write rejection verify read-only mode, not just a final hash.
        real_connect = sqlite3.connect
        observed = []
        def checked_connect(database_uri, **kwargs):
            self.assertIn("mode=ro", database_uri)
            connection = real_connect(database_uri, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM inventory")
            observed.append(database_uri)
            return connection
        with patch("task2_1_prototype.services.connector_service.connectors.sqlite3.connect", side_effect=checked_connect):
            connector.read_rows("inventory")
        self.assertEqual(len(observed), 1)
        self.assertEqual(sha(self.batch / "demo_erp.sqlite3"), db_hash)

    def test_database_protection_and_identifier_whitelist(self):
        with self.assertRaisesRegex(Rejected, "legacy"):
            DatabaseConnector(self.session, self.original_db)
        with self.assertRaisesRegex(Rejected, "already exist"):
            DatabaseConnector(self.session, self.root / "missing.sqlite3")
        with self.assertRaisesRegex(Rejected, "only orders"):
            self.connectors["SQLITE"].read_rows('orders"; DROP TABLE inventory; --')
        with self.assertRaisesRegex(Rejected, "connection_factory"):
            self.connectors["SQLITE"].read_rows("orders", "POSTGRESQL")

    def test_database_snapshot_mismatch_is_quarantined(self):
        record = self.record("SQLITE")
        record["payload"]["row"]["quantity"] = 99
        self.assertIsNone(self.connectors["SQLITE"].ingest_verified_row(record))
        self.assertEqual(self.session.rejections[-1]["code"], "DATABASE_SNAPSHOT")

    def test_postgresql_adapter_branch_uses_readonly_transaction_and_closes(self):
        row = self.record("SQLITE")["payload"]["row"]
        class Cursor:
            description = [(key,) for key in row]
            closed = False
            def execute(self, query):
                self.query = query
            def fetchall(self):
                return [tuple(row.values())]
            def close(self):
                self.closed = True
        class Connection:
            closed = rolled_back = False
            cur = Cursor()
            def set_session(self, **kwargs):
                self.options = kwargs
            def cursor(self):
                return self.cur
            def rollback(self):
                self.rolled_back = True
            def close(self):
                self.closed = True
        connection = Connection()
        connector = DatabaseConnector(self.session, connection_factory=lambda: connection)
        metadata = self.metadata(self.record("SQLITE"))
        messages = list(connector.snapshot("inventory", lambda _: metadata, protocol="POSTGRESQL"))
        self.assertEqual(messages[0].protocol, "POSTGRESQL")
        self.assertEqual(messages[0].raw_payload["source_payload"]["row"], row)
        self.assertEqual(connection.options, {"readonly": True, "autocommit": False})
        self.assertEqual(connection.cur.query, 'SELECT * FROM "inventory" ORDER BY "resource_id"')
        self.assertTrue(connection.closed and connection.rolled_back and connection.cur.closed)

    def test_postgresql_connection_cleanup_on_query_failure(self):
        from unittest.mock import Mock
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = RuntimeError("server unavailable")
        connector = DatabaseConnector(self.session, connection_factory=lambda: connection)
        with self.assertRaisesRegex(RuntimeError, "server unavailable"):
            connector.read_rows("orders", "POSTGRESQL")
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()
        connection.cursor.return_value.close.assert_called_once()

    def test_live_mqtt_wildcards_and_overlapping_subscriptions_deduplicate(self):
        record = self.record("MQTT", "timeseries_signal")
        broker = MemoryMqttBroker()
        connector = MqttConnector(self.session, ("factory/+/telemetry", "factory/#")).subscribe(broker)
        broker.publish(record["channel"], record)
        self.assertEqual(len(list(connector.drain())), 1)
        self.assertEqual(self.session.counts["duplicates"], 1)
        self.assertEqual(list(connector.drain()), [])
        with self.assertRaises(ValueError):
            MqttConnector(self.session, ("factory/#/bad",))

    def test_saved_mqtt_queue_all_four_modalities(self):
        connector = self.connectors["MQTT"]
        results = [connector.receive(delivery) for delivery in lines(self.batch / "mqtt_received.jsonl")]
        self.assertEqual(len(results), 400)
        self.assertTrue(all(results), self.session.rejections[:1])
        self.assertEqual(len({m.modality for m in results}), 4)

    def test_mqtt_wrong_topic_and_incomplete_signals(self):
        record = self.record("MQTT", "timeseries_signal")
        connector = self.connectors["MQTT"]
        self.assertIsNone(connector.receive({"topic": "unrelated", "payload": record}))
        record["channel"] = "other/topic"
        self.assertIsNone(connector.ingest(record))
        record = self.record("MQTT", "timeseries_signal")
        del record["payload"]["load_ratio"]
        self.assertIsNone(connector.ingest(record))
        self.assertEqual(self.session.counts["accepted"], 0)

    def test_opc_live_node_store_and_read_copy(self):
        record = self.record("OPC_UA")
        node = record["payload"]
        store = OpcUaNodeStore()
        store.write(node["NodeId"], node["Value"], node["SourceTimestamp"])
        result = self.connectors["OPC_UA"].read_node(store, node["NodeId"], self.metadata(record))
        self.assertEqual(result.raw_payload["spindle_speed_rpm"], node["Value"])
        self.assertEqual(result.raw_payload["source_payload"], node)
        self.assertIsNone(self.connectors["OPC_UA"].read_node(store, "missing", self.metadata(record)))

    def test_opc_quality_timestamp_fields_and_node_mismatch(self):
        for change in ({"StatusCode": "Bad"}, {"SourceTimestamp": "2026-10-28T00:00:00Z"},
                       {"NodeId": "ns=2;s=QC_01.spindle_speed_rpm"}, {"Value": True}):
            with self.subTest(change=change):
                record = self.record("OPC_UA")
                record["payload"].update(change)
                self.assertIsNone(self.connectors["OPC_UA"].ingest(record))
        record = self.record("OPC_UA")
        del record["payload"]["SourceTimestamp"]
        self.assertIsNone(self.connectors["OPC_UA"].ingest(record))
        self.assertEqual(self.session.counts["accepted"], 0)

    def test_plc_roundtrip_binary_hex_and_no_fabricated_work_order(self):
        record = self.record("PLC_FRAME")
        frame = bytes.fromhex(record["payload"]["frame_hex"])
        connector = self.connectors["PLC_FRAME"]
        result = connector.read_frame(frame, self.metadata(record))
        data = result.raw_payload
        values = {k: data[k] for k in ("spindle_speed_rpm", "temperature_c", "vibration_mm_s", "load_ratio")}
        self.assertEqual(encode_plc_frame(data["device_address"], values, data["heartbeat_seq"], data["control_cmd"]), frame)
        self.assertEqual(data["heartbeat_seq"], decode_plc_frame(frame)["heartbeat"])
        self.assertNotIn("work_order_id", data)
        self.assertEqual(result.raw_context["work_order_id"], "WO023")
        alternate = self.metadata(record)
        alternate["sequence"] += 10000
        self.assertIsNotNone(connector.read_frame(frame.hex(), alternate))

    def test_plc_all_malformed_frames_quarantined_with_diagnostics(self):
        record = self.record("PLC_FRAME")
        frame = bytes.fromhex(record["payload"]["frame_hex"])
        cases = [corrupt_frame(frame, k) for k in ("truncated", "crc", "unknown_address")]
        for index in (0, 3, 4, 13):
            body = bytearray(frame[:-2])
            body[index] = 255
            cases.append(bytes(body) + struct.pack("<H", crc16(body)))
        cases.append("not hexadecimal")
        for bad in cases:
            self.assertIsNone(self.connectors["PLC_FRAME"].read_frame(bad, self.metadata(record)))
        self.assertEqual(len(self.session.rejections), len(cases))
        for item in self.session.rejections:
            self.assertEqual(item["code"], "PLC_FRAME")
            self.assertEqual(item["sequence"], record["sequence"])
            self.assertTrue(item["reason"] and item["original_summary"])
        self.assertEqual(self.session.counts["accepted"], 0)

    def test_plc_binary_file_offset_and_address_context_consistency(self):
        record = self.record("PLC_FRAME")
        record["payload"]["offset"] += 18
        self.assertIsNone(self.connectors["PLC_FRAME"].ingest(record))
        self.assertEqual(self.session.rejections[-1]["code"], "PLC_BINARY")
        record = self.record("PLC_FRAME")
        decoded = decode_plc_frame(record["payload"]["frame_hex"])
        self.assertIsNone(self.connectors["PLC_FRAME"].read_frame(
            encode_plc_frame(2, decoded["signals"], decoded["heartbeat"]), self.metadata(record)))
        self.assertEqual(self.session.rejections[-1]["code"], "PLC_CONTEXT")

    def test_required_schedule_and_multimodal_legacy_identity_mismatch(self):
        for modality in ("timeseries_signal", "image", "log_text"):
            for key in ("equipment_id", "work_order_id", "process_id", "order_id"):
                record = self.record("MQTT", modality)
                del record["context"][key]
                self.assertIsNone(self.connectors["MQTT"].ingest(record))
        record = self.record("PLC_FRAME")
        record["context"]["legacy_ids"]["equipment_id"] = "E-QC"
        self.assertIsNone(self.connectors["PLC_FRAME"].ingest(record))
        self.assertEqual(self.session.rejections[-1]["code"], "ID_CONTEXT")

    def test_valid_png_and_hash_missing_traversal_absolute_rejections(self):
        valid = self.record("MQTT", "image")
        connector = self.connectors["MQTT"]
        self.assertIsNotNone(connector.ingest(valid))
        changes = [{"sha256": "f" * 64}, {"image_ref": "missing.png"},
                   {"image_ref": "../outside.png"}, {"image_ref": "..\\outside.png"},
                   {"image_ref": "C:\\outside.png"}, {"image_ref": "\\\\server\\x.png"},
                   {"image_ref": "sim_images/x.png:stream"}]
        for change in changes:
            record = deepcopy(valid)
            record["payload"].update(change)
            self.assertIsNone(connector.ingest(record))
        self.assertEqual(len(self.session.rejections), len(changes))
        self.assertEqual(self.session.counts["accepted"], 1)

    def test_file_with_matching_hash_but_invalid_png_rejected(self):
        folder = self.root / "invalid_image"
        folder.mkdir()
        image = folder / "fake.png"
        image.write_bytes(b"not a PNG")
        session = IngestSession(self.batch_id, folder, self.bindings)
        record = self.record("MQTT", "image")
        record["payload"].update(image_ref=image.name, sha256=sha(image))
        self.assertIsNone(MqttConnector(session).ingest(record))
        self.assertEqual(session.rejections[0]["code"], "IMAGE_DECODE")

    def test_log_alarm_and_recovery_exact_text_and_flags(self):
        connector = self.connectors["MQTT"]
        for action in ("fault", "fault_cleared"):
            record = deepcopy(next(r for r in self.records if r["payload"].get("state_action") == action))
            message = connector.ingest(record)
            self.assertEqual(message.raw_payload["state_action"], action)
            self.assertEqual(message.raw_payload["log_message"], record["payload"]["log_message"])
            for token in ("CNC_01", "OP20", "WO023", "0x84"):
                self.assertIn(token, message.raw_payload["log_message"])

    def test_same_message_is_idempotent_conflicting_payload_is_quarantined(self):
        record = self.record("MQTT", "timeseries_signal")
        connector = self.connectors["MQTT"]
        first = connector.ingest(record)
        self.assertIsNone(connector.ingest(record))
        repeat = MqttConnector(self.new_session()).ingest(record)
        self.assertEqual(first.model_dump(), repeat.model_dump())
        record["payload"]["temperature_c"] += 1
        self.assertIsNone(connector.ingest(record))
        self.assertEqual(self.session.rejections[-1]["code"], "IDENTITY_CONFLICT")

    def test_shuffled_same_timestamp_concurrent_arrivals_preserve_sequences(self):
        base = self.record("MQTT", "timeseries_signal")
        records = []
        for sequence in range(100):
            record = deepcopy(base)
            record["sequence"] = sequence
            record["context"]["sequence"] = 999 - sequence
            records.append(record)
        deliveries = records * 2
        random.Random(42).shuffle(deliveries)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [m for m in pool.map(self.connectors["MQTT"].ingest, deliveries) if m is not None]
        self.assertEqual(len(results), 100)
        self.assertEqual(self.session.counts["duplicates"], 100)
        self.assertEqual(self.session.counts["rejected"], 0)
        self.assertEqual({m.raw_context["_ingest"]["source_sequence"] for m in results}, set(range(100)))
        for message in results:
            self.assertEqual(message.raw_context["sequence"], 999 - message.raw_context["_ingest"]["source_sequence"])

    def test_second_work_order_process_does_not_merge_and_wrong_parent_rejected(self):
        rows = json.loads((self.batch / "id_registry.json").read_text())
        first = self.record("MQTT", "timeseries_signal")
        raw_process = first["context"]["legacy_ids"]["operation_id"]
        rows += [dict(kind="work_order", work_order=None, raw_id="WO_SECOND", object_id="WO024"),
                 dict(kind="order", work_order=None, raw_id="ORD_SECOND", object_id="ORDER_B"),
                 dict(kind="process", work_order="WO_SECOND", raw_id=raw_process, object_id="OP20_2")]
        session = IngestSession("two-orders", self.batch, Bindings(self.config, rows))
        connector = MqttConnector(session)
        one = connector.ingest(first)
        second = deepcopy(first)
        second["sequence"] += 10000
        second["context"].update(work_order_id="WO024", order_id="ORDER_B", process_id="OP20_2")
        second["context"]["legacy_ids"].update(work_order_id="WO_SECOND", order_id="ORD_SECOND")
        two = connector.ingest(second)
        self.assertEqual(one.raw_context["_ingest"]["resolved_ids"]["process_instance_key"], ["WO023", "OP20"])
        self.assertEqual(two.raw_context["_ingest"]["resolved_ids"]["process_instance_key"], ["WO024", "OP20_2"])
        second["context"]["process_id"] = "OP20"
        self.assertIsNone(connector.ingest(second))
        self.assertEqual(session.rejections[-1]["code"], "ID_CONTEXT")

    def test_strict_source_contract_missing_extra_enum_timestamp_nan_inf(self):
        base = self.record("MQTT", "timeseries_signal")
        invalid = []
        for key in base:
            record = deepcopy(base)
            del record[key]
            invalid.append(record)
        invalid += [{**base, "unexpected": 1}, {**base, "protocol": "MODBUS"},
                    {**base, "modality": "video"}, {**base, "timestamp": "2026-02-30T00:00:00Z"},
                    {**base, "timestamp": "2026-10-28T00:00:00"}]
        for value in (float("nan"), float("inf"), -float("inf")):
            record = deepcopy(base)
            record["payload"]["temperature_c"] = value
            invalid.append(record)
        for record in invalid:
            self.assertIsNone(self.connectors["MQTT"].ingest(record))
        json.dumps(self.session.rejections, allow_nan=False)
        self.assertEqual(self.session.counts["accepted"], 0)
        self.assertEqual(len(self.session.rejections), len(invalid))

    def test_reserved_provenance_collision_rejected(self):
        for target, key in (("context", "_ingest"), ("payload", "source_payload")):
            record = self.record("MQTT", "timeseries_signal")
            record[target][key] = {"forged": True}
            self.assertIsNone(self.connectors["MQTT"].ingest(record))
            self.assertEqual(self.session.rejections[-1]["code"], "RESERVED_KEY")

    def test_pipeline_complete_batch_diagnostics_dedup_and_organizations(self):
        output = self.root / "pipeline"
        summary = run_batch(self.batch, output, exercise_invalid=True, repeat=2)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["counts"], {"accepted": 2206, "duplicates": 2202, "rejected": 7})
        self.assertEqual(summary["source_batches"], [{"accepted": 2202}, {"duplicates": 2202}])
        self.assertEqual(summary["supplemental"], {"accepted": 4})
        self.assertEqual(summary["diagnostic_fixtures"], {"rejected": 7})
        messages = lines(output / "raw_ingest_messages.jsonl")
        self.assertEqual(len(messages), 2206)
        self.assertEqual(len(lines(output / "rejected_messages.jsonl")), 7)
        orgs = [m for m in messages if m["raw_context"].get("resource_kind") == "organization"]
        self.assertEqual({m["object_hint"]["raw_id"] for m in orgs}, {"ENT_01", "FAC_01", "LINE_01"})
        self.assertTrue(all(m["protocol"] == "SQLITE" and m["object_hint"]["suggested_type"] == "resource" for m in orgs))
        with self.assertRaises(FileExistsError):
            run_batch(self.batch, output)
        with self.assertRaisesRegex(ValueError, "overlap"):
            run_batch(self.batch, self.batch / "nested")

    def test_pipeline_bad_json_unknown_protocol_and_remaining_good_message(self):
        path = self.root / "broken.jsonl"
        source = self.record("REST_API")
        path.write_text('{broken\n{"a":1,"a":2}\n{"a":NaN}\n[]\n' + json.dumps(source) + "\n", encoding="utf-8")
        messages = list(ingest_stream(read_jsonl(path, self.session), self.connectors, self.session))
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.session.counts["rejected"], 4)
        self.assertEqual(self.session.rejections[0]["locator"], "broken.jsonl:1")

    def test_sink_io_failure_is_not_misreported_as_bad_source(self):
        def failed(_):
            raise OSError("disk full")
        self.session.accepted_sink = failed
        with self.assertRaisesRegex(OSError, "disk full"):
            self.connectors["REST_API"].ingest(self.record("REST_API"))
        self.assertFalse(self.session.seen)
        self.assertEqual(self.session.rejections, [])

    def test_z_source_batch_and_original_database_unchanged(self):
        self.assertEqual(sha(self.original_db).upper(), "D98A81A49CB950AC8DF38B4B164856D48295E10EDF44BE564A080048D4F0424A")
        self.assertEqual(sha(self.original_db), self.original_hash)
        self.assertEqual({p.relative_to(self.batch).as_posix(): sha(p) for p in self.batch.rglob("*") if p.is_file()}, self.source_hashes)


if __name__ == "__main__":
    unittest.main()
