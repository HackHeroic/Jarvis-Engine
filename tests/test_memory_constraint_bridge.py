# tests/test_memory_constraint_bridge.py
"""Tests for converting memories into OR-Tools TimeSlot constraints."""

import pytest
from unittest.mock import MagicMock
from app.services.memory.constraint_bridge import memories_to_constraints


def test_no_memories_returns_empty():
    mock_store = MagicMock()
    mock_store.get_memories_by_type.return_value = []
    result = memories_to_constraints("u1", mock_store)
    assert result == []


def test_constraint_memory_becomes_timeslot():
    """A 'constraint' memory like 'No work after 6 PM' should produce a TimeSlot."""
    mock_store = MagicMock()
    mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: (
        [{"id": "m1", "content": "No tasks between 14:00 and 15:00 (has class)", "confidence": 0.9}]
        if mtype == "constraint" else []
    )

    result = memories_to_constraints("u1", mock_store)
    # Should produce at least one TimeSlot
    assert len(result) >= 1
    slot = result[0]
    assert slot.start_min == 840  # 14:00 = 14*60
    assert slot.end_min == 900    # 15:00 = 15*60
    assert slot.availability == "blocked"
    assert slot.source == "user"


def test_pattern_memory_becomes_soft_block():
    """A 'behavioral_pattern' memory should produce a soft (minimal_work) constraint."""
    mock_store = MagicMock()
    mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: (
        [{"id": "m2", "content": "User avoids tasks during hour 8 (skipped 85%)", "confidence": 0.85}]
        if mtype == "behavioral_pattern" else []
    )

    result = memories_to_constraints("u1", mock_store)
    assert len(result) >= 1
    slot = result[0]
    assert slot.start_min == 480  # 8:00 = 8*60
    assert slot.end_min == 540    # 9:00
    assert slot.availability == "minimal_work"
    assert slot.source == "pearl_inferred"


def test_mixed_constraints_and_patterns():
    """Both explicit constraints and inferred patterns should be returned."""
    mock_store = MagicMock()
    mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: {
        "constraint": [{"id": "c1", "content": "No tasks between 14:00 and 15:00 (has class)", "confidence": 0.9}],
        "behavioral_pattern": [{"id": "p1", "content": "User avoids tasks during hour 8 (skipped 85%)", "confidence": 0.85}],
    }.get(mtype, [])

    result = memories_to_constraints("u1", mock_store)
    assert len(result) == 2
    sources = {s.source for s in result}
    assert "user" in sources
    assert "pearl_inferred" in sources
