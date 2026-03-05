"""Horizon expansion: replicate semantic slots across multi-day horizon."""

from datetime import datetime, timedelta
from typing import List

from app.schemas.context import (
    SemanticTimeSlot,
    TimeSlot,
)

MINUTES_PER_DAY = 1440
WEEKDAY_MON_FRI = (0, 1, 2, 3, 4)  # Monday=0 in Python weekday()
WEEKDAY_SAT_SUN = (5, 6)


def _parse_iso_date(s: str) -> datetime | None:
    """Parse ISO-8601 date string; return None if invalid or placeholder."""
    if not s or not isinstance(s, str) or len(s) < 10:
        return None
    s = s.strip()[:10]
    try:
        year, month, day = int(s[:4]), int(s[5:7]), int(s[8:10])
        return datetime(year, month, day)
    except (ValueError, TypeError, IndexError):
        return None


def _slot_in_validity_window(
    slot: SemanticTimeSlot,
    plan_start: datetime,
    horizon_minutes: int,
) -> bool:
    """Return False if slot should be filtered out (expired or not yet valid)."""
    horizon_end = plan_start + timedelta(minutes=horizon_minutes)
    plan_date = plan_start.date()
    horizon_end_date = horizon_end.date()
    if slot.valid_until:
        parsed = _parse_iso_date(slot.valid_until)
        if parsed and parsed.date() < plan_date:
            return False
    if slot.valid_from:
        parsed = _parse_iso_date(slot.valid_from)
        if parsed and parsed.date() > horizon_end_date:
            return False
    return True


def expand_semantic_slots_to_time_slots(
    semantic_slots: List[SemanticTimeSlot],
    horizon_minutes: int,
    plan_start: datetime,
) -> List[TimeSlot]:
    """
    Replicate each semantic slot across the horizon based on recurrence.
    Returns concrete TimeSlots with start_min/end_min in horizon space (0..horizon_minutes).

    Args:
        semantic_slots: LLM-extracted slots with recurrence, weekday, validity.
        horizon_minutes: Planning window in minutes (e.g. 2880 for 48h).
        plan_start: Reference date for day-of-week and calendar logic.

    Returns:
        List of TimeSlots with concrete start_min/end_min in horizon space.
    """
    result: List[TimeSlot] = []

    for slot in semantic_slots:
        if not _slot_in_validity_window(slot, plan_start, horizon_minutes):
            continue
        start_min = max(0, min(slot.start_min, 1440))
        end_min = max(0, min(slot.end_min, 1440))
        if start_min >= end_min:
            continue

        recurrence = slot.recurrence or "daily"
        weekday = slot.weekday

        if recurrence == "once":
            if end_min <= horizon_minutes:
                result.append(
                    TimeSlot(
                        name=slot.name,
                        start_min=start_min,
                        end_min=end_min,
                        availability=slot.availability,
                        recurring=False,
                        max_task_duration=slot.max_task_duration,
                        max_difficulty=slot.max_difficulty,
                    )
                )
            continue

        max_days = horizon_minutes // MINUTES_PER_DAY + 1
        for d in range(max_days):
            base_start = d * MINUTES_PER_DAY + start_min
            base_end = d * MINUTES_PER_DAY + end_min
            if base_end > horizon_minutes:
                break

            target_date = plan_start + timedelta(days=d)
            target_weekday = target_date.weekday()

            if recurrence == "daily":
                include = True
            elif recurrence == "weekdays":
                include = target_weekday in WEEKDAY_MON_FRI
            elif recurrence == "weekends":
                include = target_weekday in WEEKDAY_SAT_SUN
            elif recurrence == "weekly":
                if weekday is not None:
                    include = target_weekday == weekday
                else:
                    include = d % 7 == 0
            elif recurrence == "monthly":
                if weekday is not None:
                    include = target_weekday == weekday and target_date.day <= 7
                else:
                    include = target_date.day == plan_start.day
            elif recurrence == "yearly":
                include = (
                    target_date.month == plan_start.month
                    and target_date.day == plan_start.day
                )
            else:
                include = True

            if include:
                result.append(
                    TimeSlot(
                        name=f"{slot.name}_d{d}",
                        start_min=base_start,
                        end_min=base_end,
                        availability=slot.availability,
                        recurring=True,
                        max_task_duration=slot.max_task_duration,
                        max_difficulty=slot.max_difficulty,
                    )
                )

    return result
