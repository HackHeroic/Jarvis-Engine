"""Habits endpoints: Jarvis's check-ins on the user's rules, and the rules themselves.

Routing and parsing only. Check-ins (an SM-2 tracker per rule) go through the
container's ``RuleCheckIns``, rules through its ``ConstraintRepository`` and
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
from app.domain.entities import BehavioralConstraint
from app.schemas.habits import CheckInReviewed, RuleCheckInList
from app.services.analytical.habit_translator import invalidate_habit_cache

router = APIRouter()


class TrackerCompleteRequest(BaseModel):
    """The user's answer to a check-in on one of their rules."""

    quality: int = Field(
        ge=0, le=5, description="Still how the user works? 0 = not any more, 5 = exactly."
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
    response_model=CheckInReviewed,
    summary="Answer a check-in on one of the user's rules (SM-2 quality 0-5)",
    description=(
        "Records the answer and spaces the next check-in: 1 day, then 6, then longer while "
        "the rule holds; a rating under 3 brings it back tomorrow. Another user's tracker "
        "or an unknown id: 404."
    ),
)
async def complete_tracker(
    tracker_id: str,
    body: TrackerCompleteRequest,
    http_request: Request,
    user: CurrentUser,
) -> CheckInReviewed:
    """Record a check-in on one of the caller's rules and say when it comes back."""
    require_supabase(http_request)
    check_ins = get_container(http_request).rule_check_ins
    return await check_ins.review(user, tracker_id, body.quality)


@router.get(
    "/tracker/due",
    response_model=RuleCheckInList,
    summary="List the check-ins on the user's rules: due now, and not due yet",
    description=(
        "Every rule in effect, each with one tracker (started here for a rule that has "
        "none, counting from when it was learned, so its first check-in is due a day "
        "after). Due: last check-in (or learning the rule) plus its interval is today or "
        "earlier, both as dates in the user's timezone. A deleted rule is never listed."
    ),
)
async def list_due_trackers(http_request: Request, user: CurrentUser) -> RuleCheckInList:
    """The caller's check-ins, due and not yet due; 503 when the database is down.

    "Today" is the user's: the zone comes from their profile.
    """
    require_supabase(http_request)
    return await get_container(http_request).rule_check_ins.list_for_user(user)


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
        "Removes a saved rule and its check-ins; future schedules stop applying it. A rule "
        "stated in chat takes the memory of stating it with it (forgotten for good). "
        "Another user's rule or an unknown id: 404."
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
