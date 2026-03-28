# app/schemas/draft.py
"""Schemas for draft review/accept/reject API."""

from datetime import datetime
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, Field


class DraftComponentResponse(BaseModel):
    """A single reviewable component in a draft."""

    component_type: str = Field(description="habits, tasks, schedule, action_items, materials")
    data: Any = Field(description="Component-specific data")
    status: str = Field(description="pending, accepted, rejected, modified")


class DraftResponse(BaseModel):
    """Full draft state returned to frontend."""

    draft_id: str
    user_id: str
    components: dict[str, DraftComponentResponse] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DraftAcceptRequest(BaseModel):
    """Accept specific components of a draft."""

    user_id: str = Field(..., description="User identifier")
    components: Optional[List[str]] = Field(
        default=None,
        description="Component keys to accept. None = accept all pending.",
    )


class DraftRejectRequest(BaseModel):
    """Reject specific components of a draft."""

    user_id: str = Field(..., description="User identifier")
    components: List[str] = Field(..., description="Component keys to reject")


class DraftModifyRequest(BaseModel):
    """Modify a specific component's data."""

    user_id: str = Field(..., description="User identifier")
    component: str = Field(..., description="Component key to modify")
    data: Any = Field(..., description="Updated component data")


# ── New schemas for draft negotiation ──────────────────────

class DraftTask(BaseModel):
    """A single task within a draft schedule."""

    task_id: str
    title: str
    start_min: int = Field(description="Start time in minutes from horizon_start")
    duration_minutes: int = Field(le=25, description="Max 25 min per task chunk")
    difficulty_weight: float = Field(ge=0, le=1)
    completion_criteria: str
    goal_id: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class DraftSchedule(BaseModel):
    """A proposed schedule awaiting user review."""

    draft_id: str
    user_id: str
    goal_id: str | None = None
    tasks: list[DraftTask]
    horizon_start: datetime
    status: Literal["pending", "accepted", "rejected", "modified", "expired"] = "pending"
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DraftTaskEdit(BaseModel):
    """User edit to a single task in a draft."""

    title: str | None = None
    duration_minutes: int | None = Field(default=None, le=60)
    difficulty_weight: float | None = Field(default=None, ge=0, le=1)
    start_min: int | None = None


class DraftAction(BaseModel):
    """User action on a draft."""

    action: Literal["accept_all", "reject", "edit_task", "rearrange", "chat_modify"]
    draft_id: str
    task_id: str | None = None
    edits: DraftTaskEdit | None = None
    reason: str | None = None
    modification_request: str | None = None
