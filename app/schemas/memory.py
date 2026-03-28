"""Pydantic schemas for the memory system."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

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
    type: MemoryType
    content: str = Field(description="Concise statement of what was learned")
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    contradicts: str | None = Field(default=None, description="ID of existing memory this contradicts, or null")
    expires_at: str | None = Field(default=None, description="ISO date if temporal event, or null")


class MemoryExtractionResponse(BaseModel):
    """Response schema for the memory extraction LLM call."""
    memories: list[ExtractedMemory] = Field(default_factory=list)
