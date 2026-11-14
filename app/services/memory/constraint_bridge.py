# app/services/memory/constraint_bridge.py
"""Bridge between archival memory and the deterministic scheduler.

This is what makes Jarvis different from ChatGPT's memory:
- ChatGPT memory: affects what the LLM says
- Jarvis memory: affects the MATHEMATICAL CONSTRAINTS in OR-Tools

A behavioral pattern like "user skips morning tasks" doesn't just make
Jarvis say "I notice you prefer afternoons" — it makes the scheduler
STOP SCHEDULING deep work before 10 AM.
"""

import re

from app.core.config import DAY_START_HOUR
from app.domain.entities import Memory
from app.schemas.context import TimeSlot
from app.services.memory.patterns import SkipTimeWindowDetector

MINUTES_PER_DAY = 1440
DAY_START_MIN = DAY_START_HOUR * 60  # horizon minute 0 == 08:00 on the user's clock
# A learned skip window at least this sure becomes a minimal-work hour.
SKIP_WINDOW_MIN_CONFIDENCE = 0.6


def _to_horizon(clock_min: int) -> int:
    """Wall-clock minutes-from-midnight -> horizon minutes (0 = DAY_START_HOUR).

    Every consumer of ``TimeSlot`` — the horizon expander, the biological sleep
    fallback in ``run_schedule``, OR-Tools itself — anchors minute 0 at 08:00.
    Emitting minutes-from-midnight put every memory constraint 8 hours late.
    """
    return (clock_min - DAY_START_MIN) % MINUTES_PER_DAY


def _parse_time_range(text: str) -> tuple[int, int] | None:
    """Extract a time range from natural language constraint text.

    Handles patterns like:
    - "between 14:00 and 15:00"
    - "from 2 PM to 3 PM"
    - "No tasks between 14:00 and 15:00"

    Returns ``(start_min, end_min)`` in **horizon minutes** (0 = ``DAY_START_HOUR``,
    i.e. 08:00), the frame ``TimeSlot`` is defined in — or None when the text
    carries no clock time, or names a window that lies entirely outside the
    schedulable day.
    """
    # Pattern: HH:MM and HH:MM
    match_24h = re.search(r"(\d{1,2}):(\d{2})\s*(?:and|to|-)\s*(\d{1,2}):(\d{2})", text)
    if match_24h:
        start_h, start_m = int(match_24h.group(1)), int(match_24h.group(2))
        end_h, end_m = int(match_24h.group(3)), int(match_24h.group(4))
        return _bounded_range(start_h * 60 + start_m, end_h * 60 + end_m)

    # Pattern: N AM/PM to N AM/PM
    match_12h = re.search(
        r"(\d{1,2})\s*(AM|PM|am|pm)\s*(?:to|and|-)\s*(\d{1,2})\s*(AM|PM|am|pm)", text
    )
    if match_12h:
        start_h = int(match_12h.group(1))
        start_period = match_12h.group(2).upper()
        end_h = int(match_12h.group(3))
        end_period = match_12h.group(4).upper()

        if start_period == "PM" and start_h != 12:
            start_h += 12
        if start_period == "AM" and start_h == 12:
            start_h = 0
        if end_period == "PM" and end_h != 12:
            end_h += 12
        if end_period == "AM" and end_h == 12:
            end_h = 0

        return _bounded_range(start_h * 60, end_h * 60)

    # Pattern: "after N PM" → N PM to the end of the schedulable day.
    match_after = re.search(r"after\s+(\d{1,2})\s*(AM|PM|am|pm)?", text)
    if match_after:
        h = int(match_after.group(1))
        period = (match_after.group(2) or "").upper()
        if period == "PM" and h != 12:
            h += 12
        if not period and 1 <= h <= 7:
            # Bare small hour: PM is both the likelier reading ("after 6" = the
            # evening) and the *less destructive* one — 06:00 would block the
            # entire day. When guessing, guess the interpretation that blocks less.
            h += 12
        # Times at or before day start block everything schedulable, hence max(0, ...).
        return (max(0, h * 60 - DAY_START_MIN), MINUTES_PER_DAY)

    # Pattern: "before N AM/PM" → start of the schedulable day to N.
    match_before = re.search(r"before\s+(\d{1,2})\s*(AM|PM|am|pm)?", text)
    if match_before:
        h = int(match_before.group(1))
        period = (match_before.group(2) or "").upper()
        if period == "PM" and h != 12:
            h += 12
        # NOTE: no bare-hour "+12" here — the mirror image of the "after" rule.
        # For "before", PM is the *maximally* destructive guess: bare "before 6"
        # read as 18:00 blocks 08:00-18:00, the 10-hour dead zone this parser
        # used to emit. Read as 06:00 it blocks nothing schedulable. Again:
        # when guessing, guess the interpretation that blocks less.
        end = h * 60 - DAY_START_MIN
        if end <= 0:
            return None  # entirely before the day starts — nothing to block
        return (0, end)

    return None


def _bounded_range(start_clock: int, end_clock: int) -> tuple[int, int] | None:
    """Convert a wall-clock window to horizon minutes, dropping unusable ones.

    A window that wraps across the 08:00 anchor (or is inverted/empty) cannot be
    expressed as one interval in horizon space. Dropping it is the safe move: a
    zero- or negative-length block would reach OR-Tools as a malformed interval,
    and a wrapped one lands almost entirely inside the sleep window anyway.
    """
    start, end = _to_horizon(start_clock), _to_horizon(end_clock)
    if end <= start:
        return None
    return (start, end)


def _parse_hour_from_pattern(text: str) -> int | None:
    """Extract hour from a PEARL pattern like 'avoids tasks during hour 8'."""
    match = re.search(r"hour\s+(\d{1,2})", text)
    if match:
        return int(match.group(1))
    return None


def memories_to_constraints(user_id: str, memory_store) -> list[TimeSlot]:
    """Convert relevant memories into TimeSlot constraints for OR-Tools (legacy, v1).

    The v2 planning path uses ``memory_slots`` over ``MemoryService.list_active``.

    Called during _run_plan_day_flow, BEFORE run_schedule.
    Queries both explicit constraints and PEARL-inferred behavioral patterns —
    and *only* those two types. A goal memory ("tidy the inbox this evening")
    says what the user wants to do, not when they are unavailable; letting one
    block time would carve a hole out of the very day it was meant to fill.

    All emitted slots carry horizon minutes (0 = DAY_START_HOUR) and are marked
    ``recurring``: these are standing rules ("never studies before 11am"), not
    one-off calendar entries.
    """
    constraints: list[TimeSlot] = []

    # 1. Explicit constraints (user stated)
    explicit = memory_store.get_memories_by_type(user_id, "constraint")
    for mem in explicit:
        time_range = _parse_time_range(mem.get("content", ""))
        if time_range:
            start_min, end_min = time_range
            constraints.append(TimeSlot(
                name=f"memory_constraint_{mem.get('id', '')}",
                start_min=start_min,
                end_min=end_min,
                availability="blocked",
                recurring=True,
                source="user",
            ))

    # 2. PEARL behavioral patterns (system inferred)
    patterns = memory_store.get_memories_by_type(
        user_id, "behavioral_pattern", min_confidence=0.6
    )
    for pattern in patterns:
        content = pattern.get("content", "")

        # Pattern: "avoids tasks during hour X"
        hour = _parse_hour_from_pattern(content)
        if hour is not None:
            # PEARL reports wall-clock hours; TimeSlot wants horizon minutes.
            # Derive the end from the start so an hour that straddles the 08:00
            # anchor (hour 7 -> 1380-1440) stays a forward-going interval.
            start_min = _to_horizon(hour * 60)
            constraints.append(TimeSlot(
                name=f"pearl_pattern_{pattern.get('id', '')}",
                start_min=start_min,
                end_min=start_min + 60,
                availability="minimal_work",
                recurring=True,
                source="pearl_inferred",
            ))

        # Pattern: time range in pattern text
        time_range = _parse_time_range(content)
        if time_range and hour is None:  # Don't duplicate if hour already matched
            start_min, end_min = time_range
            constraints.append(TimeSlot(
                name=f"pearl_pattern_{pattern.get('id', '')}",
                start_min=start_min,
                end_min=end_min,
                availability="minimal_work",
                recurring=True,
                source="pearl_inferred",
            ))

    return constraints


def _skip_window_hour(pattern_key: str | None) -> int | None:
    """The local hour of a ``skip_time_window:{h}`` pattern key; None for any other key."""
    kind, _, hour = (pattern_key or "").partition(":")
    if kind != SkipTimeWindowDetector.name or not hour.isdigit() or int(hour) > 23:
        return None
    return int(hour)


def memory_slots(memories: list[Memory]) -> list[TimeSlot]:
    """Turn the user's memories in use into solver constraints.

    A ``constraint`` memory that names a clock range becomes a hard block. A learned
    skip window (pattern key ``skip_time_window:{h}``) at least 0.6 sure becomes a
    minimal-work hour. Nothing else blocks time: not a goal or a fact, and not any
    other pattern. A peak hour is where demanding work belongs, and reading "hour N"
    off a pattern's words used to turn it into a minimal-work window.

    Slots carry horizon minutes (0 = DAY_START_HOUR) and are ``recurring``: these are
    standing rules, not one-off calendar entries.
    """
    slots: list[TimeSlot] = []
    for memory in memories:
        if memory.memory_type == "constraint":
            time_range = _parse_time_range(memory.content)
            if time_range:
                slots.append(TimeSlot(
                    name=f"memory_constraint_{memory.id}",
                    start_min=time_range[0],
                    end_min=time_range[1],
                    availability="blocked",
                    recurring=True,
                    source="user",
                ))
        elif (
            memory.memory_type == "behavioral_pattern"
            and memory.confidence >= SKIP_WINDOW_MIN_CONFIDENCE
        ):
            hour = _skip_window_hour(memory.pattern_key)
            if hour is not None:
                # As in the legacy bridge: the end follows the start, so hour 7
                # (1380-1440) stays a forward-going interval.
                start_min = _to_horizon(hour * 60)
                slots.append(TimeSlot(
                    name=f"pearl_pattern_{memory.id}",
                    start_min=start_min,
                    end_min=start_min + 60,
                    availability="minimal_work",
                    recurring=True,
                    source="pearl_inferred",
                ))
    return slots
