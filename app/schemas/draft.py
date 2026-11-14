# app/schemas/draft.py
"""Schemas for draft review/accept/reject API."""

from datetime import datetime, timezone
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, Field

from app.domain.entities import DraftStatus


class DraftSlot(BaseModel):
    """One task's solved slot, in minutes from horizon_start and on the user's clock."""

    task_id: str = Field(description="The task the slot belongs to.")
    title: Optional[str] = Field(default=None, description="The task's title.")
    start_min: int = Field(description="Start, in minutes from horizon_start.")
    end_min: int = Field(description="End, in minutes from horizon_start.")
    start: datetime = Field(
        description="Start as an ISO-8601 instant with the offset of the user's zone."
    )
    end: datetime = Field(
        description="End as an ISO-8601 instant with the offset of the user's zone."
    )
    duration_minutes: int = Field(description="Length of the slot in minutes.")


class DraftScheduleView(BaseModel):
    """A draft's solved slots, in the shape the chat ``complete`` payload's schedule has
    (``horizon_start`` and task_id -> {start_min, end_min, title}), plus wall-clock times."""

    horizon_start: str = Field(
        description="ISO-8601 instant of minute 0 (08:00 on the user's clock), as stored."
    )
    schedule: dict[str, DraftSlot] = Field(
        description="task_id -> its slot, for every task the solver placed."
    )


class DraftStateResponse(BaseModel):
    """A ``draft_schedules`` row as the review UI sees it.

    Mirrors the table (migration 20260329000002) rather than the old
    component-bag shape: a draft is a task list plus a horizon, and it is
    accepted or rejected whole.
    """

    draft_id: str = Field(
        description="The draft's id (draft_schedules.id): `draft_id` wherever a draft is "
        "referred to, as in the chat complete frame."
    )
    user_id: str
    goal_id: Optional[str] = None
    tasks: List[dict] = Field(default_factory=list, description="TaskChunk dicts under review")
    horizon_start: Optional[str] = Field(
        default=None, description="ISO-8601 minute-0 of the scheduling horizon"
    )
    status: DraftStatus = Field(
        description=(
            "Where the draft is: pending or modified (open, under review); accepted,"
            " rejected, expired or superseded (closed)."
        )
    )
    rejection_reason: Optional[str] = None
    created_at: Optional[str] = None
    schedule: Optional[DraftScheduleView] = Field(
        default=None,
        description="The solved slots with their times on the user's clock; None for a draft "
        "stored without them.",
    )
    version: str = Field(
        description="What this read saw, as one opaque value: send it back as the accept's "
        "expected_version, and a draft changed since is not accepted (409 stale_version)."
    )


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
    expected_version: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "The draft's version from the read the user acted on (GET /drafts/{id}). When"
            " given and the draft has changed since, nothing is written: 409 stale_version"
            " with the latest draft. Omitted: no check."
        ),
    )


class DraftAcceptResponse(BaseModel):
    """What accepting a draft did."""

    status: Literal["accepted", "empty"] = Field(
        description="accepted: its tasks are on the plan; empty: it held nothing to schedule."
    )
    draft_id: str = Field(description="The draft accepted.")
    task_count: int = Field(ge=0, description="Tasks written to the user's plan.")
    moved: bool = Field(
        default=False,
        description=(
            "True when some of the draft's times had already gone by: its tasks were"
            " re-solved from now before it was put in place."
        ),
    )
    message: Optional[str] = Field(
        default=None, description="What to tell the user when times were moved; null otherwise."
    )


class DraftRejectResponse(BaseModel):
    """What rejecting a draft did."""

    status: Literal["rejected"] = Field(description="The draft is rejected; nothing was written.")
    draft_id: str = Field(description="The draft rejected.")


class DraftEditResponse(BaseModel):
    """What editing a draft's tasks did: the whole list (modify) or one task (edit)."""

    status: Literal["modified"] = Field(description="The draft holds the edit and is modified.")
    draft_id: str = Field(description="The draft edited.")
    component: Optional[Literal["tasks"]] = Field(
        default=None, description="'tasks' when the whole task list was replaced (modify)."
    )
    task_id: Optional[str] = Field(default=None, description="The task edited (edit one task).")
    updated_draft: Optional[dict] = Field(
        default=None, description="The draft as stored after the edit (edit one task)."
    )


class DraftRearrangeResponse(BaseModel):
    """What reordering a draft's tasks did."""

    status: Literal["rearranged"] = Field(description="The draft holds the new order.")
    draft_id: str = Field(description="The draft rearranged.")
    task_count: int = Field(ge=0, description="Tasks in the draft, in their new order.")
    needs_resolve: bool = Field(
        description="True: the stored times are the solver's and no longer match the order."
    )


class DraftDiscardResponse(BaseModel):
    """What discarding a draft did: it is closed (expired), and its row is kept."""

    status: Literal["discarded"] = Field(
        description="The draft is set aside (expired); nothing was written or deleted."
    )
    draft_id: str = Field(description="The draft discarded.")


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
    status: DraftStatus = DraftStatus.PENDING
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DraftTaskEdit(BaseModel):
    """User edit to a single task in a draft. Out of bounds is a 422, before anything changes."""

    title: str | None = None
    duration_minutes: int | None = Field(
        default=None, ge=1, le=25,
        description="1 to 25 minutes: a task chunk is one Pomodoro at most, and never empty.",
    )
    difficulty_weight: float | None = Field(default=None, ge=0, le=1)
    start_min: int | None = Field(
        default=None, ge=0, description="Minutes from the draft's horizon start; never before it."
    )


class DraftRearrangeRequest(BaseModel):
    """Rearrange task order in a draft."""

    user_id: str = Field(..., description="User identifier")
    task_order: List[str] = Field(..., description="Ordered list of task IDs defining new order")


class DraftAction(BaseModel):
    """User action on a draft."""

    action: Literal["accept_all", "reject", "edit_task", "rearrange", "chat_modify"]
    draft_id: str
    task_id: str | None = None
    edits: DraftTaskEdit | None = None
    reason: str | None = None
    modification_request: str | None = None
