# app/schemas/draft.py
"""Schemas for draft review/accept/reject API."""

from datetime import datetime, timezone
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, Field


class DraftStateResponse(BaseModel):
    """A ``draft_schedules`` row as the review UI sees it.

    Mirrors the table (migration 20260329000002) rather than the old
    component-bag shape: a draft is a task list plus a horizon, and it is
    accepted or rejected whole.
    """

    draft_id: str = Field(description="Primary key of the draft (draft_schedules.id)")
    user_id: str
    goal_id: Optional[str] = None
    tasks: List[dict] = Field(default_factory=list, description="TaskChunk dicts under review")
    horizon_start: Optional[str] = Field(
        default=None, description="ISO-8601 minute-0 of the scheduling horizon"
    )
    status: str = Field(description="pending, accepted, rejected, modified, expired")
    rejection_reason: Optional[str] = None
    created_at: Optional[str] = None


class DraftAcceptRequest(BaseModel):
    """Accept a draft."""

    user_id: str = Field(..., description="User identifier")
    components: Optional[List[str]] = Field(
        default=None,
        description=(
            "Ignored — kept so existing frontend callers keep validating. A draft "
            "is accepted whole; there are no per-component statuses any more."
        ),
    )


class DraftRejectRequest(BaseModel):
    """Reject a draft."""

    user_id: str = Field(..., description="User identifier")
    components: Optional[List[str]] = Field(
        default=None, description="Ignored — a draft is rejected whole (see DraftAcceptRequest)."
    )
    reason: Optional[str] = Field(default=None, description="Why the user rejected the draft")


class DraftModifyRequest(BaseModel):
    """Replace a draft's task list."""

    user_id: str = Field(..., description="User identifier")
    component: str = Field(
        ..., description="Only 'tasks' is modifiable — the component model is gone"
    )
    data: Any = Field(..., description="Replacement task list")


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DraftTaskEdit(BaseModel):
    """User edit to a single task in a draft."""

    title: str | None = None
    duration_minutes: int | None = Field(default=None, le=25, description="Max 25 min per task chunk")
    difficulty_weight: float | None = Field(default=None, ge=0, le=1)
    start_min: int | None = None


class DraftRearrangeRequest(BaseModel):
    """Rearrange task order in a draft."""

    user_id: str = Field(..., description="User identifier")
    task_order: List[str] = Field(..., description="Ordered list of task IDs defining new order")


class DraftChatRequest(BaseModel):
    """Modify a draft via natural language."""

    user_id: str = Field(..., description="User identifier")
    message: str = Field(..., description="Natural language modification request")


class DraftAction(BaseModel):
    """User action on a draft."""

    action: Literal["accept_all", "reject", "edit_task", "rearrange", "chat_modify"]
    draft_id: str
    task_id: str | None = None
    edits: DraftTaskEdit | None = None
    reason: str | None = None
    modification_request: str | None = None
