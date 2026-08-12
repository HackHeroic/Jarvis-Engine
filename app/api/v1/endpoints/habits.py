"""Habits endpoints: SM-2 tracker completion and due trackers."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.analytical.sm2_engine import get_due_trackers, record_completion

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
    user_id: str = Query(None, description="User ID (optional, for multi-tenant IDOR protection)"),
) -> dict:
    """Record completion and update SM-2 state."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    result = await record_completion(
        tracker_id=tracker_id,
        quality=body.quality,
        user_id=user_id,
        supabase_client=supabase,
    )
    if result.get("status") == "error":
        reason = result.get("reason", "")
        if "not found" in reason.lower():
            raise HTTPException(status_code=404, detail="Tracker not found")
        if "forbidden" in reason.lower():
            raise HTTPException(status_code=403, detail="Forbidden")
        raise HTTPException(status_code=500, detail=reason or "Completion failed")
    return {
        "next_review_date": result.get("next_review_date"),
        "next_interval_days": result.get("next_interval_days"),
    }


@router.get(
    "/tracker/due",
    summary="List habit trackers due for review",
    description="Returns trackers where last_done_at + next_interval_days <= today.",
)
async def list_due_trackers(
    http_request: Request,
    user_id: str = Query(..., description="User ID"),
) -> dict:
    """List SM-2 trackers due for review today or earlier."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    due = await get_due_trackers(user_id=user_id, supabase_client=supabase)
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
    """List the user's behavioral constraints, newest first. Degrades to [] when DB is down."""
    import asyncio

    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if supabase is None:
        return {"constraints": []}

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
        return {"constraints": result.data or []}
    except Exception:
        return {"constraints": []}


@router.delete(
    "/constraints/{constraint_id}",
    summary="Delete a behavioral constraint",
    description="Removes a saved rule; future schedules stop applying it.",
)
async def delete_behavioral_constraint(
    constraint_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User ID"),
) -> dict:
    """Delete one constraint, user-scoped (IDOR: id alone is not authorization)."""
    import asyncio

    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if supabase is None:
        raise HTTPException(status_code=503, detail="Storage unavailable")

    def _delete():
        return (
            supabase.table("behavioral_constraints")
            .delete()
            .eq("id", constraint_id)
            .eq("user_id", user_id)
            .execute()
        )

    result = await asyncio.to_thread(_delete)
    if not (result.data or []):
        raise HTTPException(status_code=404, detail="Constraint not found")
    from app.services.extraction.behavioral_store import invalidate_habit_cache
    invalidate_habit_cache()
    return {"status": "deleted", "id": constraint_id}
