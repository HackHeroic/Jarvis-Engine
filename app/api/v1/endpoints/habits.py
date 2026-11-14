"""Habits endpoints: SM-2 trackers and the user's rules (behavioural constraints).

Routing and parsing only. Trackers go through ``sm2_engine`` over the container's
``HabitTrackerRepository``, rules through its ``ConstraintRepository`` and
``StatedRules``. Every route first requires the database (``require_supabase``):
without it the container runs on in-memory repositories, where the page would read
as a brand-new user. An outage is a 503 in the one error envelope, never an empty list.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, get_container, require_supabase
from app.domain import errors
from app.domain.entities import BehavioralConstraint, HabitTracker
from app.services.analytical.habit_translator import invalidate_habit_cache
from app.services.analytical.sm2_engine import get_due_trackers, record_completion
from app.services.planning.local_time import user_zone

router = APIRouter()


class TrackerCompleteRequest(BaseModel):
    """Request body for marking a habit tracker as complete with SM-2 quality."""

    quality: int = Field(ge=0, le=5, description="SM-2 quality: 0=blackout, 5=perfect recall")


class TrackerCompleteResponse(BaseModel):
    """When the habit is next due, after this review."""

    next_review_date: datetime = Field(description="When the next review is due (UTC).")
    next_interval_days: int = Field(ge=0, description="Days from now until that review.")


class DueTrackersResponse(BaseModel):
    """The user's trackers due for review."""

    due_trackers: list[HabitTracker] = Field(
        description="Trackers due today or earlier on the user's clock, newest first."
    )


class ConstraintOut(BaseModel):
    """One rule the user's plans respect, as the Habits page lists it."""

    id: str = Field(description="The rule's id; delete it at /habits/constraints/{id}.")
    raw_text: str = Field(description="The rule as said, or as inferred.")
    constraint_type: str = Field(description="Kind of rule (e.g. habit, sleep).")
    created_at: datetime | None = Field(
        description="When it was stored; null for a rule older than the stamp."
    )

    @classmethod
    def of(cls, rule: BehavioralConstraint) -> "ConstraintOut":
        return cls(
            id=rule.id,
            raw_text=rule.raw_text,
            constraint_type=rule.constraint_type,
            created_at=rule.created_at,
        )


class ConstraintListResponse(BaseModel):
    """The rules that apply now: the same list the You page shows."""

    constraints: list[ConstraintOut] = Field(description="Rules in effect, newest first.")


class ConstraintDeleteResponse(BaseModel):
    """A rule was deleted."""

    status: Literal["deleted"] = Field(description="Always 'deleted'.")
    id: str = Field(description="The rule deleted.")


@router.post(
    "/tracker/{tracker_id}/complete",
    response_model=TrackerCompleteResponse,
    summary="Record habit completion with SM-2 quality",
    description=(
        "User grades the review 0-5. Returns next_review_date and next_interval_days. "
        "Another user's tracker or an unknown id: 404."
    ),
)
async def complete_tracker(
    tracker_id: str,
    body: TrackerCompleteRequest,
    http_request: Request,
    user: CurrentUser,
) -> TrackerCompleteResponse:
    """Record a review and update the SM-2 state of one of the caller's trackers."""
    require_supabase(http_request)
    trackers = get_container(http_request).repos.habit_trackers
    result = await record_completion(trackers, user, tracker_id, body.quality)
    return TrackerCompleteResponse(**result)


@router.get(
    "/tracker/due",
    response_model=DueTrackersResponse,
    summary="List habit trackers due for review",
    description=(
        "Returns trackers where last_done_at + next_interval_days <= today, both as dates "
        "in the user's timezone."
    ),
)
async def list_due_trackers(http_request: Request, user: CurrentUser) -> DueTrackersResponse:
    """List SM-2 trackers due for review today or earlier; 503 when the database is down.

    "Today" is the user's: the zone comes from their profile.
    """
    require_supabase(http_request)
    container = get_container(http_request)
    profile = await container.repos.profiles.get_or_create(user)
    due = await get_due_trackers(container.repos.habit_trackers, user, user_zone(profile))
    return DueTrackersResponse(due_trackers=due)


@router.get(
    "/constraints",
    response_model=ConstraintListResponse,
    summary="List active behavioral constraints",
    description=(
        "The rules every schedule respects, newest first: exactly the rules the You page "
        "lists (a rule past its validity window is in neither)."
    ),
)
async def list_behavioral_constraints(
    http_request: Request,
    user_id: str = Query(..., description="User ID"),
) -> ConstraintListResponse:
    """List the user's rules in effect, newest first; 503 when the database is down."""
    require_supabase(http_request)
    rules = await get_container(http_request).repos.constraints.list_active(user_id)
    return ConstraintListResponse(constraints=[ConstraintOut.of(r) for r in reversed(rules)])


@router.delete(
    "/constraints/{constraint_id}",
    response_model=ConstraintDeleteResponse,
    summary="Delete a behavioral constraint",
    description=(
        "Removes a saved rule; future schedules stop applying it. A rule stated in chat "
        "takes the memory of stating it with it (forgotten for good). Another user's rule "
        "or an unknown id: 404."
    ),
)
async def delete_behavioral_constraint(
    constraint_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User ID"),
) -> ConstraintDeleteResponse:
    """Delete one rule, user-scoped (IDOR: id alone is not authorization)."""
    require_supabase(http_request)
    rules = get_container(http_request).stated_rules
    if not await rules.delete_rule(user_id, constraint_id):
        raise errors.NotFoundError("That rule does not exist.")
    invalidate_habit_cache()
    return ConstraintDeleteResponse(status="deleted", id=constraint_id)
