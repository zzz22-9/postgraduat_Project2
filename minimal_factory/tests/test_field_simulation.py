"""四协议沙盒与数据驱动故障级联验收；所有数据库均在临时目录。"""
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from erp.dispatch import prepare_message
from mes import service as mes
from mes.schemas import ERPDispatchRequest, Note
from simulation import engine, field_protocols
from simulation.field_runtime import FieldRecorder
from simulation.inputs import configure, demo_snapshot, readonly_source, source_snapshot
from simulation.schemas import FieldScenario, Override, RunRequest
from simulation.service import SimulationService


def fixture(mode="normal", devices=None):
    snapshot = demo_snapshot()
    snapshot["orders"] = snapshot["orders"][:1]
    wo = snapshot["orders"][0]
    wo["quantity"] = 2
    config = {"mode": mode}
    if devices is not None:
        config["anomaly_equipment_ids"] = devices
    return configure(snapshot, {wo["work_order_id"]: Override(field_protocols=FieldScenario(**config))})


class DatabaseObserver:
    """在故障/恢复事件发出当刻读取已提交的真实SQLite状态。"""
    def __init__(self, path):
        self.path, self.states = path, []

    def on_event(self, event):
        if event["event_type"] in ("device_anomaly_affected_work_order", "device_recovered"):
            with mes.connect(self.path) as conn:
                row = dict(conn.execute("SELECT * FROM equipment WHERE equipment_id=?",
                                        (event["equipment_id"],)).fetchone())
            self.states.append((event, row))

    def observe(self, env, context, completed, stop):
        yield completed


class FieldSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def simulate(self, snapshot, name="case"):
        folder = self.root / name
        folder.mkdir()
        observer = DatabaseObserver(folder / "mes.db")
        result = engine.simulate(folder, "SIM-" + "a"*32, snapshot,
                                 threading.Event(), lambda *_: None, observer)
        self.assert_protocol_storage_matches(folder, result)
        return folder, result, observer

    def assert_protocol_storage_matches(self, folder, result):
        """复用原 simulate() 的存储一致性断言，供篡改副本后再次检查。"""
        with readonly_source(folder / "mes.db") as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            rows = [dict(r) for r in conn.execute("SELECT * FROM protocol_messages ORDER BY rowid")]
            db_messages = [json.loads(row["raw_json"]) for row in rows]
            protocol_events = [json.loads(r[0]) for r in conn.execute(
                "SELECT detail FROM events WHERE action IN (?,?,?,?) ORDER BY event_id", (
                    "mes_to_field_mqtt_dispatched", "field_opcua_signal_reported",
                    "field_vision_http_reported", "field_modbus_reported"))]
        jsonl = [json.loads(line) for line in (folder / "protocol_messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["protocol_messages"], db_messages)
        self.assertEqual(jsonl, db_messages)
        self.assertEqual(protocol_events, db_messages)
        self.assertTrue(all(row["alarm_summary"] for row in rows if row["is_anomaly"]))
        self.assertEqual(len({m["message_id"] for m in db_messages}), len(db_messages))
        return rows

    def assert_protocol_relations(self, folder, snapshot, result, *, run_id, sources_required=True):
        """测试专用只读断言，核对已完成批次，失败时抛出带关系名的 AssertionError。"""
        rows = self.assert_protocol_storage_matches(folder, result)
        events = self.protocol_events(folder)
        messages = result["protocol_messages"]
        self.assertEqual(Counter(e["detail"]["message_id"] for e in events),
                         Counter(m["message_id"] for m in messages), "protocol_event_ids")
        by_event = {event["detail"]["message_id"]: event for event in events}
        expected_orders = {wo["work_order_id"]: wo for wo in snapshot["orders"]}
        fields = ("message_id", "virtual_minute", "protocol", "work_order_id", "operation_id",
                  "equipment_id", "device_code", "is_anomaly", "source_file", "sample_hash")
        with readonly_source(folder / "mes.db") as conn:
            for row in rows:
                message = json.loads(row["raw_json"])
                self.assertEqual({key: row[key] for key in fields},
                                 {key: message[key] for key in fields}, "sqlite_raw_json")
                wo = mes.work_order(conn, message["work_order_id"])
                op = mes.operation(conn, message["operation_id"])
                eq = mes.equipment(conn, message["equipment_id"])
                self.assertEqual(op["work_order_id"], wo["work_order_id"], "operation_work_order")
                self.assertEqual(op["equipment_id"], eq["equipment_id"], "operation_equipment")
                self.assertEqual(op["kind"], eq["kind"], "equipment_kind")
                self.assertEqual(message["device_code"], field_protocols.DEVICES[eq["equipment_id"]],
                                 "device_code")
                expected = expected_orders[wo["work_order_id"]]
                for key in ("order_id", "product_id", "product_name", "quantity"):
                    self.assertEqual(wo[key], expected[key], "work_order_snapshot " + key)
                event = by_event[message["message_id"]]
                self.assertEqual(message["run_id"], run_id, "run_id")
                self.assertEqual(event["run_id"], run_id, "event_run_id")
                self.assertEqual(event["order_id"], wo["order_id"], "event_order_id")
                for key in ("work_order_id", "operation_id", "equipment_id"):
                    self.assertEqual(event[key], message[key], "event_context " + key)
                self.assertEqual(event["sim_time_min"], message["virtual_minute"], "virtual_minute")
                self.assertEqual(event["detail"], message, "event_detail")
                if event["phase"] == "normal":
                    self.assertEqual(event["quantity"], wo["quantity"], "normal_quantity")
                    self.assertIsNone(event["rework_id"], "normal_rework_id")
                else:
                    self.assertIn(event["phase"], ("repair", "reinspection"), "execution_phase")
                    task = conn.execute("SELECT * FROM reworks WHERE rework_id=?",
                                        (event["rework_id"],)).fetchone()
                    self.assertIsNotNone(task, "rework_id")
                    self.assertEqual(task["work_order_id"], wo["work_order_id"], "rework_work_order")
                    self.assertEqual(event["quantity"], task["quantity"], "rework_quantity")
                    operation_key = "target_operation_id" if event["phase"] == "repair" else "source_operation_id"
                    self.assertEqual(op["operation_id"], task[operation_key], "rework_operation")
        source_path = folder / "source_context.jsonl"
        if source_path.exists():
            sources = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(Counter(source["message_id"] for source in sources),
                             Counter(message["message_id"] for message in messages), "source_message_ids")
            by_source = {source["message_id"]: source for source in sources}
            for message in messages:
                source = by_source[message["message_id"]]
                for key in ("source_file", "sample_hash"):
                    self.assertEqual(source[key], message[key], "source_context " + key)
        else:
            self.assertFalse(sources_required, "source_context_required")

    def assert_protocol_rejected(self, snapshot, builder_name, changed_builder, expected_error, case):
        """在实际拒绝时核对所有通道，保留此前合法输出并记录可复核证据。"""
        original = FieldRecorder.message
        rejected = []

        def observe_message(recorder, message, action, wo, op, emit, *args, **options):
            with readonly_source(recorder.path) as conn:
                before_db = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                             for table in ("protocol_messages", "events")}
            folder = recorder.path.parent
            before_files = {name: (folder / name).read_bytes() for name in
                            ("protocol_messages.jsonl", "source_context.jsonl", "events.jsonl")}
            before_alerts, before_messages = deepcopy(recorder.alerts), deepcopy(recorder.messages)
            emitted = Mock(wraps=emit)
            try:
                return original(recorder, message, action, wo, op, emitted, *args, **options)
            except mes.BusinessError as error:
                with readonly_source(recorder.path) as conn:
                    after_db = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                                for table in before_db}
                after_files = {name: (folder / name).read_bytes() for name in before_files}
                self.assertEqual(after_db, before_db, "rejection_database_unchanged")
                self.assertEqual(after_files, before_files, "rejection_files_unchanged")
                self.assertEqual(recorder.alerts, before_alerts, "rejection_alerts_unchanged")
                self.assertEqual(recorder.messages, before_messages, "rejection_messages_unchanged")
                emitted.assert_not_called()
                self.assertNotIn(message["message_id"], [row["message_id"] for row in after_db["protocol_messages"]])
                self.assertFalse(any(message["message_id"] in row["detail"] for row in after_db["events"]))
                rejected.append(dict(case=case, runtime_rejected=True, error=str(error),
                    message=deepcopy(message), source=deepcopy(options["source"]),
                    context=dict(work_order_id=wo["work_order_id"], operation_id=op["operation_id"],
                                 equipment_id=op["equipment_id"], run_id=options["expected_run_id"],
                                 virtual_minute=options["expected_virtual_minute"]),
                    directory_run_id=folder.name,
                    protocol_rows_before=len(before_db["protocol_messages"]),
                    mes_events_before=len(before_db["events"]),
                    file_sha256_before={name: hashlib.sha256(raw).hexdigest() for name, raw in before_files.items()},
                    file_sha256_after={name: hashlib.sha256(raw).hexdigest() for name, raw in after_files.items()},
                    partial_protocol_writes=False, success_emit_calls=emitted.call_count,
                    alerts_unchanged=True, message_list_unchanged=True))
                raise

        current_run = "SIM-" + "a"*32
        (self.root / case).mkdir()
        with patch("simulation.field_protocols." + builder_name, side_effect=changed_builder), \
                patch.object(FieldRecorder, "message", new=observe_message), \
                self.assertRaisesRegex(mes.BusinessError, expected_error):
            self.simulate(snapshot, str(Path(case) / current_run))
        self.assertEqual(len(rejected), 1)
        folder = self.root / case / current_run
        for name in ("protocol_messages.jsonl", "source_context.jsonl", "events.jsonl"):
            raw = (folder / name).read_bytes()
            self.assertNotIn(rejected[0]["message"]["message_id"].encode(), raw)
            self.assertNotIn(rejected[0]["source"]["message_id"].encode(), raw)
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event["event_type"] == "processing_started" for event in events))
        rejected[0]["processing_started_preserved"] = True
        print("P03_RUNTIME_EVIDENCE " + json.dumps(rejected[0], ensure_ascii=False))
        return rejected[0]

    def test_relation_rejects_foreign_operation_before_write(self):
        snapshot = configure(demo_snapshot(), {})
        first, second = snapshot["orders"]
        original = field_protocols.build_cnc_opcua_message

        def foreign_operation(work_order_id, operation_id, virtual_minute, **options):
            if work_order_id == first["work_order_id"]:
                operation_id = second["operations"][0]["operation_id"]
            return original(work_order_id, operation_id, virtual_minute, **options)

        self.assert_protocol_rejected(snapshot, "build_cnc_opcua_message", foreign_operation,
                                      "operation_id", "A")

    def test_relation_rejects_wrong_equipment_before_write(self):
        snapshot = fixture()
        original = field_protocols.build_mqtt_dispatch_message

        def wrong_equipment(work_order_id, operation_id, equipment_id, virtual_minute, quantity, **options):
            if equipment_id == "E-ASM":
                equipment_id = "E-MOVE"
            return original(work_order_id, operation_id, equipment_id, virtual_minute, quantity, **options)

        self.assert_protocol_rejected(snapshot, "build_mqtt_dispatch_message", wrong_equipment,
                                      "equipment_id", "B")

    def test_relation_rejects_foreign_run_before_write(self):
        snapshot = fixture()
        current_run = "SIM-" + "a"*32
        foreign_run = "SIM-" + "b"*32
        original = field_protocols.build_cnc_opcua_message

        def foreign_batch(*args, **options):
            options["run_id"] = foreign_run
            return original(*args, **options)

        observed = self.assert_protocol_rejected(snapshot, "build_cnc_opcua_message", foreign_batch,
                                                "run_id", "C")
        self.assertEqual(observed["directory_run_id"], current_run)
        self.assertEqual(observed["context"]["run_id"], current_run)
        service = SimulationService(self.root / "unused-source.db", self.root / "service-runs")
        service.startup()
        self.addCleanup(service.shutdown)
        with patch("simulation.field_protocols.build_cnc_opcua_message", side_effect=foreign_batch), \
                self.assertLogs(level="ERROR") as logged:
            started = service.start(RunRequest(source="demo"))
            deadline = time.monotonic() + 10
            record = service.get(started["run_id"])
            while record["status"] == "running" and time.monotonic() < deadline:
                time.sleep(.01)
                record = service.get(started["run_id"])
        self.assertEqual(record["status"], "failed", record.get("error"))
        self.assertIn("run_id", record["error"])
        self.assertIsNone(record["summary"])
        self.assertFalse((service.folder(started["run_id"]) / "summary.json").exists())
        self.assertGreater(len(record["protocol_messages"]), 0)
        self.assertTrue(all(message["run_id"] == started["run_id"] for message in record["protocol_messages"]))
        self.assertTrue(any(event["event_type"] == "processing_started" for event in record["events"]))
        print("P03_RUNTIME_EVIDENCE " + json.dumps(dict(case="C-service", status=record["status"],
            error=record["error"], summary_absent=True, preserved_protocols=len(record["protocol_messages"]),
            processing_started_preserved=True, logged_error=logged.output[0]), ensure_ascii=False))

    def test_relation_rejects_wrong_virtual_minute_before_write(self):
        original = field_protocols.build_cnc_opcua_message

        def wrong_time(work_order_id, operation_id, virtual_minute, **options):
            return original(work_order_id, operation_id, virtual_minute + 1, **options)

        self.assert_protocol_rejected(fixture(), "build_cnc_opcua_message", wrong_time,
                                      "virtual_minute", "C-time")

    def test_relation_rejects_wrong_device_code_before_write(self):
        original = field_protocols.build_cnc_opcua_message

        def wrong_code(*args, **options):
            message, source = original(*args, **options)
            message["device_code"] = field_protocols.DEVICES["E-MOVE"]
            return message, source

        self.assert_protocol_rejected(fixture(), "build_cnc_opcua_message", wrong_code,
                                      "device_code", "B-code")

    def test_relation_sqlite_columns_disagree_with_raw_json(self):
        snapshot = configure(demo_snapshot(), {})
        other = snapshot["orders"][1]
        changes = {"work_order_id": other["work_order_id"],
                   "operation_id": other["operations"][0]["operation_id"], "equipment_id": "E-MOVE"}
        for column, value in changes.items():
            with self.subTest(column=column):
                folder, result, _ = self.simulate(snapshot, column)
                message = next(m for m in result["protocol_messages"] if m["protocol"] == "OPC-UA")
                with mes.transaction(folder / "mes.db") as conn:
                    conn.execute(f"UPDATE protocol_messages SET {column}=? WHERE message_id=?",
                                 (value, message["message_id"]))
                self.assert_protocol_storage_matches(folder, result)
                with self.assertRaisesRegex(AssertionError, "sqlite_raw_json") as caught:
                    self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
                print("P03_EVIDENCE " + json.dumps(dict(case="D", column=column,
                    sqlite_update_rejected=False, runtime_rejected=None, offline_detected=True,
                    existing_storage_checks_passed=True, message_id=message["message_id"],
                    raw_value=message[column], sqlite_value=value, assertion=str(caught.exception))))

    def test_relation_sources_detect_duplicate_orphan_missing_and_foreign(self):
        snapshot = fixture()
        folder, result, _ = self.simulate(snapshot)
        source_path = folder / "source_context.jsonl"
        sources = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines()]
        other_folder = self.root / ("SIM-" + "b"*32)
        other_folder.mkdir()
        other = engine.simulate(other_folder, other_folder.name, snapshot,
                                threading.Event(), lambda *_: None)
        self.assert_protocol_relations(other_folder, snapshot, other, run_id=other_folder.name)
        foreign = json.loads((other_folder / "source_context.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn(foreign["message_id"], {m["message_id"] for m in result["protocol_messages"]})
        mutations = {"duplicate": sources + [sources[0]],
                     "orphan": sources + [{**sources[0], "message_id": "MSG-" + "0"*32}],
                     "missing": sources[:-1], "foreign": [foreign] + sources[1:]}
        for name, changed in mutations.items():
            with self.subTest(case=name):
                source_path.write_text("".join(json.dumps(row) + "\n" for row in changed), encoding="utf-8")
                self.assert_protocol_storage_matches(folder, result)
                with self.assertRaisesRegex(AssertionError, "source_message_ids"):
                    self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
                print("P03_EVIDENCE " + json.dumps(dict(case="E-" + name, offline_detected=True,
                    runtime_rejected=None, injection="completed temporary source file",
                    protocol_ids=[m["message_id"] for m in result["protocol_messages"]],
                    source_ids=[s["message_id"] for s in changed])))

    def test_relation_rejects_foreign_source_before_write(self):
        snapshot = fixture()
        original = field_protocols.build_cnc_opcua_message

        def foreign_source(*args, **options):
            message, _ = original(*args, **options)
            _, source = original(*args, **{**options, "run_id": "SIM-" + "b"*32})
            return message, source

        self.assert_protocol_rejected(snapshot, "build_cnc_opcua_message", foreign_source,
                                      "message_id", "E-writer")

    def test_relation_existing_foreign_key_and_duplicate_message_guards(self):
        snapshot = fixture()
        folder, result, _ = self.simulate(snapshot)
        wo = snapshot["orders"][0]
        op = wo["operations"][0]
        existing = result["protocol_messages"][0]
        missing, missing_source = field_protocols.build_mqtt_dispatch_message(
            "missing-work-order", op["operation_id"], op["equipment_id"], 0, wo["quantity"],
            run_id="SIM-" + "a"*32, return_source=True)
        existing_source = json.loads((folder / "source_context.jsonl").read_text(encoding="utf-8").splitlines()[0])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            with mes.transaction(folder / "mes.db") as conn:
                conn.execute("UPDATE protocol_messages SET work_order_id=? WHERE message_id=?",
                             (missing["work_order_id"], existing["message_id"]))
        with io.StringIO() as protocol_stream, io.StringIO() as source_stream:
            recorder = FieldRecorder(folder / "mes.db", protocol_stream, snapshot["equipment"],
                                     source_stream=source_stream)
            emitted = Mock()
            for message, source, error_type, expected_error in (
                (missing, missing_source, mes.BusinessError, "work_order_id"),
                (existing, existing_source, sqlite3.IntegrityError, "UNIQUE"),
            ):
                with self.subTest(error=expected_error):
                    with self.assertRaisesRegex(error_type, expected_error):
                        recorder.message(message, "mes_to_field_mqtt_dispatched", wo, op, emitted,
                                         quantity=wo["quantity"], phase="normal", rework_id=None, source=source,
                                         expected_run_id="SIM-" + "a"*32, expected_virtual_minute=0)
            self.assertEqual((protocol_stream.getvalue(), source_stream.getvalue()), ("", ""))
            emitted.assert_not_called()
        self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
        print("P03_EVIDENCE " + json.dumps(dict(case="existing-guards", runtime_rejected=True,
            checks=["missing foreign key", "duplicate message_id"], output_streams_empty=True)))

    def test_relation_valid_cancelled_history_and_received_snapshot(self):
        initial = fixture()
        wo = initial["orders"][0]
        source = self.root / "source.db"
        engine.seed_database(source, initial)
        mes.cancel(source, wo["work_order_id"], Note(operator="测试", note="历史撤回"))
        instruction = ERPDispatchRequest(order_id=wo["order_id"], message_id="relation-snapshot",
            steps=[{key: op[key] for key in ("name", "kind", "minutes")} for op in wo["operations"]])
        envelope = prepare_message(source, instruction)
        with mes.transaction(source) as conn:
            conn.execute("UPDATE orders SET product_id=?,product_name=?,quantity=? WHERE order_id=?",
                         ("changed-product", "ERP后续名称", 9, wo["order_id"]))
        received = mes.dispatch(source, envelope)
        mes.release(source, received["work_order_id"])
        snapshot = configure(source_snapshot(source, [received["work_order_id"]]), {})
        selected = snapshot["orders"][0]
        self.assertEqual((selected["product_id"], selected["product_name"], selected["quantity"]),
                         (wo["product_id"], wo["product_name"], wo["quantity"]))
        with readonly_source(source) as conn:
            history = [dict(row) for row in conn.execute(
                "SELECT work_order_id,status FROM work_orders WHERE order_id=?", (wo["order_id"],))]
        self.assertEqual(Counter(row["status"] for row in history), Counter(["cancelled", "released"]))
        folder, result, _ = self.simulate(snapshot)
        self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
        print("P03_EVIDENCE " + json.dumps(dict(case="F-snapshot-history", offline_detected=False,
            valid=True, order_id=wo["order_id"], history=history, erp_current_quantity=9,
            received_quantity=selected["quantity"], completed_quantity=result["completed_quantity"])))

    def test_relation_valid_legacy_without_source_context_is_explicit(self):
        snapshot = fixture()
        folder, result, _ = self.simulate(snapshot)
        (folder / "source_context.jsonl").unlink()  # 仅临时夹具模拟 A2 前的文件集合。
        self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32,
                                       sources_required=False)
        with self.assertRaisesRegex(AssertionError, "source_context_required"):
            self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
        print("P03_EVIDENCE " + json.dumps(dict(case="F-legacy", valid=True,
            fixture="temporary pre-A2 file layout", legacy_accepted=True, current_missing_detected=True)))

    def test_source_context_matches_consumed_signal_rows(self):
        for mode, index, expected_time, expected_upstream in (
            ("normal", 2, 2, 2), ("anomaly", 0, 0, 240),
        ):
            with self.subTest(mode=mode):
                snapshot = fixture(mode)
                snapshot["orders"][0]["field_protocols"]["sample_index"] = index
                folder, summary, _ = self.simulate(snapshot, mode)
                sources = [json.loads(line) for line in (folder / "source_context.jsonl").read_text(
                    encoding="utf-8").splitlines()]
                messages = summary["protocol_messages"]
                self.assertEqual([source["message_id"] for source in sources],
                                 [message["message_id"] for message in messages])
                for message, source in zip(messages, sources):
                    if message["protocol"] not in ("OPC-UA", "Modbus-TCP"):
                        continue
                    raw = Path(message["source_file"]).read_bytes().splitlines()[index]
                    sample = json.loads(raw)
                    self.assertEqual(source, dict(
                        message_id=message["message_id"], source_file=message["source_file"],
                        sample_hash=hashlib.sha256(raw).hexdigest(), source_kind="synthetic",
                        sample_id=sample["sample_id"], sample_index=index,
                        sample_time_s=sample["sample_time_s"], source_time_s=sample["source_time_s"]))
                    self.assertNotIn("sample_time_s", message)
                    if message["protocol"] == "OPC-UA" and message["is_anomaly"] == (mode == "anomaly"):
                        self.assertEqual((source["sample_time_s"], source["source_time_s"]),
                                         (expected_time, expected_upstream))
                        self.assertEqual(message["virtual_minute"], 6)

    def test_mqtt_and_vision_source_context_have_no_sample_times(self):
        folder, summary, _ = self.simulate(fixture())
        sources = {source["message_id"]: source for source in map(json.loads,
                   (folder / "source_context.jsonl").read_text(encoding="utf-8").splitlines())}
        for message in summary["protocol_messages"]:
            if message["protocol"] in ("MQTT", "HTTP-Vision-Webhook"):
                source = sources[message["message_id"]]
                self.assertEqual(source["source_file"], message["source_file"])
                self.assertEqual(source["sample_hash"], message["sample_hash"])
                for key in ("sample_index", "sample_time_s", "source_time_s"):
                    self.assertIsNone(source[key])
                if message["protocol"] == "MQTT":
                    self.assertEqual(Path(source["source_file"]).name, "manifest.json")
                    self.assertEqual(source["source_kind"], "configuration_generated")
                    self.assertIsNone(source["sample_id"])
                else:
                    self.assertEqual(source["source_kind"], "synthetic")
                    self.assertEqual(source["sample_id"], "vision_normal_front")

    def test_normal_12_minutes_no_fault_and_four_protocols(self):
        snapshot = fixture()
        folder, result, observer = self.simulate(snapshot)
        self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
        self.assertEqual(result["makespan_min"], 12)
        self.assertEqual(result["delay_summary"][0], dict(work_order_id="DEMO-WO-1",
            planned_minutes=12, actual_minutes=12, delay_minutes=0, anomaly_status="NORMAL"))
        self.assertEqual(observer.states, [])
        self.assertTrue(all(not a["had_fault"] and a["status"] == "normal" for a in result["equipment_alerts"]))
        messages = result["protocol_messages"]
        self.assertEqual(len(messages), 6)
        self.assertEqual({m["protocol"] for m in messages},
                         {"MQTT", "OPC-UA", "HTTP-Vision-Webhook", "Modbus-TCP"})
        self.assertEqual([m["virtual_minute"] for m in messages], [0, 6, 6, 10, 10, 12])

    def protocol_events(self, folder):
        actions = {"mes_to_field_mqtt_dispatched", "field_opcua_signal_reported",
                   "field_vision_http_reported", "field_modbus_reported"}
        path = folder / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [event for event in events if event["event_type"] in actions]

    def test_normal_protocol_events_keep_actual_execution_context(self):
        folder, result, _ = self.simulate(fixture())
        events = self.protocol_events(folder)
        messages = {m["message_id"]: m for m in result["protocol_messages"]}
        self.assertEqual(len(events), 6)
        self.assertEqual({e["detail"]["message_id"] for e in events}, set(messages))
        for event in events:
            self.assertEqual((event["quantity"], event["phase"], event["rework_id"]), (2, "normal", None))
            self.assertEqual(event["detail"], messages[event["detail"]["message_id"]])

    def test_partial_rework_protocol_events_keep_quantity_phase_and_rework_id(self):
        snapshot = fixture("anomaly", ["E-QC"])
        folder, result, _ = self.simulate(snapshot)
        self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
        self.assertEqual(result["makespan_min"], 17)
        self.assertEqual(result["completed_quantity"], 2)
        self.assertEqual(result["rework_quantity"], 1)
        self.assertEqual(result["delay_summary"][0]["delay_minutes"], 5)
        wo = mes.get_work_order(folder / "mes.db", "DEMO-WO-1")
        rework_id = wo["reworks"][0]["rework_id"]
        self.assertEqual(wo["reworks"][0]["status"], "completed")
        task = wo["reworks"][0]
        self.assertNotEqual(task["source_operation_id"], task["target_operation_id"])
        with readonly_source(folder / "mes.db") as conn:
            business_log = dict(conn.execute(
                "SELECT operation_id,equipment_id FROM events WHERE action='rework_inspect_start'").fetchone())
        self.assertEqual(business_log, dict(operation_id=task["target_operation_id"], equipment_id="E-QC"))
        events = self.protocol_events(folder)
        messages = {m["message_id"]: m for m in result["protocol_messages"]}
        self.assertEqual(len(events), 10)
        self.assertEqual({e["detail"]["message_id"] for e in events}, set(messages))
        for phase, quantity, expected_rework, count, protocols in (
            ("normal", 2, None, 6, {"MQTT", "OPC-UA", "HTTP-Vision-Webhook", "Modbus-TCP"}),
            ("repair", 1, rework_id, 2, {"MQTT", "OPC-UA"}),
            ("reinspection", 1, rework_id, 2, {"MQTT", "HTTP-Vision-Webhook"}),
        ):
            group = [event for event in events if event["phase"] == phase]
            self.assertEqual(len(group), count)
            self.assertEqual({event["detail"]["protocol"] for event in group}, protocols)
            for event in group:
                self.assertEqual((event["quantity"], event["rework_id"]), (quantity, expected_rework))
                message = messages[event["detail"]["message_id"]]
                self.assertEqual(event["detail"], message)
                for field in ("work_order_id", "operation_id", "equipment_id"):
                    self.assertEqual(event[field], message[field])
        print("P03_EVIDENCE " + json.dumps(dict(case="F-rework", valid=True, rework_id=rework_id,
            quantity=task["quantity"], source_operation_id=task["source_operation_id"],
            target_operation_id=task["target_operation_id"], business_log=business_log,
            protocol_reinspection_operation_ids=[e["operation_id"] for e in events if e["phase"] == "reinspection"])))

    def test_all_anomalies_real_fault_repair_rework_and_delay(self):
        folder, result, observer = self.simulate(fixture("anomaly"))
        self.assertEqual(result["makespan_min"], 28)
        self.assertEqual(result["delay_summary"][0]["delay_minutes"], 16)
        self.assertEqual(result["delay_summary"][0]["anomaly_status"], "DELAYED_BY_FAULT")
        self.assertEqual([r["status"] for _, r in observer.states],
                         ["fault", "available", "fault", "available", "fault", "available"])
        self.assertEqual([e["sim_time_min"] for e, _ in observer.states], [6, 12, 16, 19, 23, 28])
        for event, row in observer.states:
            self.assertEqual(row["status"], event["detail"]["status"])
            self.assertTrue(row["alarm_message"])
        self.assertEqual(len(result["protocol_messages"]), 10)
        self.assertEqual(sum(m["is_anomaly"] for m in result["protocol_messages"]), 3)
        alerts = result["equipment_alerts"]
        self.assertTrue(all(a["had_fault"] and a["status"] == "fault" and
                            a["current_status"] == "available" for a in alerts))
        self.assertTrue(all(a["fault_records"][0]["recovered_virtual_minute"] is not None for a in alerts))
        self.assertIn("(3/4)", next(a for a in alerts if a["device_code"] == "Vision_01")["alarm_message"])
        detail = mes.get_work_order(folder / "mes.db", "DEMO-WO-1")
        self.assertEqual((detail["planned_minutes"], detail["actual_minutes"], detail["delay_minutes"]), (12, 28, 16))
        self.assertEqual(detail["reworks"][0]["status"], "completed")
        self.assertEqual(len(detail["rework_actions"]), 4)
        self.assertEqual([(i["phase"], i["end_min"]-i["start_min"]) for i in result["intervals"]],
                         [("normal", 12), ("normal", 4), ("repair", 3), ("reinspection", 2), ("normal", 7)])

    def test_each_device_independently_changes_duration(self):
        for device, expected in (("E-ASM", 18), ("E-QC", 17), ("E-MOVE", 17)):
            with self.subTest(device=device):
                _, result, observer = self.simulate(fixture("anomaly", [device]), device)
                self.assertEqual(result["makespan_min"], expected)
                self.assertEqual(len(observer.states), 2)
                self.assertEqual(sum(a["had_fault"] for a in result["equipment_alerts"]), 1)

    def test_returned_sample_drives_fault_instead_of_requested_mode(self):
        original = field_protocols.build_cnc_opcua_message

        def normal_sample(*args, **kwargs):
            kwargs["is_anomaly"] = False
            return original(*args, **kwargs)

        with patch("simulation.field_protocols.build_cnc_opcua_message", side_effect=normal_sample):
            _, result, observer = self.simulate(fixture("anomaly", ["E-ASM"]))
        self.assertEqual(result["makespan_min"], 12)
        self.assertFalse(observer.states)

    def test_sampled_replay_and_probability_extremes(self):
        snapshot = fixture("sampled")
        snapshot["orders"][0]["field_protocols"].update(seed=42, anomaly_probability=.5)
        _, first, _ = self.simulate(snapshot, "first")
        _, second, _ = self.simulate(snapshot, "second")
        self.assertEqual(first["protocol_messages"], second["protocol_messages"])
        self.assertEqual(first["delay_summary"], second["delay_summary"])
        for probability, expected in ((0, 12), (1, 28)):
            snapshot["orders"][0]["field_protocols"]["anomaly_probability"] = probability
            _, result, _ = self.simulate(snapshot, f"probability-{probability}")
            self.assertEqual(result["makespan_min"], expected)

    def test_concurrent_orders_wait_for_fault_recovery(self):
        snapshot = demo_snapshot()
        snapshot = configure(snapshot, {w["work_order_id"]: Override(field_protocols=FieldScenario(mode="anomaly"))
                                        for w in snapshot["orders"]})
        folder, result, observer = self.simulate(snapshot)
        self.assert_protocol_relations(folder, snapshot, result, run_id="SIM-" + "a"*32)
        self.assertEqual(len(result["orders"]), 2)
        self.assertTrue(all(w["anomaly_status"] == "DELAYED_BY_FAULT" for w in result["orders"]))
        self.assertEqual(sum(r["status"] == "fault" for _, r in observer.states), 6)
        for eq in snapshot["equipment"]:
            intervals = sorted((i for i in result["intervals"] if i["equipment_id"] == eq["equipment_id"]),
                               key=lambda i: i["start_min"])
            self.assertTrue(all(a["end_min"] <= b["start_min"] for a, b in zip(intervals, intervals[1:])))

    def test_service_persists_summary_get_export_and_source_unchanged(self):
        source = self.root / "source.db"
        engine.seed_database(source, fixture())
        before = source.read_bytes()
        service = SimulationService(source, self.root / "test-runs")
        service.startup()
        self.addCleanup(service.shutdown)
        request = RunRequest(source="work_orders", work_order_ids=["DEMO-WO-1"], overrides={
            "DEMO-WO-1": Override(field_protocols=FieldScenario(mode="anomaly"))})
        started = service.start(request)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = service.get(started["run_id"])
            if record["status"] != "running":
                break
            time.sleep(.01)
        self.assertEqual(record["status"], "completed", record.get("error"))
        for key in ("equipment_alerts", "delay_summary", "protocol_messages"):
            self.assertEqual(record[key], record["summary"][key])
        saved = json.loads((service.folder(record["run_id"]) / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, record["summary"])
        exported = json.loads(service.export(record["run_id"], "json")[0])
        self.assertEqual(exported["delay_summary"][0]["actual_minutes"], 28)
        self.assertEqual(source.read_bytes(), before)

    def test_validation_and_reset_to_normal(self):
        for bad in ({"sample_index": 12}, {"cnc_repair_minutes": 0}, {"anomaly_probability": 2},
                    {"mode": "typo"}, {"anomaly_equipment_ids": ["other"]}):
            with self.assertRaises(ValidationError):
                FieldScenario(**bad)
        snapshot = fixture("anomaly")
        normal = configure(snapshot, {"DEMO-WO-1": Override(field_protocols=None)})
        self.assertEqual(normal["orders"][0]["field_protocols"]["mode"], "normal")
        snapshot["orders"][0]["operations"] = snapshot["orders"][0]["operations"][1:]
        with self.assertRaises(mes.BusinessError):
            configure(snapshot, {})

    def test_existing_database_cannot_be_overwritten(self):
        path = self.root / "mes.db"
        path.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            engine.seed_database(path, fixture())
        self.assertEqual(path.read_bytes(), b"do not overwrite")


if __name__ == "__main__":
    unittest.main()
