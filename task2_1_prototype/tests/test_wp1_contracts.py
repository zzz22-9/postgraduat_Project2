import copy
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from task2_1_prototype.contracts import RawIngestMessage, RelationEdge, SemanticObject
from task2_1_prototype.contracts.export_schema import export
from task2_1_prototype.config import IdRegistry, load_config, simulation_timestamp

STAMP = "2026-10-28T02:30:00.000000Z"
SAMPLES = {
    RawIngestMessage: dict(message_id="M1", source_system="ERP", protocol="SQLITE", modality="structured_record",
        timestamp=STAMP, object_hint=dict(raw_id="O1", suggested_type="order", domain_hint="enterprise", level_hint="enterprise"),
        raw_payload={"quantity": 1}, raw_context={"sequence": 0}),
    SemanticObject: dict(object_id="CNC_01", object_type="equipment", domain="manufacturing", level="device",
        state={"load": .65}, context={"process": "OP20"}, timestamp=STAMP),
    RelationEdge: dict(relation_id="R1", source="CNC_01", relation="executes", target="OP20", weight=1.0,
        timestamp=STAMP, context={"work_order": "WO023"}),
}


class ContractTests(unittest.TestCase):
    def test_exact_fields_and_independent_json_schema_validation(self):
        for model, sample in SAMPLES.items():
            with self.subTest(model=model.__name__):
                result = model.model_validate(sample).model_dump(mode="json")
                self.assertEqual(set(result), set(sample))
                self.assertEqual(len(result), 8 if model is RawIngestMessage else 7)
                self.assertEqual(result, sample)
                validator = Draft202012Validator(model.model_json_schema(), format_checker=FormatChecker())
                validator.validate(result)
                self.assertFalse(model.model_json_schema()["additionalProperties"])
                self.assertEqual(set(model.model_json_schema()["required"]), set(sample))

    def test_missing_and_extra_fields_for_every_contract(self):
        for model, sample in SAMPLES.items():
            for field in sample:
                bad = {k: v for k, v in sample.items() if k != field}
                with self.subTest(model=model.__name__, missing=field), self.assertRaises(ValidationError):
                    model.model_validate(bad)
            with self.assertRaises(ValidationError):
                model.model_validate({**sample, "extra_field": 1})

    def test_enum_types_and_nested_hint_are_strict(self):
        cases = [(RawIngestMessage, "protocol", "HTTP"), (RawIngestMessage, "modality", "signal"),
                 (RawIngestMessage, "source_system", "unknown"), (SemanticObject, "object_type", "Equipment"),
                 (SemanticObject, "domain", "ERP"), (SemanticObject, "level", "shop"),
                 (SemanticObject, "state", "running"), (RelationEdge, "relation", "contains"),
                 (RelationEdge, "weight", "1.0"), (RelationEdge, "weight", True)]
        for model, field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                model.model_validate({**SAMPLES[model], field: value})
        bad = copy.deepcopy(SAMPLES[RawIngestMessage])
        bad["object_hint"]["unexpected"] = 1
        with self.assertRaises(ValidationError):
            RawIngestMessage.model_validate(bad)

    def test_time_validation_all_models_and_clock_conversion(self):
        invalid = ["2026-10-28", "2026-10-28T10:30:00", "2026-02-30T10:00:00Z", "2026-10-28T25:00:00Z",
                   "2026-10-28T10:00:00+08:99", 123, {"seconds": 0}]
        for model, sample in SAMPLES.items():
            for timestamp in invalid:
                with self.subTest(model=model.__name__, timestamp=timestamp), self.assertRaises(ValidationError):
                    model.model_validate({**sample, "timestamp": timestamp})
        self.assertEqual(simulation_timestamp("2026-10-28T10:30:00+08:00", 60), "2026-10-28T02:31:00.000000Z")

    def test_nonfinite_nested_numbers_rejected_for_all_models(self):
        for model, sample in SAMPLES.items():
            field = "raw_payload" if model is RawIngestMessage else "context"
            for number in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(model=model.__name__, number=number), self.assertRaises(ValidationError):
                    model.model_validate({**sample, field: {"nested": [{"value": number}]}})
        for number in (float("nan"), float("inf"), -1, 1.1):
            with self.assertRaises(ValidationError):
                RelationEdge.model_validate({**SAMPLES[RelationEdge], "weight": number})

    def test_exported_schemas_match_models(self):
        committed = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
        with tempfile.TemporaryDirectory() as temp:
            export(temp)
            for model in SAMPLES:
                name = f"{model.__name__}.schema.json"
                self.assertEqual((Path(temp) / name).read_bytes(), (committed / name).read_bytes())
                Draft202012Validator.check_schema(json.loads((committed / name).read_text()))

    def test_alias_registry_requires_work_order_context_and_prevents_collapse(self):
        registry = IdRegistry(load_config())
        for raw in ("E-ASM", "DEV_CNC_01", "0x01", "RES_M01"):
            self.assertEqual(registry.resolve("equipment", raw), "CNC_01")
        registry.bind("process", "OP-2", "OP20", "WO-A")
        registry.bind("process", "OP-2", "OP20_2", "WO-B")
        self.assertEqual(registry.resolve("process", "OP-2", "WO-B"), "OP20_2")
        with self.assertRaises(ValueError):
            registry.bind("process", "OP-3", "OP20", "WO-C")
        with self.assertRaises(ValueError):
            registry.bind("process", "OP-2", "OP20")

    def test_organization_config_keeps_five_object_types(self):
        for node in load_config()["hierarchy"]:
            obj = SemanticObject.model_validate({**node, "state": {"status": "configured"}, "timestamp": STAMP})
            if obj.level != "device":
                self.assertEqual(obj.object_type, "resource")
                self.assertEqual(obj.context["resource_kind"], "organization")
