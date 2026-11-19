"""校验协议原生结构与可解码帧，不把本地生成等同网络传输。"""
import hashlib
import json
import struct
import unittest
from pathlib import Path

from simulation import field_protocols as field


class NativeProtocolTests(unittest.TestCase):
    def test_source_opt_in_keeps_existing_message_return(self):
        for builder, args in (
            (field.build_mqtt_dispatch_message, ("WO-1", "OP-1", "E-ASM", 0, 2)),
            (field.build_cnc_opcua_message, ("WO-1", "OP-1", 6)),
            (field.build_vision_http_message, ("WO-1", "OP-2", 10)),
            (field.build_robot_modbus_message, ("WO-1", "OP-3", 12)),
        ):
            with self.subTest(builder=builder.__name__):
                original = builder(*args)
                message, source = builder(*args, return_source=True)
                self.assertIsInstance(original, dict)
                self.assertEqual(message, original)
                self.assertEqual(source["message_id"], original["message_id"])

    def test_mqtt_publish_payload_bytes_match_json_and_target_topic(self):
        m = field.build_mqtt_dispatch_message("WO-1", "OP-1", "E-ASM", 0, 2)
        p = m["payload"]
        self.assertEqual(m["endpoint"], "mqtt://127.0.0.1:1883")
        self.assertEqual((p["packet_type"], p["topic"], p["qos"], p["retain"]),
                         ("PUBLISH", "factory/line1/CNC_01/command", 1, False))
        self.assertEqual(json.loads(bytes.fromhex(p["payload_bytes_hex"])), p["payload_json"])
        self.assertEqual(p["payload_json"]["quantity"], 2)

    def test_opcua_subscribed_nodes_have_types_and_status_and_read_real_sample(self):
        for anomaly in (False, True):
            m = field.build_cnc_opcua_message("WO-1", "OP-1", 6, is_anomaly=anomaly)
            sample = json.loads(Path(m["source_file"]).read_bytes().splitlines()[0])
            nodes = m["payload"]["nodes"]
            self.assertEqual(nodes[0]["node_id"], "ns=2;s=Channel1.CNC_01.SpindleCurrent")
            self.assertEqual(nodes[0]["data_type"], "Float")
            self.assertEqual(nodes[0]["value"], sample["current_a"])
            self.assertEqual(nodes[0]["status_code"].startswith("0x803C"), anomaly)
            self.assertNotIn("values", m["payload"])

    def test_vision_native_detections_reference_actual_png(self):
        m = field.build_vision_http_message("WO-1", "OP-2", 16, is_anomaly=True)
        p = m["payload"]
        self.assertEqual(m["endpoint"], "http://127.0.0.1:8000/mes/vision/webhook")
        self.assertEqual(p["image_url"], "/static/rendered_v1/defect_front.png")
        self.assertEqual(p["detections"], [{"label": "bolt", "count": 3, "expected": 4, "PASS": False}])
        self.assertEqual(hashlib.sha256(Path(m["source_file"]).read_bytes()).hexdigest(), m["sample_hash"])

    def test_modbus_mbap_pdu_and_register_scaling_match_source(self):
        m = field.build_robot_modbus_message("WO-1", "OP-3", 23, is_anomaly=True)
        p = m["payload"]
        raw = bytes.fromhex(p["response_frame_hex"])
        transaction, protocol, length, unit, function, byte_count = struct.unpack(">HHHBBB", raw[:9])
        self.assertEqual((transaction, protocol, length, unit, function, byte_count), (1, 0, 11, 1, 3, 8))
        self.assertEqual(length, len(raw)-6)
        self.assertEqual(list(struct.unpack(">4H", raw[9:])), p["raw_registers"])
        request = bytes.fromhex(p["request_frame_hex"])
        self.assertEqual(struct.unpack(">HHHBBHH", request), (1, 0, 6, 1, 3, 0, 4))
        self.assertEqual(p["start_address"], 40001)
        sample = json.loads(Path(m["source_file"]).read_bytes().splitlines()[0])
        for value, key, scale in zip(p["raw_registers"], ["current_a", "temperature_c", "load_percent", "alarm_code"], [1000,10,10,1]):
            self.assertAlmostEqual(value/scale, sample[key], delta=1/scale)
            self.assertNotIn(key, p)
        self.assertTrue(m["is_anomaly"])
