"""Pydantic schemas for the memory system."""

import math
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

MemoryType = Literal[
    "fact", "preference", "behavioral_pattern",
    "temporal_event", "goal", "feedback", "constraint",
]


class MemoryRecord(BaseModel):
    """A single memory from the user_memories table."""
    id: str
    user_id: str
    memory_type: MemoryType
    content: str
    source: str = "conversation"
    source_id: str | None = None
    confidence: float = 0.5
    strength: float = 1.0
    stability: float = 1.0
    last_accessed: datetime | None = None
    last_reinforced: datetime | None = None
    superseded_by: str | None = None
    expires_at: datetime | None = None
    observation_count: int = 1
    applied_as: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    embedding: list[float] | None = None


class ExtractedMemory(BaseModel):
    """A memory extracted from a conversation turn by the LLM."""
    type: MemoryType = Field(description="What kind of memory it is")
    content: str = Field(description="Concise statement of what was learned")
    confidence: float = Field(ge=0.0, le=1.0, default=0.5, description="How plainly it was said, 0-1")
    contradicts: str | None = Field(
        default=None, description='Handle of the known memory this replaces, such as "m3", or null'
    )
    expires_at: str | None = Field(default=None, description="ISO date if temporal event, or null")

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> Any:
        """A number outside 0-1 is clamped into it; anything else is left to validation."""
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return min(1.0, max(0.0, float(value)))
        return value


def valid_memories(value: Any) -> list[ExtractedMemory]:
    """The items of ``value`` that are valid memories; anything else is dropped, alone.

    A model's extraction is judged memory by memory: one malformed memory (or a
    ``memories`` that is not a list at all) never costs the rest of the extraction.
    """
    if not isinstance(value, list):
        return []
    kept = []
    for item in value:
        try:
            kept.append(ExtractedMemory.model_validate(item))
        except ValidationError:
            continue
    return kept


class MemoryExtractionResponse(BaseModel):
    """Response schema for the memory extraction LLM call."""
    memories: list[ExtractedMemory] = Field(default_factory=list)
