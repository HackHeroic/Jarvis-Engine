"""Phase 2 — OR-Tools Solver execution for deterministic scheduling.

Consumes ExecutionGraph from the Socratic Task Chunker (Phase 1), applies
Temporal Motivation Theory (TMT) ranking, and returns mathematically valid
schedules. Supports dynamic daily context (hard/soft blocks) from timetables
and embodies the Anti-Guilt Architecture: INFEASIBLE triggers Socratic
recalibration rather than user guilt.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata
from app.core.config import DAY_START_HOUR, DEFAULT_HORIZON_MINUTES
from app.core.or_tools.solver import JarvisScheduler
from app.schemas.context import Availability, TimeSlot

# ---------------------------------------------------------------------------
# TMT (Temporal Motivation Theory) constants
# ---------------------------------------------------------------------------

EXPECTANCY = 1.0  # Default: user expects to complete
IMPULSIVENESS = 1.5  # Constant; higher = more discounting of delayed rewards
DEFAULT_DELAY_HOURS = 24  # Used when deadline_hint is missing


def _compute_tmt_priority(
    difficulty_weight: float,
    delay_hours: float = DEFAULT_DELAY_HOURS,
) -> tuple[float, int]:
    """Compute TMT motivation score and integer priority.

    Formula: Motivation = (Expectancy * Value) / (Impulsiveness * Delay)
    Value = difficulty_weight (0–1). Scale to integer: max(1, int(motivation * 100)).

    Args:
        difficulty_weight: Task value from TaskChunk (0.0–1.0).
        delay_hours: Hours until deadline; default 24 if missing.

    Returns:
        Tuple of (raw_tmt_score, priority_score integer).
    """
    value = difficulty_weight
    motivation = (EXPECTANCY * value) / (IMPULSIVENESS * delay_hours)
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


def run_schedule(
    graph: ExecutionGraph,
    daily_context: List[TimeSlot],
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    horizon_start: Optional[datetime] = None,
) -> GenerateScheduleResponse:
    """Reusable schedule generation from ExecutionGraph and daily context.
    Raises HTTPException on INFEASIBLE."""
    resolved_horizon_start = horizon_start or _compute_horizon_start()

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

    scheduler = JarvisScheduler(horizon_minutes=horizon_minutes)

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

    # TMT scores and task mapping
    tmt_scores: dict[str, float] = {}
    for chunk in graph.decomposition:
        tmt_raw, priority_score = _compute_tmt_priority(
            chunk.difficulty_weight,
            DEFAULT_DELAY_HOURS,
        )
        tmt_scores[chunk.task_id] = tmt_raw
        scheduler.add_task(
            chunk.task_id,
            chunk.duration_minutes,
            priority_score,
            chunk.dependencies,
            difficulty_weight=chunk.difficulty_weight,
        )

    result, status_or_empty = scheduler.solve()

    if result == "INFEASIBLE":
        raise HTTPException(
            status_code=422,
            detail=(
                "Schedule infeasible; consider reducing scope or extending deadline."
            ),
        )

    # Build response with schedule and TMT scores
    schedule: dict[str, ScheduledTask] = {}
    for task_id, slot in result.items():
        schedule[task_id] = ScheduledTask(
            start_min=slot["start"],
            end_min=slot["end"],
            tmt_score=round(tmt_scores[task_id], 2),
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
    )
