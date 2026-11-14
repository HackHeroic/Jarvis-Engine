"""The schedule contract: what a solve returns (``run_schedule``, ``/generate-schedule``).

Here, below the API layer, so the planning module, the draft re-solve and the endpoint
all read one definition.
"""

from datetime import datetime
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.decomposition import GoalMetadata


class ScheduledTask(BaseModel):
    """A task with computed start, end, and TMT score."""

    start_min: int = Field(..., description="Start time in minutes from horizon zero.")
    end_min: int = Field(..., description="End time in minutes from horizon zero.")
    tmt_score: float = Field(..., description="Temporal Motivation Theory score.")
    title: Optional[str] = Field(default=None, description="Human-readable task title from decomposition.")


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
        description=(
            "ISO-8601 instant, with its UTC offset, when minute 0 of the horizon occurs: "
            "08:00 in the user's zone. Client: wall_time = horizon_start + "
            "timedelta(minutes=start_min), shown in the viewer's local time."
        ),
    )
