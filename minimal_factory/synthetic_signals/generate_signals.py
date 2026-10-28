"""Generate deterministic synthetic current-envelope samples from a saved timeline.

The original SimPy events are read only. This is not measured current or a rerun.
Python standard library generates JSONL; Pillow draws the optional preview.
"""
import argparse
import hashlib
import json
import math
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "SIM-97bdfc0915964e5ba0b0a33c3466a301"
SOURCE = ROOT / "data" / "erp.simulations" / SOURCE_ID / "events.jsonl"
PARAMETERS = dict(seed=20261026, sample_interval_s=1, idle_mean_A=.2,
                  running_mean_A=3., running_ripple_amplitude_A=.12,
                  ripple_period_s=12., running_noise_std_A=.04, idle_noise_std_A=.01,
                  injected_interval_s=[240, 252], injected_increment_A=2.5,
                  demonstration_threshold_A=4.)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def intervals_from(events):
    intervals, active = [], None
    for event in sorted(events, key=lambda e: (e["sim_time_min"], e["sequence"])):
        if event["equipment_id"] != "E-ASM":
            continue
        kind = event["event_type"]
        if kind == "processing_started":
            if active is not None:
                raise ValueError("Overlapping processing on E-ASM")
            active = event
        elif kind == "processing_finished":
            if active is None or active["operation_id"] != event["operation_id"]:
                raise ValueError("Unmatched processing finish")
            start, end = active["sim_time_min"] * 60, event["sim_time_min"] * 60
            if end <= start:
                raise ValueError("Nonpositive processing duration")
            intervals.append(dict(start_s=start, end_s=end,
                                  order_id=active["order_id"], work_order_id=active["work_order_id"],
                                  operation_id=active["operation_id"],
                                  start_event_sequence=active["sequence"], end_event_sequence=event["sequence"]))
            active = None
    if active is not None or not intervals:
        raise ValueError("Incomplete processing timeline")
    return intervals


def generate(intervals, end_s, scenario):
    rng = random.Random(PARAMETERS["seed"])
    rows = []
    for second in range(end_s + 1):
        # Half-open intervals resolve same-time finish/start to the new job.
        context = next((i for i in intervals if i["start_s"] <= second < i["end_s"]), None)
        if context:
            value = (PARAMETERS["running_mean_A"]
                     + PARAMETERS["running_ripple_amplitude_A"] * math.sin(2 * math.pi * second / PARAMETERS["ripple_period_s"])
                     + rng.gauss(0, PARAMETERS["running_noise_std_A"]))
        else:
            value = PARAMETERS["idle_mean_A"] + rng.gauss(0, PARAMETERS["idle_noise_std_A"])
        low, high = PARAMETERS["injected_interval_s"]
        injected = scenario == "injected_surge" and low <= second < high
        if injected:
            if context is None:
                raise ValueError("Injection interval must lie within active processing")
            value += PARAMETERS["injected_increment_A"]
        rows.append(dict(sample_id=f"{scenario}-{second:04d}",
                         dataset_id="SYNTH-CURRENT-V1", run_id=f"SYNTH-CURRENT-V1-{scenario}",
                         source_run_id=SOURCE_ID, source_type="synthetic_event_replay", synthetic=True,
                         scenario=scenario, equipment_id="E-ASM", sim_time_s=second,
                         sim_time_min=round(second / 60, 8), signal="current_envelope", unit="A",
                         value=round(max(value, 0.), 6), equipment_state="processing" if context else "idle",
                         order_id=context["order_id"] if context else None,
                         work_order_id=context["work_order_id"] if context else None,
                         operation_id=context["operation_id"] if context else None,
                         source_start_event_sequence=context["start_event_sequence"] if context else None,
                         injected_anomaly=injected))
    return rows


def preview(out, data):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1200, 710), "#f4f7fa")
    draw = ImageDraw.Draw(image)
    font = lambda size: ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)
    draw.text((35, 20), "组装设备电流 · 合成信号对照", fill="#183247", font=font(28))
    draw.text((35, 64), "1秒采样 / 0—9分钟 / 基于保存事件回放 / 数值为教学假设", fill="#516a7f", font=font(18))
    for index, (scenario, rows) in enumerate(data.items()):
        left, top, width, height = 85, 150 + index * 275, 1050, 185
        xy = lambda t, value: (left + t / 540 * width, top + height - value / 6 * height)
        for a, b, color, label in [(0, 180, "#e4eef8", "订单1加工"), (180, 360, "#e2f1ed", "订单2加工"), (360, 540, "#e9edf1", "设备空闲")]:
            draw.rectangle([xy(a, 6), xy(b, 0)], fill=color)
            draw.text((xy(a, 6)[0] + 12, top - 28), label, fill="#40586b", font=font(16))
        for value in [0, 2, 4, 6]:
            y = xy(0, value)[1]
            draw.line([(left, y), (left + width, y)], fill="#cad4dd", width=1)
            draw.text((42, y - 11), str(value), fill="#40586b", font=font(16))
        for minute in range(10):
            x, y = xy(minute * 60, 0)
            draw.text((x - 5, y + 5), str(minute), fill="#40586b", font=font(16))
        draw.text((35, top - 28), "A", fill="#40586b", font=font(18))
        threshold_y = xy(0, 4)[1]
        for x in range(left, left + width, 16):
            draw.line([(x, threshold_y), (x + 8, threshold_y)], fill="#b9815c", width=1)
        if scenario == "injected_surge":
            draw.rectangle([xy(240, 6), xy(252, 0)], fill="#f5d9c5")
        draw.line([xy(r["sim_time_s"], r["value"]) for r in rows], fill="#126c9a" if index == 0 else "#b65e2d", width=2)
        draw.text((85, top + height + 34), "正常基线" if index == 0 else "人为注入：第4.0—4.2分钟增加2.5 A（12秒）", fill="#183247", font=font(17))
        draw.text((1075, top + height + 34), "时间/分钟", fill="#40586b", font=font(15))
    draw.text((35, 678), "虚线：4 A演示阈值；非设备额定参数。异常标签来自注入规则，不是算法诊断。", fill="#62788a", font=font(16))
    image.save(out / "preview.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "generated_v1")
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args()
    before = digest(SOURCE)
    events = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if {e["run_id"] for e in events} != {SOURCE_ID}:
        raise ValueError("Source run mismatch")
    intervals = intervals_from(events)
    end_s = max(e["sim_time_min"] for e in events) * 60
    if end_s != int(end_s):
        raise ValueError("This sample generator expects an integer-second end time")
    data = {name: generate(intervals, int(end_s), name) for name in ["normal", "injected_surge"]}
    normal, surge = data.values()
    assert [(i["start_s"], i["end_s"]) for i in intervals] == [(0, 180), (180, 360)]
    assert normal[179]["order_id"] == "DEMO-ORD-1" and normal[180]["order_id"] == "DEMO-ORD-2"
    assert normal[359]["equipment_state"] == "processing" and normal[360]["order_id"] is None
    assert all(math.isfinite(r["value"]) and r["value"] >= 0 for rows in data.values() for r in rows)
    assert normal == generate(intervals, int(end_s), "normal")
    changed = [a["sim_time_s"] for a, b in zip(normal, surge) if a["value"] != b["value"]]
    assert changed == list(range(240, 252))
    assert all(abs((b["value"] - a["value"]) - 2.5) < 1e-5 for a, b in zip(normal, surge) if b["injected_anomaly"])
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for name, rows in data.items():
        path = out / f"{name}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        readback = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert readback == rows
        summaries[name] = dict(sample_count=len(rows), state_counts=dict(Counter(r["equipment_state"] for r in rows)),
                               minimum_A=min(r["value"] for r in rows), maximum_A=max(r["value"] for r in rows),
                               above_4A_count=sum(r["value"] > 4 for r in rows), sha256=digest(path))
    write_json(out / "timeline.json", intervals)
    write_json(out / "summary.json", summaries)
    write_json(out / "manifest.json", dict(dataset_id="SYNTH-CURRENT-V1", source_run_id=SOURCE_ID,
               source_events_path=str(SOURCE.relative_to(ROOT)), source_events_sha256=before,
               generated_at=datetime.now(timezone.utc).isoformat(), script_sha256=digest(Path(__file__)),
               parameters=PARAMETERS, sample_range_s=[0, int(end_s)], endpoint_included=True,
               meaning="Synthetic low-rate current envelope, not an AC waveform or measured RMS current",
               time_semantics="processing intervals [start,end); all same-time source events applied before sample",
               context_semantics="Source business IDs are replay references; run_id identifies new synthetic scenario",
               image_binding=None, database_ingested=False))
    if not args.no_preview:
        preview(out, data)
    assert digest(SOURCE) == before
    write_json(out / "validation.json", dict(total_samples=sum(len(rows) for rows in data.values()),
               finite_nonnegative=True, boundary_180s_and_360s_correct=True, deterministic_regeneration=True,
               injection_only_240_through_251s=True, jsonl_roundtrip=True, source_events_unchanged=True,
               business_tests_run=False, simpy_rerun=False))
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
