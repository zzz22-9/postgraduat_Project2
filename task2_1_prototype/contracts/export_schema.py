"""Run from repository root: python -m task2_1_prototype.contracts.export_schema."""
import json
from pathlib import Path

from .models import RawIngestMessage, RelationEdge, SemanticObject


def export(destination=None):
    destination = Path(destination or Path(__file__).parent / "schemas")
    destination.mkdir(parents=True, exist_ok=True)
    for model in (RawIngestMessage, SemanticObject, RelationEdge):
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        (destination / f"{model.__name__}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return destination


if __name__ == "__main__":
    print(export())
