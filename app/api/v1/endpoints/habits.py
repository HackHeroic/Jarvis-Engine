"""Habits endpoints: SM-2 tracker completion and due trackers."""

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, get_container, require_supabase
from app.domain import errors
from app.services.analytical.sm2_engine import get_due_trackers, record_completion
from app.services.planning.local_time import user_zone

router = APIRouter()


class TrackerCompleteRequest(BaseModel):
    """Request body for marking a habit tracker as complete with SM-2 quality."""

    quality: int = Field(ge=0, le=5, description="SM-2 quality: 0=blackout, 5=perfect recall")


@router.post(
    "/tracker/{tracker_id}/complete",
    summary="Record habit completion with SM-2 quality",
    description="User grades task 0-5. Returns next_review_date and next_interval_days.",
)
async def complete_tracker(
    tracker_id: str,
    body: TrackerCompleteRequest,
    http_request: Request,
    user: CurrentUser,
) -> dict:
    """Record completion and update SM-2 state on one of the caller's trackers."""
    supabase = require_supabase(http_request)
    result = await record_completion(
        tracker_id=tracker_id,
        quality=body.quality,
        user_id=user,
        supabase_client=supabase,
    )
    if result.get("status") == "error":
        if result.get("reason") == "not found":
            raise errors.NotFoundError("Tracker not found.")
        raise errors.PersistenceError("Could not reach the database.")
    return {
        "next_review_date": result.get("next_review_date"),
        "next_interval_days": result.get("next_interval_days"),
    }


@router.get(
    "/tracker/due",
    summary="List habit trackers due for review",
    description=(
        "Returns trackers where last_done_at + next_interval_days <= today, both as dates "
        "in the user's timezone."
    ),
)
async def list_due_trackers(http_request: Request, user: CurrentUser) -> dict:
    """List SM-2 trackers due for review today or earlier; 503 when the database is down.

    "Today" is the user's: the zone comes from their profile. An outage is never an
    empty list: "nothing due" would be a false answer.
    """
    supabase = require_supabase(http_request)
    profile = await get_container(http_request).repos.profiles.get_or_create(user)
    due = await get_due_trackers(user_id=user, supabase_client=supabase, zone=user_zone(profile))
    return {"due_trackers": due}


@router.get(
    "/constraints",
    summary="List active behavioral constraints",
    description="Constraints saved from chat (store_constraint) that shape every schedule.",
)
async def list_behavioral_constraints(
    http_request: Request,
    user_id: str = Query(..., description="User ID"),
) -> dict:
    """List the user's behavioral constraints, newest first; 503 when the database is down.

    An outage is never an empty list: "no rules" would be a false answer.
    """
    import asyncio

    supabase = require_supabase(http_request)

    def _query():
        return (
            supabase.table("behavioral_constraints")
            .select("id,raw_text,constraint_type,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )

    try:
        result = await asyncio.to_thread(_query)
    except Exception as exc:
        raise errors.PersistenceError("Could not reach the database.") from exc
    return {"constraints": result.data or []}


@router.delete(
    "/constraints/{constraint_id}",
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
) -> dict:
    """Delete one constraint, user-scoped (IDOR: id alone is not authorization)."""
    from app.services.extraction.behavioral_store import invalidate_habit_cache

    require_supabase(http_request)
    rules = get_container(http_request).stated_rules
    if not await rules.delete_rule(user_id, constraint_id):
        raise errors.NotFoundError("That rule does not exist.")
    invalidate_habit_cache()
    return {"status": "deleted", "id": constraint_id}
