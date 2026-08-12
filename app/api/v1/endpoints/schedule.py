"""Phase 2 — OR-Tools Solver execution for deterministic scheduling.

Consumes ExecutionGraph from the Socratic Task Chunker (Phase 1), applies
Temporal Motivation Theory (TMT) ranking, and returns mathematically valid
schedules. Supports dynamic daily context (hard/soft blocks) from timetables
and embodies the Anti-Guilt Architecture: INFEASIBLE triggers Socratic
recalibration rather than user guilt.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Dict, List, Literal, Optional, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata
from app.core.config import DAY_START_HOUR, DEFAULT_HORIZON_MINUTES
from app.utils.deadline_parser import parse_deadline_to_date
from app.utils.pacing import compute_adaptive_daily_cap
from app.core.or_tools.solver import JarvisScheduler
from app.schemas.context import Availability, TimeSlot

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
        success_rate: From mastery tracker _calculate_sr(). Feeds Expectancy.
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
# Response schemas
# ---------------------------------------------------------------------------


class ScheduledTask(BaseModel):
    """A task with computed start, end, and TMT score."""

    start_min: int = Field(..., description="Start time in minutes from horizon zero.")
    end_min: int = Field(..., description="End time in minutes from horizon zero.")
    tmt_score: float = Field(..., description="Temporal Motivation Theory score.")
    title: Optional[str] = Field(default=None, description="Human-readable task title from decomposition.")


def _compute_horizon_start(plan_start: Optional[datetime] = None) -> datetime:
    """Compute horizon_start = 8 AM of plan date. Minute 0 = this datetime."""
    ref = plan_start or datetime.now(timezone.utc)
    return datetime.combine(ref.date(), time(DAY_START_HOUR, 0), tzinfo=timezone.utc)


class ScheduleRequest(BaseModel):
    """Request body for POST /generate-schedule."""

    graph: ExecutionGraph = Field(
        ...,
        description="ExecutionGraph from /reasoning/decompose-goal.",
    )
    daily_context: List[TimeSlot] = Field(
        default_factory=list,
        description="Dynamic calendar blocks (hard/soft) from timetable ingestion.",
    )
    horizon_minutes: int = Field(
        default=DEFAULT_HORIZON_MINUTES,
        description="Planning window in minutes (default 48h).",
    )
    plan_start: Optional[datetime] = Field(
        default=None,
        description="Reference datetime for horizon; default = now. Used to compute horizon_start.",
    )
    max_daily_deep_work_minutes: Optional[int] = Field(
        default=None,
        ge=30,
        le=600,
        description="Cap on scheduled work per day; None uses adaptive formula.",
    )
    min_daily_deep_work_minutes: Optional[int] = Field(
        default=None,
        ge=15,
        le=240,
        description="Avoid days with less than X min; constrains spread.",
    )
    max_task_duration_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=25,
        description="Clamp per-chunk duration; None uses LLM values.",
    )
    min_task_duration_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=25,
        description="Clamp per-chunk duration floor; None uses LLM values.",
    )


class GenerateScheduleResponse(BaseModel):
    """Response from POST /generate-schedule."""

    status: Literal["FEASIBLE", "OPTIMAL"] = Field(
        ...,
        description="Solver status (FEASIBLE or OPTIMAL).",
    )
    schedule: Dict[str, ScheduledTask] = Field(
        ...,
        description="Task IDs mapped to scheduled slots and TMT scores.",
    )
    goal_metadata: GoalMetadata = Field(
        ...,
        description="Pass-through from ExecutionGraph.",
    )
    horizon_start: datetime = Field(
        ...,
        description="ISO-8601 datetime when minute 0 of the horizon occurs. Client: wall_time = horizon_start + timedelta(minutes=start_min).",
    )


router = APIRouter()


MINUTES_PER_DAY = 1440
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
    user_id: Optional[str] = None,
    db_client: object = None,
) -> GenerateScheduleResponse:
    """Reusable schedule generation from ExecutionGraph and daily context.
    Raises HTTPException on INFEASIBLE."""
    resolved_horizon_start = horizon_start or _compute_horizon_start()

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
    )
    scheduler = JarvisScheduler(
        horizon_minutes=horizon_minutes,
        max_daily_deep_work_minutes=cap if horizon_minutes > MINUTES_PER_DAY else None,
        slack_ratio=slack_ratio,
    )

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

    # Fetch success rate for TMT Expectancy (reuse existing db_client)
    _sr = 0.5  # neutral prior
    if user_id and db_client and hasattr(db_client, "supabase"):
        try:
            from app.services.analytical.mastery_tracker import _calculate_sr

            goal_id = getattr(graph.goal_metadata, "goal_id", None)
            _sr = _calculate_sr(user_id, goal_id, db_client.supabase)
        except Exception:
            pass

    # TMT scores and task mapping (per-chunk delay from deadline_hint)
    tmt_scores: dict[str, float] = {}
    for chunk in graph.decomposition:
        duration = clamped_durations[chunk.task_id]
        delay_h = _delay_hours_for_chunk(chunk, resolved_horizon_start)
        _mastery_val = getattr(graph.goal_metadata, "mastery_level_target", None)
        tmt_display, priority_score = _compute_tmt_priority(
            difficulty_weight=chunk.difficulty_weight,
            delay_hours=delay_h,
            success_rate=_sr,
            mastery_value=float(_mastery_val) if _mastery_val else None,
        )
        tmt_scores[chunk.task_id] = tmt_display
        scheduler.add_task(
            chunk.task_id,
            duration,
            priority_score,
            chunk.dependencies,
            difficulty_weight=chunk.difficulty_weight,
        )

    result, status_or_empty = scheduler.solve()

    if result == "INFEASIBLE":
        from app.core.jarvis_logger import log_step

        log_step(
            "SCHEDULER_INFEASIBLE",
            "OR-Tools returned INFEASIBLE",
            {"horizon_min": horizon_minutes, "num_blocks": len(daily_context)},
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "Schedule infeasible; consider reducing scope or extending deadline."
            ),
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


@router.post(
    "/generate-schedule",
    response_model=GenerateScheduleResponse,
    summary="Generate deterministic schedule",
    description=(
        "Accepts an ExecutionGraph and optional daily_context (hard/soft blocks). "
        "Returns a mathematically valid schedule using OR-Tools CP-SAT. "
        "Applies TMT prioritization so high-value tasks start earlier. "
        "INFEASIBLE triggers 422 for Socratic recalibration."
    ),
)
def generate_schedule(request: ScheduleRequest) -> GenerateScheduleResponse:
    """Generate a deterministic schedule from an ExecutionGraph and daily context."""
    horizon_start = _compute_horizon_start(request.plan_start)
    return run_schedule(
        request.graph,
        request.daily_context,
        horizon_minutes=request.horizon_minutes,
        horizon_start=horizon_start,
        max_daily_deep_work_minutes=request.max_daily_deep_work_minutes,
        min_daily_deep_work_minutes=request.min_daily_deep_work_minutes,
        max_task_duration_minutes=request.max_task_duration_minutes,
        min_task_duration_minutes=request.min_task_duration_minutes,
    )
