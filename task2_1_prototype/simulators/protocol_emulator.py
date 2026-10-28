"""Inspectable local transports and the documented 18-byte PLC test frame."""
from collections import deque
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from pathlib import Path
import struct

from task2_1_prototype.contracts.models import parse_timestamp, require_json

SIGNALS = ("spindle_speed_rpm", "temperature_c", "vibration_mm_s", "load_ratio")


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS: init FFFF, reflected polynomial A001, xorout 0."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low},{high}]")
    return value


def scaled(value, scale, low, high, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} outside [{low},{high}]")
    return int((Decimal(str(value)) * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def encode_plc_frame(address, values, heartbeat, control=1) -> bytes:
    integer(address, 1, 247, "address")
    integer(heartbeat, 0, 65535, "heartbeat")
    integer(control, 0, 2, "control")
    if set(values) != set(SIGNALS):
        raise ValueError("PLC data requires exactly four signal fields")
    data = struct.pack(">HhHH", scaled(values[SIGNALS[0]], 1, 0, 65535, SIGNALS[0]),
                       scaled(values[SIGNALS[1]], 100, -327.68, 327.67, SIGNALS[1]),
                       scaled(values[SIGNALS[2]], 100, 0, 655.35, SIGNALS[2]),
                       scaled(values[SIGNALS[3]], 10000, 0, 1, SIGNALS[3]))
    body = b"\xaa\x55" + bytes((address, 3, len(data))) + data + struct.pack(">BH", control, heartbeat)
    return body + struct.pack("<H", crc16(body))


def decode_plc_frame(frame, known_addresses=(1, 2, 3)):
    if isinstance(frame, str):
        try:
            frame = bytes.fromhex(frame)
        except ValueError as exc:
            raise ValueError("invalid hexadecimal frame") from exc
    if not isinstance(frame, bytes):
        raise ValueError("frame must be bytes or a hexadecimal string")
    if len(frame) != 18:
        raise ValueError("frame length must be 18 bytes (truncated/extra data)")
    if frame[:2] != b"\xaa\x55" or frame[4] != 8:
        raise ValueError("invalid header or data length")
    if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
        raise ValueError("CRC mismatch")
    address, function = frame[2:4]
    if address not in known_addresses:
        raise ValueError(f"unknown device address: 0x{address:02X}")
    if function != 3 or frame[13] not in (0, 1, 2):
        raise ValueError("unknown function or control code")
    rpm, temp, vibration, load = struct.unpack(">HhHH", frame[5:13])
    if load > 10000:
        raise ValueError("load_ratio outside [0,1]")
    return dict(address=address, function_code=function, control=frame[13],
                heartbeat=struct.unpack(">H", frame[14:16])[0],
                signals=dict(zip(SIGNALS, (rpm, temp / 100, vibration / 100, load / 10000))))


def corrupt_frame(frame: bytes, kind: str) -> bytes:
    if kind == "truncated":
        return frame[:-1]
    if kind == "crc":
        return frame[:-1] + bytes((frame[-1] ^ 0x80,))
    if kind == "unknown_address":
        body = frame[:2] + b"\x7f" + frame[3:-2]
        return body + struct.pack("<H", crc16(body))
    raise ValueError("unknown corruption kind")


def topic_matches(pattern, topic):
    p, t = pattern.split("/"), topic.split("/")
    for index, part in enumerate(p):
        if part == "#":
            return True
        if index >= len(t) or part not in ("+", t[index]):
            return False
    return len(p) == len(t)


class MemoryMqttBroker:
    """In-process publish/subscribe only; no network broker, QoS or retain claim."""
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, topic_filter):
        parts = topic_filter.split("/")
        if not topic_filter or any(("#" in p and (p != "#" or i != len(parts)-1))
                                   or ("+" in p and p != "+") for i, p in enumerate(parts)):
            raise ValueError("invalid MQTT filter")
        queue = deque()
        self.subscriptions.append((topic_filter, queue))
        return queue

    def publish(self, topic, payload):
        if not topic or "+" in topic or "#" in topic:
            raise ValueError("invalid publish topic")
        require_json(payload)
        for pattern, queue in self.subscriptions:
            if topic_matches(pattern, topic):
                queue.append(deepcopy(dict(topic=topic, payload=payload)))


class OpcUaNodeStore:
    def __init__(self):
        self.nodes = {}

    def write(self, node_id, value, timestamp, status="Good"):
        parse_timestamp(timestamp)
        require_json(value)
        node = dict(NodeId=node_id, Value=value, StatusCode=status, SourceTimestamp=timestamp)
        self.nodes[node_id] = deepcopy(node)
        return node

    def read(self, node_id):
        return deepcopy(self.nodes[node_id])


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def append_json(path, data):
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


class ProtocolEmitter:
    """Source envelopes for WP2. Does not manufacture RawIngestMessage output."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.broker = MemoryMqttBroker()
        self.mqtt_received = self.broker.subscribe("#")
        self.opcua = OpcUaNodeStore()
        self.sequence = 0
        self.counts = {}
        self.heartbeats = {}

    def emit(self, source, protocol, modality, channel, timestamp, payload, context, hint):
        parse_timestamp(timestamp)
        require_json(payload)
        require_json(context)
        self.sequence += 1
        record = dict(sequence=self.sequence, source_system=source, protocol=protocol, modality=modality,
                      channel=channel, timestamp=timestamp, payload=payload, context=context, object_hint=hint)
        append_json(self.folder / "transport_records.jsonl", record)
        self.counts[protocol] = self.counts.get(protocol, 0) + 1
        if protocol == "MQTT":
            self.broker.publish(channel, record)
            # This local observer saves actual delivered messages, not just publish calls.
            while self.mqtt_received:
                append_json(self.folder / "mqtt_received.jsonl", self.mqtt_received.popleft())
        return record

    def telemetry(self, sample, address, hint):
        values, context, timestamp = sample["signals"], sample["context"], sample["timestamp"]
        device = sample["equipment_id"]
        self.emit("SIMPY_PLC", "MQTT", "timeseries_signal", f"factory/{device}/telemetry",
                  timestamp, values, context, hint)
        # StatusCode describes acquisition quality. A valid fault reading is still Good.
        for name, value in values.items():
            node = self.opcua.write(f"ns=2;s={device}.{name}", value, timestamp)
            self.emit("SIMPY_PLC", "OPC_UA", "timeseries_signal", node["NodeId"],
                      timestamp, self.opcua.read(node["NodeId"]), context, hint)
        heartbeat = self.heartbeats.get(address, 0)
        self.heartbeats[address] = (heartbeat + 1) % 65536
        frame = encode_plc_frame(address, values, heartbeat, 2 if sample["injected_fault"] else 1)
        with (self.folder / "plc_frames.bin").open("ab") as stream:
            offset = stream.tell()
            stream.write(frame)
        self.emit("SIMPY_PLC", "PLC_FRAME", "timeseries_signal", f"plc://0x{address:02X}", timestamp,
                  {"frame_hex": frame.hex(" ").upper(), "binary_file": "plc_frames.bin", "offset": offset, "length": 18},
                  context, hint)

    def frame_examples(self, values):
        frame = encode_plc_frame(1, values, 42)
        examples = {"normal": frame.hex(" ").upper()}
        for kind in ("truncated", "crc", "unknown_address"):
            examples[kind] = corrupt_frame(frame, kind).hex(" ").upper()
        write_json(self.folder / "plc_frame_examples.json", examples)
        return examples
