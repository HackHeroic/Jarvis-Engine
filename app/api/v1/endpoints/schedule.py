"""Phase 2 — OR-Tools Solver execution for deterministic scheduling.

Consumes ExecutionGraph from the Socratic Task Chunker (Phase 1), applies
Temporal Motivation Theory (TMT) ranking, and returns mathematically valid
schedules. Enforces biological constraints (sleep 23:00–07:00) and embodies
the Anti-Guilt Architecture: INFEASIBLE triggers Socratic recalibration rather
than user guilt.
"""

from __future__ import annotations

from typing import Dict, Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata
from app.core.or_tools.solver import JarvisScheduler

# ---------------------------------------------------------------------------
# TMT (Temporal Motivation Theory) constants
# ---------------------------------------------------------------------------

EXPECTANCY = 1.0  # Default: user expects to complete
IMPULSIVENESS = 1.5  # Constant; higher = more discounting of delayed rewards
DEFAULT_DELAY_HOURS = 24  # Used when deadline_hint is missing

# Sleep block: 23:00 (11 PM) to 07:00 next day
SLEEP_START_MIN = 23 * 60  # 1380
SLEEP_END_MIN = 24 * 60 + 7 * 60  # 1860


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


router = APIRouter()


@router.post(
    "/generate-schedule",
    response_model=GenerateScheduleResponse,
    summary="Generate deterministic schedule",
    description=(
        "Accepts an ExecutionGraph from the Socratic Task Chunker and "
        "returns a mathematically valid schedule using OR-Tools CP-SAT. "
        "Applies TMT prioritization so high-value tasks start earlier. "
        "Sleep block (23:00–07:00) is enforced. INFEASIBLE triggers 422 "
        "for Socratic recalibration."
    ),
)
def generate_schedule(
    graph: ExecutionGraph = Body(
        ...,
        description="ExecutionGraph from /reasoning/decompose-goal.",
    ),
) -> GenerateScheduleResponse:
    """Generate a deterministic schedule from an ExecutionGraph."""
    scheduler = JarvisScheduler(horizon_minutes=2880)

    # Hard block: sleep 11 PM to 7 AM
    scheduler.add_hard_block(SLEEP_START_MIN, SLEEP_END_MIN, "sleep")

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
    )
