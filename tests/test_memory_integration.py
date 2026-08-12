# tests/test_memory_integration.py
"""Integration tests for the full memory pipeline: extract → store → score → retrieve → bridge."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

from app.services.memory.store import MemoryStore
from app.services.memory.retriever import (
    compute_memory_strength,
    score_memory,
    format_memory_block,
    deduplicate_memories,
    IMPORTANCE_WEIGHTS,
)
from app.services.memory.constraint_bridge import memories_to_constraints, _parse_time_range
from app.schemas.memory import MemoryRecord, ExtractedMemory, MemoryExtractionResponse


class TestMemorySchemas:
    def test_memory_record_from_dict(self):
        record = MemoryRecord(
            id="m1", user_id="u1", memory_type="fact",
            content="CS student", confidence=0.8,
        )
        assert record.memory_type == "fact"
        assert record.confidence == 0.8
        assert record.stability == 1.0  # default

    def test_extracted_memory_validates_type(self):
        mem = ExtractedMemory(type="preference", content="Likes mornings")
        assert mem.type == "preference"

    def test_extraction_response_schema(self):
        resp = MemoryExtractionResponse(memories=[
            ExtractedMemory(type="fact", content="CS student"),
            ExtractedMemory(type="goal", content="Finish DSA by April"),
        ])
        assert len(resp.memories) == 2


class TestTimeRangeParsing:
    """Ranges come back in HORIZON minutes (0 = DAY_START_HOUR = 08:00), the
    frame TimeSlot and OR-Tools use — not wall-clock minutes from midnight."""

    def test_24h_format(self):
        result = _parse_time_range("No tasks between 14:00 and 15:00")
        assert result == (360, 420)  # 14:00-15:00 is 6-7h after 08:00

    def test_12h_format(self):
        result = _parse_time_range("Meeting from 2 PM to 3 PM")
        assert result == (360, 420)

    def test_after_pattern(self):
        result = _parse_time_range("No work after 6 PM")
        assert result == (600, 1440)  # 18:00 to the end of the 08:00-anchored day

    def test_before_pattern(self):
        result = _parse_time_range("No tasks before 10 AM")
        assert result == (0, 120)  # day start to 10:00, not an 8AM-6PM dead zone

    def test_no_time_returns_none(self):
        result = _parse_time_range("User prefers quiet study")
        assert result is None


class TestDeduplication:
    def test_removes_duplicates_by_id(self):
        memories = [
            {"id": "m1", "content": "A"},
            {"id": "m2", "content": "B"},
            {"id": "m1", "content": "A duplicate"},
        ]
        result = deduplicate_memories(memories)
        assert len(result) == 2

    def test_preserves_order(self):
        memories = [
            {"id": "m1", "content": "First"},
            {"id": "m2", "content": "Second"},
        ]
        result = deduplicate_memories(memories)
        assert result[0]["content"] == "First"


class TestStabilityCapIntegration:
    def test_stability_cap_at_20(self):
        """Reinforcing a memory 25 times should cap stability at 20."""
        mock_supabase = MagicMock()
        # get_memory chains: .select().eq(id).eq(user_id).execute()
        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"id": "m1", "stability": 19.5, "confidence": 0.9}
        ]
        # update_memory chains: .update().eq(id).eq(user_id).execute()
        mock_supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"id": "m1"}]

        store = MemoryStore(supabase_client=mock_supabase)
        store.reinforce_memory("m1", user_id="u1")

        # Check the update was called with capped stability
        update_call = mock_supabase.table.return_value.update.call_args
        updates = update_call[0][0] if update_call[0] else update_call[1]
        assert updates["stability"] == 20.0  # Capped, not 20.5


class TestConstraintBridgeIntegration:
    def test_constraint_and_pattern_both_produce_slots(self):
        """End-to-end: memories of different types produce different TimeSlot types."""
        mock_store = MagicMock()
        mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: {
            "constraint": [{"id": "c1", "content": "Meeting from 2 PM to 3 PM", "confidence": 0.9}],
            "behavioral_pattern": [{"id": "p1", "content": "User avoids tasks during hour 9 (skipped 80%)", "confidence": 0.8}],
        }.get(mtype, [])

        slots = memories_to_constraints("u1", mock_store)
        assert len(slots) == 2

        blocked = [s for s in slots if s.availability == "blocked"]
        soft = [s for s in slots if s.availability == "minimal_work"]
        assert len(blocked) == 1  # Explicit constraint
        assert len(soft) == 1     # PEARL pattern
        assert blocked[0].source == "user"
        assert soft[0].source == "pearl_inferred"
