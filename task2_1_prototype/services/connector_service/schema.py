"""WP1 source envelope and the unchanged, shared eight-field output contract."""
from typing import Literal

from pydantic import Field

from task2_1_prototype.contracts.models import (
    Identifier, Modality, ObjectHint, Protocol, RawIngestMessage, StrictContract, Timestamp,
)


class SourceEnvelope(StrictContract):
    sequence: int = Field(ge=0)
    source_system: Literal["ERP", "MES", "SIMPY_PLC"]
    protocol: Protocol
    modality: Modality
    channel: Identifier
    timestamp: Timestamp
    payload: dict
    context: dict
    object_hint: ObjectHint
    event_key: Identifier | None = None


__all__ = ["RawIngestMessage", "SourceEnvelope"]
