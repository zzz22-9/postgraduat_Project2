"""Frozen JSON boundaries. No database, connector or semantic inference here."""
from datetime import datetime, timezone
import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

ObjectType = Literal["equipment", "process", "work_order", "order", "resource"]
Domain = Literal["manufacturing", "enterprise"]
Level = Literal["device", "line", "factory", "enterprise"]
Protocol = Literal["OPC_UA", "MQTT", "REST_API", "POSTGRESQL", "SQLITE", "PLC_FRAME"]
Modality = Literal["timeseries_signal", "image", "log_text", "structured_record"]
Identifier = Annotated[str, Field(min_length=1, pattern=r"\S")]
TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
Timestamp = Annotated[str, Field(pattern=TIME_PATTERN, json_schema_extra={"format": "date-time"})]


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(TIME_PATTERN, value):
        raise ValueError("timestamp must be a timezone-qualified ISO-8601 string")
    # datetime accepts an overflowing timezone minute, while RFC3339 does not.
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError("invalid timezone offset")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed


def utc_text(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_json(value):
    """Validate nested extension payloads too; NaN/Inf are never JSON numbers."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            require_json(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            require_json(item)
        return
    raise ValueError("only JSON values with finite numbers and string keys are permitted")


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def json_only(cls, value):
        require_json(value)
        return value

    @field_validator("timestamp", check_fields=False)
    @classmethod
    def valid_timestamp(cls, value):
        parse_timestamp(value)
        return value


class ObjectHint(StrictContract):
    raw_id: Identifier
    suggested_type: ObjectType
    domain_hint: Domain
    level_hint: Level


class RawIngestMessage(StrictContract):
    message_id: Identifier
    source_system: Literal["SIMPY_PLC", "MES", "ERP"]
    protocol: Protocol
    modality: Modality
    timestamp: Timestamp
    object_hint: ObjectHint
    raw_payload: dict[str, JsonValue]
    raw_context: dict[str, JsonValue]


class SemanticObject(StrictContract):
    object_id: Identifier
    object_type: ObjectType
    domain: Domain
    level: Level
    state: dict[str, JsonValue]
    context: dict[str, JsonValue]
    timestamp: Timestamp


class RelationEdge(StrictContract):
    relation_id: Identifier
    source: Identifier
    relation: Literal["executes", "belongs_to", "maps_to", "uses_resource", "located_in", "part_of", "managed_by"]
    target: Identifier
    weight: float = Field(ge=0, le=1)
    timestamp: Timestamp
    context: dict[str, JsonValue]
