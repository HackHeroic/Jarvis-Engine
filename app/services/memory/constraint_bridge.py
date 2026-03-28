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

from app.schemas.context import TimeSlot


def _parse_time_range(text: str) -> tuple[int, int] | None:
    """Extract a time range from natural language constraint text.

    Handles patterns like:
    - "between 14:00 and 15:00"
    - "from 2 PM to 3 PM"
    - "No tasks between 14:00 and 15:00"

    Returns (start_min, end_min) or None if no time range found.
    """
    # Pattern: HH:MM and HH:MM
    match_24h = re.search(r"(\d{1,2}):(\d{2})\s*(?:and|to|-)\s*(\d{1,2}):(\d{2})", text)
    if match_24h:
        start_h, start_m = int(match_24h.group(1)), int(match_24h.group(2))
        end_h, end_m = int(match_24h.group(3)), int(match_24h.group(4))
        return start_h * 60 + start_m, end_h * 60 + end_m

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

        return start_h * 60, end_h * 60

    # Pattern: "after N PM" → N PM to midnight
    match_after = re.search(r"after\s+(\d{1,2})\s*(AM|PM|am|pm)?", text)
    if match_after:
        h = int(match_after.group(1))
        period = (match_after.group(2) or "").upper()
        if period == "PM" and h != 12:
            h += 12
        return h * 60, 24 * 60

    # Pattern: "before N AM/PM" → midnight to N
    match_before = re.search(r"before\s+(\d{1,2})\s*(AM|PM|am|pm)?", text)
    if match_before:
        h = int(match_before.group(1))
        period = (match_before.group(2) or "").upper()
        if period == "PM" and h != 12:
            h += 12
        return 0, h * 60

    return None


def _parse_hour_from_pattern(text: str) -> int | None:
    """Extract hour from a PEARL pattern like 'avoids tasks during hour 8'."""
    match = re.search(r"hour\s+(\d{1,2})", text)
    if match:
        return int(match.group(1))
    return None


def memories_to_constraints(user_id: str, memory_store) -> list[TimeSlot]:
    """Convert relevant memories into TimeSlot constraints for OR-Tools.

    Called during _run_plan_day_flow, BEFORE run_schedule.
    Queries both explicit constraints and PEARL-inferred behavioral patterns.
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
            constraints.append(TimeSlot(
                name=f"pearl_pattern_{pattern.get('id', '')}",
                start_min=hour * 60,
                end_min=(hour + 1) * 60,
                availability="minimal_work",
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
                source="pearl_inferred",
            ))

    return constraints
