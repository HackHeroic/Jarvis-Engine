"""Task lifecycle endpoints: complete, update, delete, list, skip."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------


class TaskCompleteRequest(BaseModel):
    """Mark a task as completed with quality feedback."""

    user_id: str = Field(..., description="User ID (required for IDOR protection)")
    quality: int = Field(
        default=3,
        ge=0,
        le=5,
        description="Quality rating 0-5 (SM-2 style): 0=blackout, 3=correct with difficulty, 5=perfect",
    )
    actual_duration_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        description="How long the user actually spent (for future pacing adjustments)",
    )


class TaskUpdateRequest(BaseModel):
    """Update a task's mutable fields."""

    user_id: str = Field(..., description="User ID (required for IDOR protection)")
    title: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=120)
    difficulty_weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    deadline_hint: Optional[str] = None
    status: Optional[str] = Field(
        default=None,
        description="New status: pending, completed, skipped, rolled_forward",
    )


class TaskDeleteRequest(BaseModel):
    """Delete a task."""

    user_id: str = Field(..., description="User ID (required for IDOR protection)")


class TaskResponse(BaseModel):
    """Standard task response."""

    task_id: str
    status: str
    message: str
    replan_triggered: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_supabase(request: Request) -> Any:
    """Extract Supabase client from app state."""
    db_client = getattr(request.app.state, "db_client", None)
    if db_client and hasattr(db_client, "supabase"):
        return db_client.supabase
    return None


async def _update_task_status(
    supabase: Any,
    task_id: str,
    user_id: str,
    new_status: str,
    extra_fields: Optional[dict] = None,
) -> dict:
    """Update a task's status in user_tasks, scoped by user_id."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    result = (
        supabase.table("user_tasks")
        .select("task_id, status, user_id")
        .eq("task_id", task_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found for this user")

    update_payload = {
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra_fields:
        update_payload.update(extra_fields)

    supabase.table("user_tasks").update(update_payload).eq(
        "task_id", task_id
    ).eq("user_id", user_id).execute()

    return rows[0]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{task_id}/complete",
    response_model=TaskResponse,
    summary="Mark a task as completed",
    description="Records completion with quality rating. Triggers background replan of remaining tasks.",
)
async def complete_task(
    task_id: str,
    body: TaskCompleteRequest,
    request: Request,
) -> TaskResponse:
    """Complete a task and trigger replan of remaining schedule."""
    supabase = _get_supabase(request)

    extra = {}
    if body.actual_duration_minutes is not None:
        extra["actual_duration_minutes"] = body.actual_duration_minutes

    await asyncio.to_thread(
        _update_task_status_sync,
        supabase, task_id, body.user_id, "completed", extra,
    )

    # Record quality signal for future DKT/RL integration
    _record_completion_signal(
        supabase, task_id, body.user_id, body.quality, body.actual_duration_minutes,
    )

    # Trigger background replan
    db_client = getattr(request.app.state, "db_client", None)
    replan_triggered = await _try_trigger_replan(
        body.user_id, db_client, reason=f"task_completed:{task_id}",
    )

    return TaskResponse(
        task_id=task_id,
        status="completed",
        message="Task completed. Remaining schedule updated.",
        replan_triggered=replan_triggered,
    )


@router.post(
    "/{task_id}/skip",
    response_model=TaskResponse,
    summary="Skip a task",
    description="Marks task as skipped and triggers replan.",
)
async def skip_task(
    task_id: str,
    body: TaskDeleteRequest,
    request: Request,
) -> TaskResponse:
    """Skip a task without completing it."""
    supabase = _get_supabase(request)

    await asyncio.to_thread(
        _update_task_status_sync,
        supabase, task_id, body.user_id, "skipped", None,
    )

    db_client = getattr(request.app.state, "db_client", None)
    replan_triggered = await _try_trigger_replan(
        body.user_id, db_client, reason=f"task_skipped:{task_id}",
    )

    return TaskResponse(
        task_id=task_id,
        status="skipped",
        message="Task skipped. Schedule adjusted.",
        replan_triggered=replan_triggered,
    )


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Update a task",
    description="Update task fields (title, duration, difficulty, deadline, status).",
)
async def update_task(
    task_id: str,
    body: TaskUpdateRequest,
    request: Request,
) -> TaskResponse:
    """Update mutable fields on a task."""
    supabase = _get_supabase(request)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    # Verify task exists and belongs to user
    result = (
        supabase.table("user_tasks")
        .select("task_id, user_id")
        .eq("task_id", task_id)
        .eq("user_id", body.user_id)
        .limit(1)
        .execute()
    )
    if not (result.data or []):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found for this user")

    update_payload: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if body.title is not None:
        update_payload["title"] = body.title
    if body.duration_minutes is not None:
        update_payload["duration_minutes"] = body.duration_minutes
    if body.difficulty_weight is not None:
        update_payload["difficulty_weight"] = body.difficulty_weight
    if body.deadline_hint is not None:
        update_payload["deadline_hint"] = body.deadline_hint
    if body.status is not None:
        update_payload["status"] = body.status

    if len(update_payload) <= 1:  # only updated_at
        return TaskResponse(
            task_id=task_id, status="unchanged", message="No fields to update."
        )

    supabase.table("user_tasks").update(update_payload).eq(
        "task_id", task_id
    ).eq("user_id", body.user_id).execute()

    return TaskResponse(
        task_id=task_id,
        status=body.status or "updated",
        message="Task updated.",
    )


@router.delete(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Delete a task",
    description="Remove a task permanently and trigger replan.",
)
async def delete_task(
    task_id: str,
    user_id: str = Query(..., description="User ID (required for IDOR protection)"),
    request: Request = None,
) -> TaskResponse:
    """Delete a task and trigger replan."""
    supabase = _get_supabase(request)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    # Verify exists
    result = (
        supabase.table("user_tasks")
        .select("task_id")
        .eq("task_id", task_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not (result.data or []):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found for this user")

    supabase.table("user_tasks").delete().eq(
        "task_id", task_id
    ).eq("user_id", user_id).execute()

    db_client = getattr(request.app.state, "db_client", None)
    replan_triggered = await _try_trigger_replan(
        user_id, db_client, reason=f"task_deleted:{task_id}",
    )

    return TaskResponse(
        task_id=task_id,
        status="deleted",
        message="Task removed. Schedule adjusted.",
        replan_triggered=replan_triggered,
    )


@router.delete(
    "/",
    summary="Clear all tasks for a user",
    description="Delete all tasks (or only pending) for a user. Useful for testing/reset.",
)
async def clear_all_tasks(
    user_id: str = Query(..., description="User ID (required for IDOR protection)"),
    status_filter: Optional[str] = Query(
        default=None,
        description="Only delete tasks with this status (e.g. 'pending'). Omit to delete ALL.",
    ),
    request: Request = None,
) -> dict:
    """Clear all tasks for a user, optionally filtered by status."""
    supabase = _get_supabase(request)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    query = supabase.table("user_tasks").delete().eq("user_id", user_id)
    if status_filter:
        query = query.eq("status", status_filter)
    result = query.execute()
    deleted_count = len(result.data or [])

    return {"status": "cleared", "deleted_count": deleted_count, "filter": status_filter or "all"}


@router.get(
    "/",
    summary="List tasks for a user",
    description="Returns pending, completed, and skipped tasks.",
)
async def list_tasks(
    user_id: str = Query(..., description="User ID"),
    status: Optional[str] = Query(
        default=None,
        description="Filter by status: pending, completed, skipped, rolled_forward",
    ),
    request: Request = None,
) -> dict:
    """List all tasks for a user, optionally filtered by status."""
    supabase = _get_supabase(request)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    query = (
        supabase.table("user_tasks")
        .select("task_id, title, status, duration_minutes, difficulty_weight, dependencies, deadline_hint, goal_id, created_at, updated_at, actual_duration_minutes")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(200)
    )
    if status:
        query = query.eq("status", status)

    result = query.execute()
    return {"tasks": result.data or [], "count": len(result.data or [])}


# ---------------------------------------------------------------------------
# Internal helpers (sync wrappers for asyncio.to_thread)
# ---------------------------------------------------------------------------


def _update_task_status_sync(
    supabase: Any,
    task_id: str,
    user_id: str,
    new_status: str,
    extra_fields: Optional[dict],
) -> None:
    """Synchronous wrapper for DB update (runs in thread)."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    result = (
        supabase.table("user_tasks")
        .select("task_id, status, user_id")
        .eq("task_id", task_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found for this user")

    update_payload = {
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra_fields:
        update_payload.update(extra_fields)

    supabase.table("user_tasks").update(update_payload).eq(
        "task_id", task_id
    ).eq("user_id", user_id).execute()


def _record_completion_signal(
    supabase: Any,
    task_id: str,
    user_id: str,
    quality: int,
    actual_duration_minutes: Optional[int],
) -> None:
    """Record a task completion signal for future DKT/RL input.

    Stores in a lightweight signals table. If the table doesn't exist yet,
    silently skip (the signal collection is forward-looking).
    """
    if not supabase:
        return
    try:
        supabase.table("task_completion_signals").insert({
            "user_id": user_id,
            "task_id": task_id,
            "quality": quality,
            "actual_duration_minutes": actual_duration_minutes,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        # Table may not exist yet; silently skip
        pass


async def _try_trigger_replan(
    user_id: str,
    db_client: Any,
    reason: str,
) -> bool:
    """Attempt to trigger a background replan. Returns True if triggered.

    Uses trigger_replan from control_policy. If it fails (e.g., no pending tasks),
    returns False without raising.
    """
    try:
        from app.services.analytical.control_policy import trigger_replan
        asyncio.create_task(trigger_replan(user_id, db_client, reason))
        return True
    except Exception:
        return False
