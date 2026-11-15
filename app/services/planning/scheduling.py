"""Phase 2 — the OR-Tools solve: one deterministic schedule from an ExecutionGraph.

``run_schedule`` consumes the ExecutionGraph from the Socratic Task Chunker (Phase 1),
applies Temporal Motivation Theory (TMT) ranking, the adaptive daily cap, hard and soft
calendar blocks and the biological sleep fallback, and returns a mathematically valid
schedule. It is the one place these live: the planning module, the draft re-solve and
``POST /schedule/generate-schedule`` import and call it, never reimplement it. It
embodies the Anti-Guilt Architecture: INFEASIBLE is ``ScheduleInfeasibleError``, a scope
problem (422 ``schedule_infeasible`` in the envelope), never the user's failure.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Protocol
from zoneinfo import ZoneInfo

import app.core.config as config
from app.core.config import DEFAULT_HORIZON_MINUTES
from app.core.or_tools.solver import JarvisScheduler
from app.domain.errors import ScheduleInfeasibleError
from app.schemas.context import Availability, TimeSlot
from app.schemas.decomposition import ExecutionGraph
from app.schemas.schedule import GenerateScheduleResponse, ScheduledTask
from app.services.planning.local_time import (
    day_start_on,
    logical_day_start,
    resolve_not_before,
    user_zone,
)
from app.utils.deadline_parser import parse_deadline_to_date
from app.utils.pacing import compute_adaptive_daily_cap

# ---------------------------------------------------------------------------
# TMT (Temporal Motivation Theory) constants
# ---------------------------------------------------------------------------

IMPULSIVENESS = 1.5  # Constant; higher = more discounting of delayed rewards
DEFAULT_DELAY_HOURS = 24  # Used when deadline_hint is missing
DEFAULT_SELF_EFFICACY = 0.8  # Proxy for task-level confidence


class _ChunkWithDeadline(Protocol):
    deadline_hint: Optional[str]


def _delay_hours_for_chunk(
    chunk: _ChunkWithDeadline,
    horizon_start: datetime,
) -> float:
    """Compute delay_hours from chunk.deadline_hint for TMT.

    Past deadlines -> 1 (highest urgency). Invalid ISO -> DEFAULT_DELAY_HOURS.
    """
    parsed = parse_deadline_to_date(chunk.deadline_hint, horizon_start)
    if parsed is None:
        return DEFAULT_DELAY_HOURS
    if parsed.date() < horizon_start.date():
        return 1.0
    delta_days = (parsed.date() - horizon_start.date()).days
    hours = delta_days * 24
    return max(0.1, hours)  # floor 0.1 prevents zero


def _compute_tmt_priority(
    difficulty_weight: float,
    delay_hours: float = DEFAULT_DELAY_HOURS,
    success_rate: float = 0.5,
    self_efficacy_proxy: float = DEFAULT_SELF_EFFICACY,
    mastery_value: float | None = None,
) -> tuple[float, int]:
    """Canonical TMT (Steel & König 2006).

    Motivation = (Expectancy × Value) / (1 + Impulsiveness × Delay)

    Args:
        difficulty_weight: Cognitive load (0-1). Inverted to reward if no mastery_value.
        delay_hours: Hours until deadline. Floor 0.1.
        success_rate: From mastery tracker calculate_success_ratio(). Feeds Expectancy.
        self_efficacy_proxy: Task-level confidence (default 0.8).
        mastery_value: Explicit value (1-5) from goal_metadata if available.

    Returns:
        Tuple of (raw_tmt_score, priority_score integer).
    """
    expectancy = success_rate * self_efficacy_proxy
    value = mastery_value if mastery_value is not None else (1.0 - difficulty_weight + 0.1)
    delay = max(delay_hours, 0.1)

    motivation = (expectancy * value) / (1.0 + IMPULSIVENESS * delay)
    priority_score = max(1, int(motivation * 100))
    return (motivation, priority_score)


# ---------------------------------------------------------------------------
# The energy term (a learned focus profile; see energy_objective_terms)
# ---------------------------------------------------------------------------

# Tasks due within this many hours (today's, and overdue ones) keep their TMT place.
ENERGY_MIN_DELAY_HOURS = 24


def _focus_penalties(focus_by_hour: List[float]) -> List[int]:
    """Per local hour: round(10 × (1 − score)), 0 at full focus and 10 at none."""
    return [round(10 * (1 - score)) for score in focus_by_hour]


def _energy_weight(difficulty_weight: float, delay_hours: float) -> int:
    """How strongly a task leans toward high-focus hours: ENERGY_OBJECTIVE_WEIGHT × difficulty.

    0 for a task due today or overdue, so the energy term never competes with urgency.
    """
    if delay_hours < ENERGY_MIN_DELAY_HOURS:
        return 0
    return round(config.ENERGY_OBJECTIVE_WEIGHT * difficulty_weight)


def logical_horizon_start(
    zone: Optional[ZoneInfo] = None, *, plan_start: Optional[datetime] = None
) -> datetime:
    """DAY_START_HOUR on the plan's local date, in ``zone`` (the user's). Minute 0 = this instant.

    The plan's date is today's on this module's clock (the one tests freeze), or the
    date of ``plan_start`` for a caller that names its own (``/generate-schedule``); a
    naive ``plan_start`` is the user's wall time. Unlike ``current_horizon_start`` there
    is no late-night rule: before DAY_START_HOUR it is still today's 08:00. Offset-aware,
    so the client's ``horizon_start + minutes`` is the right instant in any browser.
    """
    return day_start_on(plan_start or datetime.now(timezone.utc), zone or user_zone())


def current_horizon_start(zone: Optional[ZoneInfo] = None) -> datetime:
    """Where a plan made now starts: DAY_START_HOUR local on the user's logical day.

    Read off this module's clock, which is the one tests freeze. Before
    DAY_START_HOUR the logical day is yesterday's (the late-night rule).
    """
    return logical_day_start(datetime.now(timezone.utc), zone or user_zone())


# The past is blocked out to the next whole 5 minutes, so the first task never
# starts a few seconds ago.
PAST_BLOCK_STEP_MINUTES = 5


def _minutes_already_past(horizon_start: datetime, now: datetime, horizon_minutes: int) -> int:
    """How much of the horizon lies before ``now``, rounded up to the next 5 minutes.

    0 when the horizon has not started yet (a caller's horizon in the future), never
    more than the horizon itself.
    """
    elapsed = (now - horizon_start).total_seconds() / 60
    if elapsed <= 0:
        return 0
    rounded = math.ceil(elapsed / PAST_BLOCK_STEP_MINUTES) * PAST_BLOCK_STEP_MINUTES
    return min(rounded, horizon_minutes)


def _not_before_minute(
    not_before: Optional[str], horizon_start: datetime, zone: ZoneInfo, now: datetime
) -> int:
    """The minute of the horizon a task's ``not_before`` holds it until; 0 for none.

    Read on the user's clock (``resolve_not_before``). One already past (before now,
    or before the horizon starts) is no constraint, and neither is one that cannot be
    read. One past the horizon's end is returned as it is: the solve is then
    INFEASIBLE, and the planner's horizon ladder widens the horizon until it fits.
    """
    moment = resolve_not_before(not_before, zone)
    if moment is None or moment <= now:
        return 0
    return max(0, math.ceil((moment - horizon_start).total_seconds() / 60))


MINUTES_PER_DAY = 1440
# Local time: minute 0 is 08:00 in the user's zone, so 960 is their midnight.
SLEEP_START = 960  # midnight (intra-day: 0=8 AM, 960=midnight)
SLEEP_END = 1440  # 8 AM


def _clamp_duration(
    duration: int,
    min_task: Optional[int],
    max_task: Optional[int],
) -> int:
    """Clamp duration to [min_task, max_task] when overrides provided."""
    if min_task is not None:
        duration = max(duration, min_task)
    if max_task is not None:
        duration = min(duration, max_task)
    return duration


def run_schedule(
    graph: ExecutionGraph,
    daily_context: List[TimeSlot],
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    horizon_start: Optional[datetime] = None,
    max_daily_deep_work_minutes: Optional[int] = None,
    min_daily_deep_work_minutes: Optional[int] = None,
    max_task_duration_minutes: Optional[int] = None,
    min_task_duration_minutes: Optional[int] = None,
    zone: Optional[ZoneInfo] = None,
    block_past: Optional[bool] = None,
    focus_by_hour: list[float] | None = None,
    success_rate: float = 0.5,
) -> GenerateScheduleResponse:
    """Reusable schedule generation from ExecutionGraph and daily context.
    Raises ScheduleInfeasibleError on INFEASIBLE.

    ``success_rate`` is TMT's Expectancy: the neutral prior 0.5 unless a caller knows
    better (no caller reads it from the database here).

    ``focus_by_hour`` is a learned focus profile (24 scores, index == local hour).
    With it, and with ``ENERGY_OBJECTIVE_ENABLED``, each task not due today leans
    toward the user's high-focus hours. Without it the objective is TMT alone.

    Minute 0 is 08:00 in the user's ``zone`` (default: ``user_zone()``). Without an
    explicit ``horizon_start`` it is 08:00 on the user's logical day — yesterday's
    before 08:00 — which is in the past for any plan made after 08:00: that stretch
    is blocked, so nothing is scheduled before now. An explicit ``horizon_start`` is
    the caller's to place and is left as given; its past is blocked only when the
    caller asks (``block_past=True``: the planner fixes one horizon per turn, then
    solves against now).

    A chunk's ``not_before`` is a hard lower bound on its start (see
    ``_not_before_minute``); one beyond this horizon makes it INFEASIBLE.
    """
    now = datetime.now(timezone.utc)
    resolved_horizon_start = horizon_start or current_horizon_start(zone)
    if block_past is None:
        block_past = horizon_start is None
    already_past = (
        _minutes_already_past(resolved_horizon_start, now, horizon_minutes) if block_past else 0
    )

    # Per-task duration clamp when overrides provided
    clamped_durations: Dict[str, int] = {}
    for chunk in graph.decomposition:
        d = _clamp_duration(
            chunk.duration_minutes,
            min_task_duration_minutes,
            max_task_duration_minutes,
        )
        clamped_durations[chunk.task_id] = d
    total_task_minutes = sum(clamped_durations.values())

    # Each task's earliest start (its not_before), in minutes from the horizon's start.
    clock_zone = zone or user_zone()
    earliest_starts = {
        chunk.task_id: _not_before_minute(
            chunk.not_before, resolved_horizon_start, clock_zone, now
        )
        for chunk in graph.decomposition
    }

    # Dynamic Biological Fallback: inject default sleep block for cold-start users
    has_sleep_habit = any(
        "sleep" in slot.name.lower() or "night" in slot.name.lower()
        for slot in daily_context
    )
    if not has_sleep_habit:
        max_days = horizon_minutes // MINUTES_PER_DAY + 1
        for d in range(max_days):
            start = d * MINUTES_PER_DAY + SLEEP_START
            end = d * MINUTES_PER_DAY + SLEEP_END
            if end > horizon_minutes:
                break
            daily_context.append(
                TimeSlot(
                    name=f"Default Sleep / Recharge_d{d}",
                    start_min=start,
                    end_min=end,
                    availability=Availability.BLOCKED,
                    recurring=True,
                )
            )

    intrinsic_load = graph.cognitive_load_estimate.get("intrinsic_load", 0.5)
    slack_ratio = horizon_minutes / max(1, total_task_minutes)
    cap = compute_adaptive_daily_cap(
        horizon_minutes=horizon_minutes,
        total_task_minutes=total_task_minutes,
        intrinsic_load=intrinsic_load,
        user_override=max_daily_deep_work_minutes,
        min_daily_override=min_daily_deep_work_minutes,
        daily_context=daily_context if horizon_minutes > MINUTES_PER_DAY else None,
        longest_task_minutes=max(
            (clamped_durations.get(t.task_id, t.duration_minutes) for t in graph.decomposition),
            default=None,
        ),
        held_work=[
            (earliest, clamped_durations[task_id])
            for task_id, earliest in earliest_starts.items()
            if earliest > 0
        ],
    )
    scheduler = JarvisScheduler(
        horizon_minutes=horizon_minutes,
        max_daily_deep_work_minutes=cap if horizon_minutes > MINUTES_PER_DAY else None,
        slack_ratio=slack_ratio,
    )

    # The part of the horizon already behind us. A hard block on the solver only,
    # not a daily_context slot: in the context it would count as a short day in
    # the adaptive cap and shrink every other day's budget along with it.
    if already_past:
        scheduler.add_hard_block(0, already_past, "past")

    # Minute 0 is DAY_START_HOUR local, so the penalties are indexed by local hour.
    energy_on = focus_by_hour is not None and config.ENERGY_OBJECTIVE_ENABLED
    if energy_on:
        scheduler.set_focus_penalties(_focus_penalties(focus_by_hour), config.DAY_START_HOUR)

    # Enforce dynamic calendar blocks from daily_context
    for slot in daily_context:
        if slot.availability == Availability.BLOCKED:
            scheduler.add_hard_block(slot.start_min, slot.end_min, slot.name)
        elif slot.availability == Availability.MINIMAL_WORK:
            scheduler.add_soft_block(
                slot.start_min,
                slot.end_min,
                slot.name,
                max_task_duration=slot.max_task_duration or 15,
                max_difficulty=slot.max_difficulty or 0.4,
            )
        # FULL_FOCUS: no block added


    # TMT scores and task mapping (per-chunk delay from deadline_hint)
    tmt_scores: dict[str, float] = {}
    for chunk in graph.decomposition:
        duration = clamped_durations[chunk.task_id]
        delay_h = _delay_hours_for_chunk(chunk, resolved_horizon_start)
        _mastery_val = getattr(graph.goal_metadata, "mastery_level_target", None)
        tmt_display, priority_score = _compute_tmt_priority(
            difficulty_weight=chunk.difficulty_weight,
            delay_hours=delay_h,
            success_rate=success_rate,
            mastery_value=float(_mastery_val) if _mastery_val else None,
        )
        tmt_scores[chunk.task_id] = tmt_display
        scheduler.add_task(
            chunk.task_id,
            duration,
            priority_score,
            chunk.dependencies,
            difficulty_weight=chunk.difficulty_weight,
            energy_weight=_energy_weight(chunk.difficulty_weight, delay_h) if energy_on else 0,
            earliest_start=earliest_starts[chunk.task_id],
        )

    result, status_or_empty = scheduler.solve()

    if result == "INFEASIBLE":
        from app.core.jarvis_logger import log_step

        log_step(
            "SCHEDULER_INFEASIBLE",
            "OR-Tools returned INFEASIBLE",
            {"horizon_min": horizon_minutes, "num_blocks": len(daily_context)},
        )
        # A scope problem, never the user's failure: the error envelope answers 422
        # (schedule_infeasible), and planning's horizon ladder catches it first.
        raise ScheduleInfeasibleError(
            "Schedule infeasible; consider reducing scope or extending deadline."
        )

    # Build response with schedule and TMT scores
    title_map = {c.task_id: c.title for c in graph.decomposition}
    schedule: dict[str, ScheduledTask] = {}
    for task_id, slot in result.items():
        schedule[task_id] = ScheduledTask(
            start_min=slot["start"],
            end_min=slot["end"],
            tmt_score=tmt_scores[task_id],
            title=title_map.get(task_id),
        )

    status: Literal["FEASIBLE", "OPTIMAL"] = (
        status_or_empty if status_or_empty in ("FEASIBLE", "OPTIMAL") else "FEASIBLE"
    )

    return GenerateScheduleResponse(
        status=status,
        schedule=schedule,
        goal_metadata=graph.goal_metadata,
        horizon_start=resolved_horizon_start,
    )
