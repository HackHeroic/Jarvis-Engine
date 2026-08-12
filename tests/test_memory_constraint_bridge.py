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
    # Horizon minutes, anchored at DAY_START_HOUR: 14:00 is 6h after 08:00.
    assert slot.start_min == 360
    assert slot.end_min == 420
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
    # Wall-clock hour 8 is 08:00 — horizon minute 0, the start of the day.
    assert slot.start_min == 0
    assert slot.end_min == 60
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


class TestHorizonAnchoredParsing:
    """``_parse_time_range`` must return HORIZON minutes, not wall-clock minutes.

    ``TimeSlot.start_min`` is anchored at ``DAY_START_HOUR`` (08:00) — minute 0 of
    the horizon is 8 AM, the convention ``SemanticTimeSlot`` documents and the
    horizon expander, the biological sleep fallback and OR-Tools all use. The
    parser returned minutes from *midnight*, so every memory constraint landed 8
    hours late: "does not take calls before 10am" became the block 0-600, read by
    the solver as 08:00-18:00 — a 10-hour dead zone in the middle of the day.
    """

    def test_before_11am__blocks_day_start_to_11(self):
        from app.services.memory.constraint_bridge import _parse_time_range

        assert _parse_time_range("The user never studies before 11am.") == (0, 180)

    def test_before_10am__blocks_day_start_to_10(self):
        from app.services.memory.constraint_bridge import _parse_time_range

        assert _parse_time_range("The user does not take calls before 10am.") == (0, 120)

    def test_after_6pm__blocks_evening_tail_only(self):
        from app.services.memory.constraint_bridge import _parse_time_range

        # 18:00 is horizon minute 600; the block runs to the end of the 8 AM day.
        assert _parse_time_range("No work after 6 PM") == (600, 1440)

    def test_explicit_range__converted_to_horizon_space(self):
        from app.services.memory.constraint_bridge import _parse_time_range

        assert _parse_time_range("No tasks between 14:00 and 15:00") == (360, 420)
        assert _parse_time_range("Meeting from 2 PM to 3 PM") == (360, 420)

    def test_before_time_earlier_than_day_start__no_constraint(self):
        from app.services.memory.constraint_bridge import _parse_time_range

        # Nothing in the schedulable day happens before 07:00; blocking 0 minutes
        # is the honest answer, not a wrapped or clamped block.
        assert _parse_time_range("Never schedules anything before 7 AM") is None

    def test_bare_before_small_hour__reads_as_am_not_pm(self):
        """The "+12 for small hours" heuristic is safe for 'after', ruinous for 'before'.

        Bare "after 6" almost certainly means the evening, and reading it as 06:00
        would block the entire day — so PM is the *less* destructive guess there.
        Bare "before 6" is the mirror image: PM blocks 08:00-18:00, AM blocks
        nothing schedulable. Both branches now pick the reading that blocks less.
        """
        from app.services.memory.constraint_bridge import _parse_time_range

        assert _parse_time_range("wants it done before 6") is None
        assert _parse_time_range("no deep work after 6") == (600, 1440)

    def test_goal_prose_without_a_clock_time__no_constraint(self):
        from app.services.memory.constraint_bridge import _parse_time_range

        assert _parse_time_range("tidy their email inbox this evening") is None
        assert _parse_time_range("wants to finish the report tomorrow morning") is None


class TestBridgeMemoryTypeFiltering:
    def test_goal_memories_never_become_constraints(self):
        """Only 'constraint' and 'behavioral_pattern' memories may block time.

        A goal memory ("tidy their email inbox ... this evening") describes what
        the user wants to *do*, not when they are unavailable. The bridge must
        never query it — a goal that happens to mention a clock time would
        otherwise carve a hard block out of the day it was meant to fill.
        """
        queried = []
        mock_store = MagicMock()

        def _by_type(uid, mtype, **kw):
            queried.append(mtype)
            return {
                "goal": [{"id": "g1", "content": "tidy their email inbox before 6", "confidence": 0.9}],
                "preference": [{"id": "pr1", "content": "prefers to read before 9pm", "confidence": 0.9}],
                "fact": [{"id": "f1", "content": "has a lecture before 10am", "confidence": 0.9}],
            }.get(mtype, [])

        mock_store.get_memories_by_type.side_effect = _by_type

        assert memories_to_constraints("u1", mock_store) == []
        assert set(queried) == {"constraint", "behavioral_pattern"}

    def test_pattern_confidence_gate_stays_at_0_6(self):
        """Behavioural patterns are inferred, so they keep the >=0.6 gate."""
        calls = {}
        mock_store = MagicMock()

        def _by_type(uid, mtype, **kw):
            calls[mtype] = kw
            return []

        mock_store.get_memories_by_type.side_effect = _by_type
        memories_to_constraints("u1", mock_store)

        assert calls["behavioral_pattern"]["min_confidence"] == 0.6

    def test_standing_rules_are_recurring(self):
        """"Never studies before 11am" is a standing rule, not a one-off block."""
        mock_store = MagicMock()
        mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: {
            "constraint": [{"id": "c1", "content": "The user never studies before 11am.", "confidence": 0.9}],
            "behavioral_pattern": [{"id": "p1", "content": "User avoids tasks during hour 8 (skipped 85%)", "confidence": 0.85}],
        }.get(mtype, [])

        slots = memories_to_constraints("u1", mock_store)

        assert len(slots) == 2
        assert all(s.recurring for s in slots)

    def test_pearl_hour_pattern__converted_to_horizon_space(self):
        """'avoids tasks during hour 8' is 8 AM — horizon minute 0, not 480."""
        mock_store = MagicMock()
        mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: (
            [{"id": "p1", "content": "User avoids tasks during hour 8 (skipped 85%)", "confidence": 0.85}]
            if mtype == "behavioral_pattern" else []
        )

        slot = memories_to_constraints("u1", mock_store)[0]

        assert (slot.start_min, slot.end_min) == (0, 60)
