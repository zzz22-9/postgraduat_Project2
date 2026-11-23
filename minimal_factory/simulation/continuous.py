"""SimPy-time CNC/Robot *synthetic derived* telemetry, separate from original protocol frames.

Every in-process sample is derived from a verifiable, existing synthetic material row.
The series is useful for Connector and time-alignment tests, not a physical sensor model.
"""
from __future__ import annotations

import hashlib
import json
import math

SAMPLE_PERIOD_S = 1
SUPPORTED = frozenset(("E-ASM", "E-MOVE"))


def _base_metrics(message):
    payload = message["payload"]
    if message["equipment_id"] == "E-ASM":
        by_name = {node["node_id"].rsplit(".", 1)[-1]: node["value"] for node in payload["nodes"]}
        return {"current_a": by_name["SpindleCurrent"],
                "temperature_c": by_name["MotorTemperature"],
                "load_percent": by_name["SpindleLoad"],
                "alarm_code": by_name["AlarmCode"]}
    regs = payload["raw_registers"]
    return {"current_a": regs[0] / 1000, "temperature_c": regs[1] / 10,
            "load_percent": regs[2] / 10, "alarm_code": regs[3]}


def _intermediate_metrics(base, elapsed_s, duration_s):
    """Small deterministic variation, NOT a claim of real sensor dynamics."""
    phase = 2 * math.pi * elapsed_s / 17
    progress = elapsed_s / duration_s
    return {
        "current_a": round(max(0, base["current_a"] * (1 + 0.025 * math.sin(phase))), 6),
        "temperature_c": round(max(0, base["temperature_c"] + 0.8 * progress
                                     + 0.15 * math.sin(phase * 0.5)), 3),
        "load_percent": round(min(100, max(0, base["load_percent"]
                                           + 1.5 * math.sin(phase * 0.6))), 3),
        "alarm_code": 0,
    }


def _encode_payload(message, metrics):
    if message["equipment_id"] == "E-ASM":
        mapping = {"SpindleCurrent": "current_a", "MotorTemperature": "temperature_c",
                   "SpindleLoad": "load_percent", "AlarmCode": "alarm_code"}
        nodes = [dict(node, value=metrics[mapping[node["node_id"].rsplit(".", 1)[-1]]])
                 for node in message["payload"]["nodes"]]
        return {**message["payload"], "nodes": nodes}
    regs = [int(round(metrics["current_a"] * 1000)),
            int(round(metrics["temperature_c"] * 10)),
            int(round(metrics["load_percent"] * 10)), metrics["alarm_code"]]
    if any(n < 0 or n > 65535 for n in regs):
        raise ValueError("连续Robot寄存器值超出UInt16范围")
    p = message["payload"]
    unit = p["unit_id"]
    frame = bytes.fromhex("0001 0000 000B") + bytes([unit, 3, 8])
    frame += b"".join(n.to_bytes(2, "big") for n in regs)
    return {**p, "raw_registers": regs, "response_frame_hex": frame.hex(" ").upper()}


def make_record(message, source, context, sequence, virtual_minute, elapsed_s, duration_s,
                *, terminal=False):
    """A self-describing record; derived samples never masquerade as wire transmissions."""
    if message["equipment_id"] not in SUPPORTED:
        raise ValueError("连续采样暂只支持CNC和Robot")
    base = _base_metrics(message)
    metrics = base if terminal else _intermediate_metrics(base, elapsed_s, duration_s)
    payload = message["payload"] if terminal else _encode_payload(message, metrics)
    sample_key = [context[k] for k in ("run_id", "work_order_id", "operation_id", "phase", "rework_id")]
    sample_key.append(sequence)
    sample_id = "TEL-" + hashlib.sha256(json.dumps(sample_key, ensure_ascii=False).encode()).hexdigest()[:32]
    record = {
        "schema_version": "telemetry.v1", "telemetry_id": sample_id,
        "run_id": context["run_id"], "order_id": context["order_id"],
        "work_order_id": context["work_order_id"], "operation_id": context["operation_id"],
        "equipment_id": message["equipment_id"], "device_code": message["device_code"],
        "phase": context["phase"], "rework_id": context["rework_id"],
        "virtual_minute": round(virtual_minute, 9),
        "elapsed_s": round(elapsed_s, 6), "sample_sequence": sequence,
        "sample_period_s": SAMPLE_PERIOD_S,
        "protocol": message["protocol"], "transport_mode": "local_derived",
        "synthetic": True, "is_anomaly": bool(message["is_anomaly"]) if terminal else False,
        "metrics": metrics, "payload": payload,
        "source_file": message["source_file"], "sample_hash": message["sample_hash"],
        "reference_sample_index": source.get("sample_index"),
        "source_sample_time_s": source.get("sample_time_s"),
        "source_time_s": source.get("source_time_s"),
        "generation_method": "existing_terminal_protocol_sample" if terminal else "deterministic_template_variation_v1",
        "terminal_message_id": message["message_id"] if terminal else None,
    }
    return record
