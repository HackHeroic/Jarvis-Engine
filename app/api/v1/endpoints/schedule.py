"""``POST /api/v1/schedule/generate-schedule``: one deterministic schedule, on request.

Routing and parsing only. The solve is ``run_schedule``
(``app/services/planning/scheduling.py``); an INFEASIBLE solve is its
``ScheduleInfeasibleError``, answered as 422 ``schedule_infeasible`` in the envelope.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import DEFAULT_HORIZON_MINUTES
from app.schemas.context import TimeSlot
from app.schemas.decomposition import ExecutionGraph
from app.schemas.schedule import GenerateScheduleResponse
from app.services.planning.scheduling import logical_horizon_start, run_schedule

router = APIRouter()


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
    # No plan_start: run_schedule derives the horizon from now, and blocks the
    # part of it that has already gone by.
    horizon_start = (
        logical_horizon_start(plan_start=request.plan_start)
        if request.plan_start is not None
        else None
    )
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
