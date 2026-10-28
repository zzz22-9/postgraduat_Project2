"""Explicit demo aliases; process keys include their parent work order."""
from copy import deepcopy
from datetime import timedelta
import json
import math
from pathlib import Path

from task2_1_prototype.contracts.models import parse_timestamp, require_json, utc_text


def load_config(path=None):
    data = json.loads(Path(path or Path(__file__).with_name("demo.json")).read_text(encoding="utf-8"))
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
