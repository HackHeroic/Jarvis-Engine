"""Behavioral pipeline: Store constraints in Strategy Hub (L7)."""

from typing import Any, Optional

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from supabase import create_client


def _get_supabase():
    """Get Supabase client. Uses config env vars."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def store_behavioral_constraint(
    raw_text: str,
    constraint_type: str = "preference",
    user_id: Optional[str] = None,
    structured_override: Optional[dict[str, Any]] = None,
    supabase_client=None,
) -> dict[str, Any]:
    """Store a behavioral constraint in Strategy Hub (Supabase).

    Args:
        raw_text: Raw user statement (e.g. "I sit in back bench during OS").
        constraint_type: One of "preference", "habit", "availability_override".
        user_id: Optional user identifier.
        structured_override: Optional parsed structure (e.g. {"slot_pattern": "OS", "availability": "minimal_work"}).
        supabase_client: Optional Supabase client; creates one if not provided.

    Returns:
        Dict with stored id and status. If Supabase unavailable, returns status without persisting.
    """
    client = supabase_client or _get_supabase()
    if not client:
        return {"status": "skipped", "reason": "Supabase not configured"}

    row = {
        "raw_text": raw_text,
        "constraint_type": constraint_type,
        "structured_override": structured_override or {},
    }
    if user_id:
        row["user_id"] = user_id

    try:
        result = client.table("behavioral_constraints").insert(row).execute()
        if result.data and len(result.data) > 0:
            return {"status": "stored", "id": result.data[0].get("id")}
        return {"status": "stored"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def get_behavioral_context_for_calendar(user_id: Optional[str] = None, supabase_client=None) -> str:
    """Fetch relevant behavioral constraints to pass as user_context to calendar extraction.

    Returns concatenated raw_text of constraints that affect calendar slots.
    """
    client = supabase_client or _get_supabase()
    if not client:
        return ""

    try:
        query = client.table("behavioral_constraints").select("raw_text").order("created_at", desc=True).limit(10)
        if user_id:
            query = query.eq("user_id", user_id)
        result = query.execute()
        if result.data:
            return "; ".join(r.get("raw_text", "") for r in result.data if r.get("raw_text"))
    except Exception:
        pass
    return ""


async def delete_behavioral_constraint(
    constraint_id: str,
    user_id: Optional[str] = None,
    supabase_client=None,
) -> dict[str, Any]:
    """Delete a behavioral constraint by id from Strategy Hub (Supabase).

    Args:
        constraint_id: UUID of the constraint to delete.
        user_id: Optional user identifier. When provided, delete is scoped to this user (IDOR protection).
        supabase_client: Optional Supabase client; creates one if not provided.

    Returns:
        Dict with status. On success: {"status": "deleted", "id": constraint_id}.
        On error: {"status": "error", "reason": "..."}. Use reason "not found" for 404.
    """
    client = supabase_client or _get_supabase()
    if not client:
        return {"status": "error", "reason": "Supabase not configured"}

    try:
        query = client.table("behavioral_constraints").select("id").eq("id", constraint_id)
        if user_id:
            query = query.eq("user_id", user_id)
        result = query.execute()
        if not result.data or len(result.data) == 0:
            return {"status": "error", "reason": "not found"}

        delete_query = client.table("behavioral_constraints").delete().eq("id", constraint_id)
        if user_id:
            delete_query = delete_query.eq("user_id", user_id)
        delete_query.execute()
        return {"status": "deleted", "id": constraint_id}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
