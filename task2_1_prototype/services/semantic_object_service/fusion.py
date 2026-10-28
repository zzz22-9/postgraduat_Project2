"""Numeric observation alignment and explicit fault lifecycle rules (no generator truth)."""
from copy import deepcopy
import re

from .models import MappingError

SIGNALS = ("spindle_speed_rpm", "temperature_c", "vibration_mm_s", "load_ratio")
PLC_STEP = dict(zip(SIGNALS, (1.0, .01, .01, .0001)))
DEFAULT_RULES = {"window_size": 10, "temperature_warning": 72.0, "vibration_warning": 3.8,
                 "load_warning": .82, "temperature_fault": 90.0, "vibration_fault": 8.0, "load_fault": .95}


def classify(values, rules):
    if (values.get("temperature_c", float("-inf")) > rules["temperature_fault"]
            or values.get("vibration_mm_s", 0) > rules["vibration_fault"]
            or values.get("load_ratio", 0) > rules["load_fault"]):
        return "fault"
    if values.get("load_ratio", 0) > rules["load_warning"]:
        return "overload"
    if (values.get("temperature_c", float("-inf")) > rules["temperature_warning"]
            or values.get("vibration_mm_s", 0) > rules["vibration_warning"]):
        return "warning"
    if len(values) < 4:
        return "unknown"
    return "idle" if values["spindle_speed_rpm"] <= 1 and values["load_ratio"] < .05 else "running"


def latch_fault(state, status, timestamp):
    if not state.get("fault_latched"):
        state["fault_count"] = state.get("fault_count", 0) + 1
        state["fault_started_at"] = timestamp
        state.pop("recovery_pending_since", None)
    state["fault_latched"] = True
    # A milder overload observation cannot downgrade a latched fault.
    state["status"] = "fault" if status == "fault" or state.get("status") == "fault" else "overload"


def maybe_recover(state):
    pending = state.get("recovery_pending_since")
    if (state.get("fault_latched") and pending and state.get("signal_complete")
            and state.get("latest_signal_timestamp", "") >= pending
            and state.get("signal_health") in ("running", "idle")):
        state["fault_latched"] = False
        state["status"] = "running"
        state["recovery_count"] = state.get("recovery_count", 0) + 1
        state["recovered_at"] = state["latest_signal_timestamp"]
        state["recovery_log_timestamp"] = pending
        state["last_fault_codes"] = state.pop("active_fault_codes", [])
        state.pop("recovery_pending_since", None)


def align_signal(store, raw, object_id, scope):
    payload, protocol, timestamp = raw["raw_payload"], raw["protocol"], raw["timestamp"]
    if protocol not in ("MQTT", "OPC_UA", "PLC_FRAME"):
        raise MappingError("SIGNAL_PROTOCOL", "signal fusion requires MQTT, OPC_UA or PLC_FRAME")
    # Deliberate whitelist: neither injected_fault nor control_cmd is read here.
    values = {name: payload[name] for name in SIGNALS if name in payload}
    if not values or any(type(value) not in (int, float) for value in values.values()):
        raise MappingError("SIGNAL_VALUE", "signals must contain finite numeric metrics")
    if protocol != "OPC_UA" and set(values) != set(SIGNALS):
        raise MappingError("SIGNAL_VALUE", "MQTT and PLC observations need all four metrics")
    if protocol == "OPC_UA" and len(values) != 1:
        raise MappingError("SIGNAL_VALUE", "one OPC named metric is expected")
    resolved = raw["raw_context"]["_ingest"]["resolved_ids"]
    instance = [resolved["work_order_id"], resolved["process_id"], raw["raw_context"].get("phase"), raw["raw_context"].get("rework_id")]
    cycle = store.cycle(object_id, scope, timestamp) or dict(timestamp=timestamp, instance=instance, observations={}, values={})
    if cycle["instance"] != instance:
        raise MappingError("SAMPLE_CONTEXT", "same device/time has conflicting scheduled process instances")
    observations = cycle["observations"]
    for metric, value in values.items():
        for other_protocol, other_values in observations.items():
            if metric not in other_values:
                continue
            tolerance = PLC_STEP[metric] / 2 + 1e-7 if "PLC_FRAME" in (protocol, other_protocol) and protocol != other_protocol else 1e-7
            if abs(value - other_values[metric]) > tolerance:
                raise MappingError("SIGNAL_CONFLICT", f"{metric} differs beyond protocol quantization tolerance")
    observations.setdefault(protocol, {}).update(values)
    # Full-precision sources win over quantized PLC. Never average the same physical observation six times.
    cycle["values"] = {}
    for name in SIGNALS:
        for preferred in ("MQTT", "OPC_UA", "PLC_FRAME"):
            if name in observations.get(preferred, {}):
                cycle["values"][name] = observations[preferred][name]
                break
    cycle["protocols"] = sorted(observations)
    cycle["complete"] = len(cycle["values"]) == 4
    store.put_cycle(object_id, scope, timestamp, cycle)
    return cycle


def apply_signal(store, raw, obj, scope, rules):
    state, context, timestamp = obj["state"], obj["context"], raw["timestamp"]
    cycle = align_signal(store, raw, obj["object_id"], scope)
    window = store.window(obj["object_id"], scope, timestamp, rules["window_size"])
    features = {}
    for name in SIGNALS:
        values = [c["values"][name] for c in window if name in c["values"]]
        if values:
            features[name] = {"mean": round(sum(values) / len(values), 8), "peak": max(values), "latest": values[0], "count": len(values)}
    state.update(cycle["values"])
    state.update(latest_signal=deepcopy(cycle["values"]), latest_signal_timestamp=timestamp,
                 signal_complete=cycle["complete"], signal_health=classify(cycle["values"], rules),
                 sample_count=store.cycle_count(obj["object_id"], scope, timestamp),
                 window_features=features, window_sample_count=len(window), window_size=rules["window_size"])
    context["observed_protocols"] = cycle["protocols"]
    context["signal_fusion"] = {"cycle_timestamp": timestamp, "policy": "MQTT_then_OPC_UA_then_PLC_FRAME",
                                "plc_quantization_step": PLC_STEP, "count_basis": "distinct_device_scope_timestamp"}
    health = state["signal_health"]
    if health in ("fault", "overload"):
        latch_fault(state, health, timestamp)
        if timestamp >= state.get("recovery_pending_since", "~"):
            state.pop("recovery_pending_since", None)
    elif not state.get("fault_latched"):
        state["status"] = health
    maybe_recover(state)


def apply_log(raw, state):
    payload, timestamp = raw["raw_payload"], raw["timestamp"]
    line = payload.get("log_message")
    if not isinstance(line, str) or not line:
        raise MappingError("LOG_TEXT", "a nonempty original log line is required")
    level = re.search(r"\[(INFO|WARN|ERROR)\]", line)
    if not level:
        raise MappingError("LOG_LEVEL", "log severity must be INFO/WARN/ERROR")
    codes = re.findall(r"\bCode\s+(0x[0-9a-fA-F]+)\b", line)
    action = payload.get("state_action", raw["raw_context"].get("state_action"))
    state["log"] = {"level": level[1], "fault_codes": codes, "message": line, "summary": line.split(": ", 1)[-1],
                    "state_action": action, "timestamp": timestamp}
    if action == "fault_cleared":
        if level[1] != "INFO":
            raise MappingError("RECOVERY_EVIDENCE", "fault_cleared requires an INFO recovery log")
        if state.get("fault_latched") and timestamp >= state.get("fault_started_at", ""):
            state["recovery_pending_since"] = timestamp
            maybe_recover(state)
    elif level[1] == "ERROR":
        latch_fault(state, "fault", timestamp)
        state["active_fault_codes"] = [code for code in codes if int(code, 16)]
    elif not state.get("fault_latched"):
        if level[1] == "WARN":
            state["status"] = "warning"
        elif "processing_finished" in line:
            state["status"] = "idle"
        elif "processing_started" in line:
            state["status"] = "running"


def image_state(payload):
    for key in ("image_ref", "sha256", "synthetic_label", "annotation_source"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise MappingError("IMAGE_METADATA", f"validated image metadata requires {key}")
    return {"image_ref": payload["image_ref"], "sha256": payload["sha256"],
            "synthetic_label": payload["synthetic_label"], "stage": payload.get("stage"),
            "annotation_source": "synthetic_label", "source_annotation": payload["annotation_source"]}
