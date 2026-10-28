"""Frozen object contract and WP3 validation; no eighth top-level field."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from task2_1_prototype.contracts.models import RawIngestMessage, SemanticObject

SCHEMA_ROOT = Path(__file__).parents[2] / "contracts/schemas"
VALIDATORS = {name: Draft202012Validator(json.loads((SCHEMA_ROOT / f"{name}.schema.json").read_text(encoding="utf-8")),
                                        format_checker=FormatChecker())
              for name in ("RawIngestMessage", "SemanticObject")}


class MappingError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def validate_raw(value):
    # Revalidate even model_construct/mutated instances at the boundary.
    if isinstance(value, RawIngestMessage):
        value = value.model_dump(mode="json")
    result = RawIngestMessage.model_validate(value).model_dump(mode="json")
    VALIDATORS["RawIngestMessage"].validate(result)
    return result


def validate_object(value):
    result = SemanticObject.model_validate(value).model_dump(mode="json")
    VALIDATORS["SemanticObject"].validate(result)
    return result


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
