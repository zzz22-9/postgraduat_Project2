"""Explicit demo aliases; process keys include their parent work order."""
from copy import deepcopy
from datetime import timedelta
import json
import math
from pathlib import Path

from task2_1_prototype.contracts.models import parse_timestamp, require_json, utc_text
from task2_1_prototype.simulators.device_bindings import DEVICE_BINDINGS


def load_config(path=None):
    # 不传路径才使用新绑定；显式指定的历史/自定义配置保持文件中的编号。
    source = Path(__file__).with_name("demo.json") if path is None else Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if path is None:
        # 将“目标设备 -> 原MES资源”反向整理，编号只来自已有绑定表。
        targets = {binding["legacy_equipment_id"]: device_id
                   for device_id, binding in DEVICE_BINDINGS.items()}
        if len(targets) != len(DEVICE_BINDINGS):
            raise ValueError("设备绑定表中的原MES资源编号重复")
        renamed = {}
        for equipment in data["equipment"]:
            legacy_id = equipment["legacy_id"]
            if legacy_id not in targets:
                raise ValueError(f"默认配置中的MES设备未登记绑定：{legacy_id}")
            old_id = equipment["object_id"]
            device_id = targets[legacy_id]
            renamed[old_id] = device_id
            equipment["object_id"] = device_id
            # 保留旧编号和协议别名，补上目标编号；dict.fromkeys按原顺序去重。
            equipment["aliases"] = list(dict.fromkeys([*equipment["aliases"], old_id, device_id]))
        # 只同步已有引用，不新增工序、设备模型或层级节点。
        for node in data["hierarchy"]:
            node["object_id"] = renamed.get(node["object_id"], node["object_id"])
            context = node["context"]
            if "parent" in context:
                context["parent"] = renamed.get(context["parent"], context["parent"])
        fault = data["fault"]
        fault["equipment_id"] = renamed.get(fault["equipment_id"], fault["equipment_id"])
    validate_config(data)
    return data


def validate_config(data):
    require_json(data)
    parse_timestamp(data["simulation_epoch"])
    period = data["sample_interval_s"]
    if type(period) not in (int, float) or not .001 <= period <= 86400:
        raise ValueError("sample_interval_s must be in [0.001,86400]")
    if type(data["seed"]) is not int:
        raise ValueError("seed must be an integer")
    addresses = [e["address"] for e in data["equipment"]]
    if len(set(addresses)) != len(addresses) or any(type(a) is not int or not 1 <= a <= 247 for a in addresses):
        raise ValueError("duplicate/invalid equipment address")
    f = data["fault"]
    if not 0 <= f["start_offset_s"] < f["end_offset_s"]:
        raise ValueError("invalid fault interval")
    for values in [data["noise_amplitude"], f["values"], *(e["normal"] for e in data["equipment"])]:
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values.values()):
            raise ValueError("signal parameters must be finite and nonnegative")


def simulation_timestamp(epoch, seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("simulation seconds must be finite and nonnegative")
    return utc_text(parse_timestamp(epoch) + timedelta(seconds=seconds))


class IdRegistry:
    def __init__(self, config):
        self.config = deepcopy(config)
        self.entries = {}
        for e in config["equipment"]:
            for raw in [e["legacy_id"], *e["aliases"]]:
                self.bind("equipment", raw, e["object_id"])

    def bind(self, kind, raw_id, object_id, work_order=None):
        if kind == "process" and not work_order:
            raise ValueError("process mapping requires work-order context")
        key = (kind, work_order if kind == "process" else None, raw_id)
        old = self.entries.get(key)
        if old is not None and old != object_id:
            raise ValueError("conflicting ID mapping")
        # Device aliases may share identity. Business instances must not collapse.
        if kind != "equipment" and any(v == object_id and k != key for k, v in self.entries.items()):
            raise ValueError("canonical business ID already assigned")
        self.entries[key] = object_id

    def resolve(self, kind, raw_id, work_order=None):
        return self.entries[(kind, work_order if kind == "process" else None, raw_id)]

    def equipment(self, raw_id):
        canonical = self.resolve("equipment", raw_id)
        return next(e for e in self.config["equipment"] if e["object_id"] == canonical)

    def bind_work_order(self, work, ordinal=0):
        aliases = self.config["demo_aliases"]
        suffix = "" if ordinal == 0 else f"_{ordinal + 1}"
        self.bind("order", work["order_id"], aliases["order"] + suffix)
        self.bind("work_order", work["work_order_id"], aliases["work_order"] + suffix)
        for index, op in enumerate(work["operations"]):
            alias = aliases["processes"][index] if index < len(aliases["processes"]) else f"OP_{index+1}"
            self.bind("process", op["operation_id"], alias + suffix, work["work_order_id"])

    def records(self):
        return [dict(kind=k[0], work_order=k[1], raw_id=k[2], object_id=v)
                for k, v in sorted(self.entries.items(), key=lambda item: str(item[0]))]
