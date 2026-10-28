"""WP1 runnable source demo. No connectors, semantic mapping or graph service."""
import argparse
from copy import deepcopy
import hashlib
from pathlib import Path
import random
import threading

from task2_1_prototype.config import IdRegistry, load_config, simulation_timestamp, validate_config
from .erp_simulator import create_demo_order
from .legacy_bridge import independent_path, load_legacy
from .mes_simulator import prepare_work_order
from .protocol_emulator import ProtocolEmitter, SIGNALS, append_json, write_json
from .visuals import render_frame


class FactoryObserver:
    def __init__(self, folder, config, registry, emitter):
        validate_config(config)
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "sim_images").mkdir(exist_ok=True)
        self.config, self.registry, self.emitter = deepcopy(config), registry, emitter
        self.sequence = 0
        self.counts = dict(signals=0, images=0, logs=0, faults=0, recoveries=0)
        self.last = {}

    def context(self, raw, seconds):
        self.sequence += 1
        work = raw["work_order_id"]
        context = dict(clock="simulation", simulation_epoch=self.config["simulation_epoch"],
                       sim_time_s=round(seconds, 9), sequence=self.sequence, run_id=raw["run_id"],
                       enterprise_id="ENT_01", factory_id="FAC_01", line_id="LINE_01",
                       order_id=self.registry.resolve("order", raw["order_id"]),
                       work_order_id=self.registry.resolve("work_order", work),
                       phase=raw.get("phase", "normal"), rework_id=raw.get("rework_id"),
                       synthetic=True, legacy_ids={k: raw.get(k) for k in ("order_id", "work_order_id", "operation_id", "equipment_id")})
        if raw.get("operation_id"):
            context["process_id"] = self.registry.resolve("process", raw["operation_id"], work)
            context["equipment_id"] = self.registry.resolve("equipment", raw["equipment_id"])
        return context

    def hint(self, raw):
        return dict(raw_id=raw["equipment_id"], suggested_type="equipment", domain_hint="manufacturing", level_hint="device")

    def log(self, raw, seconds, severity, code, message, action=None):
        context = self.context(raw, seconds)
        timestamp = simulation_timestamp(self.config["simulation_epoch"], seconds)
        equipment = self.registry.equipment(raw["equipment_id"])
        line = (f"[{timestamp}] [PLC-0x{equipment['address']:02X}] [{severity}] Code {code}: "
                f"{message} on {context['equipment_id']} while executing {context['process_id']} "
                f"for {context['work_order_id']} order {context['order_id']}")
        with (self.folder / "equipment.log").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
        payload = dict(log_message=line, synthetic=True)
        if action:
            payload["state_action"] = action
        append_json(self.folder / "device_logs.jsonl", dict(timestamp=timestamp, context=context, **payload))
        self.emitter.emit("SIMPY_PLC", "MQTT", "log_text", f"factory/{context['equipment_id']}/logs",
                          timestamp, payload, context, self.hint(raw))
        self.counts["logs"] += 1

    def image(self, raw, seconds, signals, stage, label):
        context = self.context(raw, seconds)
        timestamp = simulation_timestamp(self.config["simulation_epoch"], seconds)
        name = f"sim_images/frame_{self.counts['images']:05d}_{stage}.png"
        metadata = dict(timestamp=timestamp, sim_time_s=round(seconds, 9), signals=signals,
                        stage=stage, synthetic_label=label, synthetic=True,
                        annotation_source="generator_metadata_not_vision", context=context,
                        **{k: context[k] for k in ("equipment_id", "process_id", "work_order_id", "order_id")})
        path = self.folder / name
        render_frame(path, metadata)
        record = dict(image_ref=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), **metadata)
        append_json(self.folder / "images.jsonl", record)
        self.emitter.emit("SIMPY_PLC", "MQTT", "image", f"factory/{context['equipment_id']}/images",
                          timestamp, record, context, self.hint(raw))
        self.counts["images"] += 1

    @staticmethod
    def operation_key(raw):
        return (raw["work_order_id"], raw["operation_id"], raw.get("phase", "normal"), raw.get("rework_id"))

    def on_event(self, event):
        seconds = event["sim_time_min"] * 60
        context = self.context(event, seconds)
        timestamp = simulation_timestamp(self.config["simulation_epoch"], seconds)
        is_process = bool(event.get("operation_id"))
        hint = dict(raw_id=event["operation_id"] if is_process else event["work_order_id"],
                    suggested_type="process" if is_process else "work_order", domain_hint="manufacturing",
                    level_hint="device" if is_process else "factory")
        # Original recorded_at remains in legacy events.jsonl. This stream uses event time.
        payload = {k: v for k, v in event.items() if k != "recorded_at"}
        self.emitter.emit("SIMPY_PLC", "MQTT", "structured_record", "factory/process/events",
                          timestamp, payload, context, hint)
        if event["event_type"] in ("processing_started", "processing_finished"):
            self.log(event, seconds, "INFO", "0x00", event["event_type"])
        if event["event_type"] == "quality_result":
            values = self.last[self.operation_key(event)]
            label = "pass" if event["rejected_quantity"] == 0 else "reject"
            self.image(event, seconds, values, "quality", label)

    def observe(self, env, raw, completed, stop):
        equipment = self.registry.equipment(raw["equipment_id"])
        process = self.registry.resolve("process", raw["operation_id"], raw["work_order_id"])
        # Independent stream per operation/phase, stable across interleaving and replays.
        key = f"{self.config['seed']}:{equipment['object_id']}:{process}:{raw['phase']}"
        rng = random.Random(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big"))
        period = self.config["sample_interval_s"]
        fault = self.config["fault"]
        eligible = (fault["enabled"] and fault["equipment_id"] == equipment["object_id"]
                    and fault["process_id"] == process and fault["phase"] == raw["phase"])
        duration_s = (raw["end_min"] - raw["start_min"]) * 60
        if eligible and fault["end_offset_s"] > duration_s:
            raise ValueError("configured fault lies outside active operation")
        index, was_fault, middle = 0, False, False
        # SimPy Timeout.triggered is already True at construction; processed means fired.
        while env.now < raw["end_min"] and not completed.processed:
            if stop.is_set():
                _, _, _, _, engine, _ = load_legacy()
                raise engine.Interrupted("telemetry interrupted")
            seconds = env.now * 60
            offset = index * period
            injected = bool(eligible and fault["start_offset_s"] <= offset < fault["end_offset_s"])
            # Always draw the same noise values; toggling fault does not shift RNG state.
            values = {}
            for name in SIGNALS:
                noise = rng.uniform(-self.config["noise_amplitude"][name], self.config["noise_amplitude"][name])
                base = fault["values"][name] if injected else equipment["normal"][name]
                value = max(0, base + noise) if base else 0.0
                values[name] = round(min(1, value) if name == "load_ratio" else value, 4)
            context = self.context(raw, seconds)
            context.update(active_start_s=raw["start_min"]*60, active_end_s=raw["end_min"]*60)
            timestamp = simulation_timestamp(self.config["simulation_epoch"], seconds)
            sample = dict(timestamp=timestamp, equipment_id=equipment["object_id"], signals=values,
                          context=context, injected_fault=injected)
            append_json(self.folder / "signals.jsonl", sample)
            self.emitter.telemetry(sample, equipment["address"], self.hint(raw))
            self.last[self.operation_key(raw)] = values
            self.counts["signals"] += 1
            if index == 0:
                self.image(raw, seconds, values, "start", "fault" if injected else "normal")
            if injected and not was_fault:
                self.log(raw, seconds, "ERROR", fault["code"], "Spindle temperature/vibration alarm", "fault")
                self.image(raw, seconds, values, "alarm", "fault")
                self.counts["faults"] += 1
            if was_fault and not injected:
                self.log(raw, seconds, "INFO", "0x00", f"RECOVERED from Code {fault['code']}", "fault_cleared")
                self.image(raw, seconds, values, "recovery", "normal")
                self.counts["recoveries"] += 1
            if not middle and offset >= duration_s / 2:
                self.image(raw, seconds, values, "processing", "fault" if injected else "normal")
                middle = True
            was_fault = injected
            index += 1
            next_min = raw["start_min"] + index * period / 60
            # Never construct an orphan timeout after completion: env.run drains all events.
            if next_min >= raw["end_min"] - 1e-12:
                yield completed
                return
            yield env.timeout(next_min - env.now)


def run_demo(output, config=None):
    config = deepcopy(config or load_config())
    validate_config(config)
    output = independent_path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", config)
    write_json(output / "run.json", {"status": "running", "scope": "WP1 sources only"})
    try:
        emitter = ProtocolEmitter(output)
        registry = IdRegistry(config)
        db = output / "demo_erp.sqlite3"
        order = create_demo_order(db, config, emitter)
        work, snapshot = prepare_work_order(db, order, registry, emitter)
        write_json(output / "id_registry.json", registry.records())
        write_json(output / "input.json", snapshot)
        observer = FactoryObserver(output, config, registry, emitter)
        _, _, _, _, engine, _ = load_legacy()
        summary = engine.simulate(output, "WP1_DEMO", snapshot, threading.Event(), lambda *_: None, observer=observer)
        write_json(output / "summary.json", summary)
        emitter.frame_examples(config["equipment"][0]["normal"])
        result = dict(status="completed", scope="WP1 sources only; WP2 not implemented",
                      makespan_min=summary["makespan_min"], counts=observer.counts, protocols=emitter.counts,
                      transport_modes={"REST_API": "in_process_asgi", "SQLITE": "real_local_db",
                                       "MQTT": "memory_pubsub_mock", "OPC_UA": "named_node_mock", "PLC_FRAME": "custom_binary"},
                      legacy_work_order=work["work_order_id"], output=str(output))
        write_json(output / "run.json", result)
        return result
    except Exception as error:
        write_json(output / "run.json", {"status": "failed", "error": str(error), "scope": "WP1 sources only"})
        raise


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New output folder; existing paths are refused")
    parser.add_argument("--config")
    parser.add_argument("--normal", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.normal:
        config["fault"]["enabled"] = False
    print(json.dumps(run_demo(args.output, config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
