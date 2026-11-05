"""从恢复的历史素材整理确定性的三设备样本，只读写素材文件。"""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "synthetic_signals" / "generated_v1"
IMAGES = ROOT / "synthetic_images" / "rendered_v1"
SOURCE_COMMIT = "d53fb7b9c5f4e2fcea520de2cd62a323e802fd07"
DATASET_ID = "FIELD-ASSETS-V1"
SAMPLE_COUNT = 12
DEVICES = (
    {"id": "E-ASM", "name": "CNC_01", "file": "cnc", "scale": 1.0,
     "base_current": 3.0, "base_temperature": 30.0, "temperature_gain": 26.0,
     "rated_current": 6.0, "limits": {"current_a": 4.0, "temperature_c": 70.0, "load_percent": 80.0}},
    {"id": "E-MOVE", "name": "Robot_01", "file": "robot", "scale": 0.8,
     "base_current": 2.4, "base_temperature": 28.0, "temperature_gain": 30.0,
     "rated_current": 4.0, "limits": {"current_a": 3.5, "temperature_c": 70.0, "load_percent": 90.0}},
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def source_samples():
    """校验原始电流文件，正常取前12条加工样本，过载取12条注入样本。"""
    summary = read_json(SIGNALS / "summary.json")
    selections = {}
    for scenario, filename in (("normal", "normal.jsonl"), ("overload", "injected_surge.jsonl")):
        path = SIGNALS / filename
        raw = path.read_bytes()
        if digest(raw) != summary[path.stem]["sha256"]:
            raise ValueError(f"原始信号哈希不符：{path}")
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        eligible = [row for row in rows if row["equipment_state"] == "processing"
                    and row["injected_anomaly"] == (scenario == "overload")]
        if len(eligible) < SAMPLE_COUNT:
            raise ValueError(f"{scenario} 的有效样本不足 {SAMPLE_COUNT} 条")
        selections[scenario] = (path.relative_to(ROOT).as_posix(), digest(raw), eligible[:SAMPLE_COUNT])
    return selections


def signal_outputs(selections):
    """原电流是合成模板，Robot电流及温度/负载是显式派生的教学特征。"""
    outputs, entries = {}, []
    for device in DEVICES:
        for scenario, (source_file, source_hash, samples) in selections.items():
            rows = []
            for index, source in enumerate(samples):
                current = round(source["value"] * device["scale"], 6)
                temperature = round(device["base_temperature"] +
                                    max(0, current - device["base_current"]) * device["temperature_gain"], 3)
                load = round(100 * current / device["rated_current"], 3)
                values = {"current_a": current, "temperature_c": temperature, "load_percent": load}
                alarm = int(any(values[key] > limit for key, limit in device["limits"].items()))
                if bool(alarm) != (scenario == "overload"):
                    raise ValueError(f"样本特征与场景不一致：{device['name']} {source['sample_id']}")
                row = {
                    "dataset_id": DATASET_ID,
                    "sample_id": f"{device['file']}_{scenario}_{index:04d}",
                    "equipment_id": device["id"], "equipment_name": device["name"],
                    "scenario": scenario, "sample_index": index, "sample_time_s": index,
                    "sample_interval_s": 1, "synthetic": True,
                    "feature_source": "deterministic_transform_of_synthetic_current",
                    **values, "alarm_code": alarm,
                    "source_file": source_file, "source_sha256": source_hash,
                    "source_sample_id": source["sample_id"], "source_time_s": source["sim_time_s"],
                    "template_equipment_id": source["equipment_id"],
                    "template_run_id": source["source_run_id"],
                }
                if device["id"] == "E-MOVE":
                    row["holding_registers"] = {
                        "40001": int(round(current * 1000)),
                        "40002": int(round(temperature * 10)),
                        "40003": int(round(load * 10)), "40004": alarm,
                    }
                rows.append(row)
            path = SIGNALS / f"{device['file']}_{scenario}.jsonl"
            raw = ("\n".join(json.dumps(row, ensure_ascii=False, allow_nan=False) for row in rows) + "\n").encode("utf-8")
            outputs[path] = raw
            entries.append({
                "equipment_id": device["id"], "equipment_name": device["name"], "scenario": scenario,
                "path": path.name, "sample_count": len(rows), "sha256": digest(raw),
                "thresholds": device["limits"],
                "ranges": {key: [min(row[key] for row in rows), max(row[key] for row in rows)]
                           for key in ("current_a", "temperature_c", "load_percent")},
                "feature_model": {"current_scale": device["scale"],
                                  "base_current_a": device["base_current"],
                                  "base_temperature_c": device["base_temperature"],
                                  "temperature_gain_c_per_a": device["temperature_gain"],
                                  "rated_current_a": device["rated_current"]},
            })
    return outputs, entries


def image_outputs(manifest):
    """复用已渲染的合格/缺螺栓PNG，为视觉站建立可直接读取的标准文件名。"""
    outputs, entries = {}, []
    for sample in manifest["samples"]:
        source = IMAGES / sample["image_path"]
        raw = source.read_bytes()
        if digest(raw) != sample["image_sha256"]:
            raise ValueError(f"原始图片哈希不符：{source}")
        if sample["state"] not in ("complete", "missing_bolt"):
            continue
        defective = sample["state"] == "missing_bolt"
        label = "defect" if defective else "normal"
        filename = f"{label}_{sample['view']}.png"
        outputs[IMAGES / filename] = raw
        entries.append({
            "dataset_id": DATASET_ID, "sample_id": f"vision_{label}_{sample['view']}",
            "equipment_id": "E-QC", "equipment_name": "Vision_01", "image_path": filename,
            "label": label, "defect_type": "missing_bolt" if defective else None,
            "view": sample["view"], "bolt_count": sample["bolt_count"],
            "missing_bolt_positions": sample["missing_bolt_positions"],
            "source_image_path": sample["image_path"], "source_sample_id": sample["sample_id"],
            "image_sha256": digest(raw), "synthetic": True,
            "annotation_source": "scene_configuration_not_image_recognition",
        })
    if len(entries) != 4:
        raise ValueError("视觉样本必须包含正常/缺陷的正面和侧面共4张图片")
    return outputs, entries


def main():
    signal_manifest = read_json(SIGNALS / "manifest.json")
    image_manifest = read_json(IMAGES / "manifest.json")
    outputs, signal_entries = signal_outputs(source_samples())
    images, image_entries = image_outputs(image_manifest)
    outputs.update(images)
    provenance = {"dataset_id": DATASET_ID, "source_git_commit": SOURCE_COMMIT,
                  "preparation_script": "tools/prepare_field_assets.py",
                  "preparation_script_sha256": digest(Path(__file__).read_bytes()),
                  "time_semantics": "sample_time_s is local asset time; bind to new run at consumption",
                  "business_binding": "template_run_id is provenance; assign current work order at consumption"}
    signal_manifest["field_library"] = {
        **provenance, "files": signal_entries,
        "units": {"current_a": "A", "temperature_c": "degC", "load_percent": "%"},
        "feature_formula": "current=template*scale; temperature=base+max(0,current-base_current)*gain; load=100*current/rated_current",
        "alarm_rule": "any feature strictly exceeds its threshold",
        "register_map": {"40001": "current_a * 1000", "40002": "temperature_c * 10",
                         "40003": "load_percent * 10", "40004": "alarm_code"},
        "register_addressing": "40001..40004 are holding-register reference labels; wire offsets 0..3",
    }
    image_manifest["field_library"] = provenance
    image_manifest["field_samples"] = image_entries
    outputs[SIGNALS / "manifest.json"] = json_bytes(signal_manifest)
    outputs[IMAGES / "manifest.json"] = json_bytes(image_manifest)
    # 全部输入验证完成后逐文件原子替换，重跑得到相同字节。
    for path, raw in outputs.items():
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
    print(json.dumps({"signal_files": signal_entries, "image_files": image_entries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
