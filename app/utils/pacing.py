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
    longest_task_minutes: int | None = None,
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

    # A cap below the longest single task can never place that task, and a cap
    # that isn't a whole number of task-atoms can't be composed either: cap=63
    # with 25-min tasks fits 2 per day (50) never 63, so 5x25 over 2 days
    # (needs one 75-min day) is INFEASIBLE at every horizon rung. Floor at one
    # task, then round UP to a whole number of longest-task atoms.
    if longest_task_minutes:
        atoms = math.ceil(max(cap, longest_task_minutes) / longest_task_minutes)
        cap = min(atoms * longest_task_minutes, max(longest_task_minutes, PACING_MAX_DEEP_WORK_PER_DAY))

    if daily_context:
        free_per_day = compute_free_minutes_per_day(daily_context, horizon_minutes)
        if free_per_day:
            min_free = min(free_per_day)
            if min_free < cap:
                cap = min_free
            # Re-apply the atom floor: the cap is GLOBAL (every day gets it) but
            # min_free is the single WORST day. One booked-solid day would
            # otherwise drag the cap under one task atom, and a cap below the
            # longest task places nothing on ANY day — INFEASIBLE at every rung
            # of the horizon ladder even when later days are wide open. Per-day
            # blocked time is already enforced exactly by the hard-block
            # NoOverlap; this cap is a pacing heuristic, never feasibility.
            if longest_task_minutes:
                cap = max(cap, longest_task_minutes)

    return max(1, cap)


def compute_free_minutes_per_day(
    daily_context: list["TimeSlot"],
    horizon_minutes: int,
) -> list[int]:
    """Estimate free (non-blocked) minutes per day from daily_context.

    Blocked windows are merged before they are summed: a 0-120 habit block nested
    inside a 0-600 constraint is 600 blocked minutes, not 720. Summing raw slots
    over-reports blocked time, which narrows the daily cap against free time that
    was never actually missing.

    Returns list of free minutes per day; empty if no blocks or single-day.
    """
    from app.core.or_tools.constraints import merge_time_windows
    from app.schemas.context import Availability

    if horizon_minutes <= MINUTES_PER_DAY:
        return []
    num_days = math.ceil(horizon_minutes / MINUTES_PER_DAY)
    blocked_per_day = [0] * num_days
    blocked_windows = merge_time_windows(
        (slot.start_min, slot.end_min)
        for slot in daily_context
        if slot.availability == Availability.BLOCKED
    )
    for start_min, end_min in blocked_windows:
        for d in range(num_days):
            day_start = d * MINUTES_PER_DAY
            day_end = (d + 1) * MINUTES_PER_DAY
            if day_end > horizon_minutes:
                break
            overlap_start = max(day_start, start_min)
            overlap_end = min(day_end, end_min)
            if overlap_end > overlap_start:
                blocked_per_day[d] += overlap_end - overlap_start
    return [MINUTES_PER_DAY - b for b in blocked_per_day]
