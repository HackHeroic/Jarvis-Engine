# tests/test_memory_constraint_bridge.py
"""Tests for converting memories into OR-Tools TimeSlot constraints."""

import pytest


class TestHorizonAnchoredParsing:
    """``parse_time_range`` must return HORIZON minutes, not wall-clock minutes.

    ``TimeSlot.start_min`` is anchored at ``DAY_START_HOUR`` (08:00) — minute 0 of
    the horizon is 8 AM, the convention ``SemanticTimeSlot`` documents and the
    horizon expander, the biological sleep fallback and OR-Tools all use. The
    parser returned minutes from *midnight*, so every memory constraint landed 8
    hours late: "does not take calls before 10am" became the block 0-600, read by
    the solver as 08:00-18:00 — a 10-hour dead zone in the middle of the day.
    """

    def test_before_11am__blocks_day_start_to_11(self):
        from app.services.memory.constraint_bridge import parse_time_range

        assert parse_time_range("The user never studies before 11am.") == (0, 180)

    def test_before_10am__blocks_day_start_to_10(self):
        from app.services.memory.constraint_bridge import parse_time_range

        assert parse_time_range("The user does not take calls before 10am.") == (0, 120)

    def test_after_6pm__blocks_evening_tail_only(self):
        from app.services.memory.constraint_bridge import parse_time_range

        # 18:00 is horizon minute 600; the block runs to the end of the 8 AM day.
        assert parse_time_range("No work after 6 PM") == (600, 1440)

    def test_explicit_range__converted_to_horizon_space(self):
        from app.services.memory.constraint_bridge import parse_time_range

        assert parse_time_range("No tasks between 14:00 and 15:00") == (360, 420)
        assert parse_time_range("Meeting from 2 PM to 3 PM") == (360, 420)

    def test_before_time_earlier_than_day_start__no_constraint(self):
        from app.services.memory.constraint_bridge import parse_time_range

        # Nothing in the schedulable day happens before 07:00; blocking 0 minutes
        # is the honest answer, not a wrapped or clamped block.
        assert parse_time_range("Never schedules anything before 7 AM") is None

    def test_bare_before_small_hour__reads_as_am_not_pm(self):
        """The "+12 for small hours" heuristic is safe for 'after', ruinous for 'before'.

        Bare "after 6" almost certainly means the evening, and reading it as 06:00
        would block the entire day — so PM is the *less* destructive guess there.
        Bare "before 6" is the mirror image: PM blocks 08:00-18:00, AM blocks
        nothing schedulable. Both branches now pick the reading that blocks less.
        """
        from app.services.memory.constraint_bridge import parse_time_range

        assert parse_time_range("wants it done before 6") is None
        assert parse_time_range("no deep work after 6") == (600, 1440)

    def test_goal_prose_without_a_clock_time__no_constraint(self):
        from app.services.memory.constraint_bridge import parse_time_range

        assert parse_time_range("tidy their email inbox this evening") is None
        assert parse_time_range("wants to finish the report tomorrow morning") is None


# --- memory_slots: the bridge, over Memory entities -------------------------------------


def _memory(memory_type: str, content: str, **fields):
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.domain.entities import Memory, MemorySource

    now = datetime(2026, 11, 16, 2, 30, tzinfo=timezone.utc)
    return Memory(
        id=str(uuid4()),
        user_id="u1",
        memory_type=memory_type,
        content=content,
        provenance=fields.pop("provenance", MemorySource.INFERRED),
        strength_as_of=now,
        created_at=now,
        **fields,
    )


def test_memory_slots__peak_hour_pattern__never_blocks_that_hour():
    """A best-work hour is where demanding work belongs; reading "hour 19" off the text
    used to turn it into a minimal-work window."""
    from app.services.memory.constraint_bridge import memory_slots

    peak = _memory(
        "behavioral_pattern",
        "User does their best work around hour 19 (4 of 4 done)",
        pattern_key="peak_hour:19",
        confidence=0.9,
    )
    crunch = _memory(
        "behavioral_pattern",
        "User tends to finish deadlined work in the last day (3 of 4)",
        pattern_key="deadline_crunch",
        confidence=0.75,
    )

    assert memory_slots([peak, crunch]) == []


def test_memory_slots__skip_pattern__minimal_work_hour():
    from app.services.memory.constraint_bridge import memory_slots

    skips = _memory(
        "behavioral_pattern",
        "User avoids tasks during hour 9 (3 of 3 set aside)",
        pattern_key="skip_time_window:9",
        confidence=0.9,
    )
    unsure = _memory(
        "behavioral_pattern",
        "User avoids tasks during hour 14 (3 of 5 set aside)",
        pattern_key="skip_time_window:14",
        confidence=0.55,  # below the 0.6 gate
    )

    (slot,) = memory_slots([skips, unsure])

    # 09:00 local is horizon minute 60 (minute 0 is 08:00).
    assert (slot.start_min, slot.end_min) == (60, 120)
    assert slot.availability == "minimal_work"
    assert (slot.recurring, slot.source) == (True, "pearl_inferred")
    assert slot.name == f"pearl_pattern_{skips.id}"


def test_memory_slots__constraint_memory__hard_block():
    from app.domain.entities import MemorySource
    from app.services.memory.constraint_bridge import memory_slots

    rule = _memory(
        "constraint",
        "No tasks between 14:00 and 15:00 (has class)",
        provenance=MemorySource.SAID,
        confidence=0.9,
    )
    goal = _memory("goal", "Tidy the inbox before 6 PM", provenance=MemorySource.SAID)

    (slot,) = memory_slots([rule, goal])

    assert (slot.start_min, slot.end_min) == (360, 420)
    assert slot.availability == "blocked"
    assert (slot.recurring, slot.source) == (True, "user")
    assert slot.name == f"memory_constraint_{rule.id}"


def test_memory_slots__habit_claims_the_hour_for_deep_work__no_skip_block_there():
    """The user's own rule beats what Jarvis inferred: an hour they keep for deep work is
    never made a minimal-work hour because of past set-asides there."""
    from app.services.memory.constraint_bridge import memory_slots

    skips_9 = _memory(
        "behavioral_pattern", "User avoids tasks during hour 9 (3 of 3 set aside)",
        pattern_key="skip_time_window:9", confidence=0.9,
    )
    skips_14 = _memory(
        "behavioral_pattern", "User avoids tasks during hour 14 (3 of 3 set aside)",
        pattern_key="skip_time_window:14", confidence=0.9,
    )

    (slot,) = memory_slots([skips_9, skips_14], habits=["Deep work 9-10 every morning"])

    assert (slot.start_min, slot.end_min, slot.availability) == (360, 420, "minimal_work")


def test_memory_slots__said_rule_claims_the_hour__neither_blocked_nor_soft_blocked():
    from app.domain.entities import MemorySource
    from app.services.memory.constraint_bridge import memory_slots

    claim = _memory(
        "constraint", "Deep work from 9 AM to 10 AM, phone off", provenance=MemorySource.SAID
    )
    no_study = _memory("constraint", "No study before 11 AM", provenance=MemorySource.SAID)
    skips_9 = _memory(
        "behavioral_pattern", "User avoids tasks during hour 9 (3 of 3 set aside)",
        pattern_key="skip_time_window:9", confidence=0.9,
    )

    assert memory_slots([claim, skips_9]) == []
    # A rule that keeps study out of the morning claims nothing: both still apply.
    assert [(s.availability, s.start_min, s.end_min) for s in memory_slots([no_study, skips_9])] == [
        ("blocked", 0, 180),
        ("minimal_work", 60, 120),
    ]


@pytest.mark.asyncio
async def test_planning_memory_to_constraints__habit_keeps_the_hour__no_skip_block_in_the_plan():
    """The planning node hands the bridge the user's habits along with their memories."""
    import app.core.config as cfg
    from app.container import AppContainer
    from app.modules.planning_graph import memory_to_constraints
    from app.orchestrator.hooks import ActionHooks
    from tests.fakes import BagOfWordsEmbedder

    container = AppContainer.build(
        supabase_client=None, cfg=cfg, hooks=ActionHooks(), embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )
    skips_9 = _memory(
        "behavioral_pattern", "User avoids tasks during hour 9 (3 of 3 set aside)",
        pattern_key="skip_time_window:9", confidence=0.9,
    )
    await container.repos.memories.add("u1", skips_9)
    deep_work = {"raw_text": "Deep work 9-10 every morning", "constraint_type": "habit"}

    planning = container.planning  # the sub-graph's collaborators, as planning_state_in copies them
    without = await memory_to_constraints({"user_id": "u1", "constraints": [], "planning": planning})
    with_habit = await memory_to_constraints(
        {"user_id": "u1", "constraints": [deep_work], "planning": planning}
    )

    assert {s["start_min"] % 1440 for s in without["time_slots"]} == {60}
    assert "time_slots" not in with_habit
