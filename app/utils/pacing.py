"""Adaptive pacing: formula-driven daily cap and slack ratio.

Research-grounded: 3-4h optimal deep work/day; distributed practice favors spread
when we have slack. No hardcoded horizon tiers; behavior driven by slack_ratio.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from app.core.config import (
    PACING_COGNITIVE_LOAD_HIGH,
    PACING_COGNITIVE_LOAD_MED,
    PACING_MAX_DEEP_WORK_PER_DAY,
    PACING_MODERATE_MIN_PER_DAY,
    PACING_STANDARD_MIN_PER_DAY,
    PACING_SUSTAINABLE_MIN_PER_DAY,
)

MINUTES_PER_DAY = 1440

if TYPE_CHECKING:
    from app.schemas.context import TimeSlot


def compute_adaptive_daily_cap(
    horizon_minutes: int,
    total_task_minutes: int,
    intrinsic_load: float = 0.5,
    user_override: int | None = None,
    min_daily_override: int | None = None,
    daily_context: list["TimeSlot"] | None = None,
) -> int:
    """Compute daily deep-work cap from horizon, work, and cognitive load.

    Research: 3-4h optimal; distributed practice favors spread when we have slack.
    User override takes precedence. min_daily_override limits over-spreading.
    """
    if user_override is not None:
        return user_override

    if horizon_minutes <= MINUTES_PER_DAY:
        return horizon_minutes

    horizon_days = horizon_minutes / MINUTES_PER_DAY
    slack_ratio = horizon_minutes / max(1, total_task_minutes)

    if slack_ratio >= 10:
        target_min_per_day = PACING_SUSTAINABLE_MIN_PER_DAY
    elif slack_ratio >= 5:
        target_min_per_day = PACING_MODERATE_MIN_PER_DAY
    elif slack_ratio >= 3:
        target_min_per_day = PACING_STANDARD_MIN_PER_DAY
    else:
        target_min_per_day = PACING_MAX_DEEP_WORK_PER_DAY

    target_days = max(2, min(horizon_days, math.ceil(total_task_minutes / target_min_per_day)))

    if min_daily_override and min_daily_override > 0:
        max_days_from_min = max(1, int(total_task_minutes / min_daily_override))
        target_days = min(target_days, max_days_from_min)

    cap = math.ceil(total_task_minutes / target_days)

    if intrinsic_load >= 0.8:
        cap = int(cap * PACING_COGNITIVE_LOAD_HIGH)
    elif intrinsic_load >= 0.7:
        cap = int(cap * PACING_COGNITIVE_LOAD_MED)

    cap = min(cap, PACING_MAX_DEEP_WORK_PER_DAY)

    if daily_context:
        free_per_day = compute_free_minutes_per_day(daily_context, horizon_minutes)
        if free_per_day:
            min_free = min(free_per_day)
            if min_free < cap:
                cap = min_free

    return max(1, cap)


def compute_free_minutes_per_day(
    daily_context: list["TimeSlot"],
    horizon_minutes: int,
) -> list[int]:
    """Estimate free (non-blocked) minutes per day from daily_context.

    Returns list of free minutes per day; empty if no blocks or single-day.
    """
    from app.schemas.context import Availability

    if horizon_minutes <= MINUTES_PER_DAY:
        return []
    num_days = math.ceil(horizon_minutes / MINUTES_PER_DAY)
    blocked_per_day = [0] * num_days
    for slot in daily_context:
        if slot.availability != Availability.BLOCKED:
            continue
        for d in range(num_days):
            day_start = d * MINUTES_PER_DAY
            day_end = (d + 1) * MINUTES_PER_DAY
            if day_end > horizon_minutes:
                break
            overlap_start = max(day_start, slot.start_min)
            overlap_end = min(day_end, slot.end_min)
            if overlap_end > overlap_start:
                blocked_per_day[d] += overlap_end - overlap_start
    return [MINUTES_PER_DAY - b for b in blocked_per_day]
