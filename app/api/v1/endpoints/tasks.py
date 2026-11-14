"""Task lifecycle endpoints: complete, update, delete, list, skip.

Complete and Skip are outcomes: they go through ``TaskService``, which records
each one as a single append-only task event.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_container
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain import errors
from app.domain.entities import TaskStatus

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
        description=(
            "Not accepted here: any value is a 422. Status changes go through "
            "POST /tasks/{task_id}/complete or /skip, which record each outcome once."
        ),
    )


class TaskDeleteRequest(BaseModel):
    """Delete a task."""

    user_id: str = Field(..., description="User ID (required for IDOR protection)")


class TaskResponse(BaseModel):
    """Standard task response."""

    task_id: str
    status: str
    message: str
    # Always False: the background replan (control_policy.trigger_replan) is off
    # until it can run to completion. Kept because the frontend reads it.
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


def _require_supabase(request: Request) -> Any:
    """The Supabase client, or the one 503 every task route answers without it.

    Complete and Skip check it too: with Supabase unreachable the container runs on
    in-memory repositories, where every task is unknown, and a 404 would tell the
    user their task is gone.
    """
    supabase = _get_supabase(request)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")
    return supabase


# What a task that is no longer pending already is, in the user's words.
_ALREADY = {TaskStatus.COMPLETED: "done", TaskStatus.MISSED: "set aside"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{task_id}/complete",
    response_model=TaskResponse,
    summary="Mark a task as completed",
    description=(
        "Records one completed outcome event with the quality rating and actual "
        "duration. A repeat click records nothing more; an unknown task is a 404."
    ),
)
async def complete_task(
    task_id: str,
    body: TaskCompleteRequest,
    request: Request,
) -> TaskResponse:
    """Complete a task and record its quality signal."""
    supabase = _require_supabase(request)
    event = await get_container(request).task_service.complete(
        body.user_id,
        task_id,
        actual_duration_minutes=body.actual_duration_minutes,
        quality=body.quality,
    )
    if event is not None:  # a repeat click recorded nothing, so it adds no signal either
        _record_completion_signal(
            supabase, task_id, body.user_id, body.quality, body.actual_duration_minutes,
        )

    return TaskResponse(
        task_id=task_id,
        status="completed",
        message="Task completed.",
        replan_triggered=False,
    )


@router.post(
    "/{task_id}/skip",
    response_model=TaskResponse,
    summary="Skip a task (anti-guilt)",
    description="Marks the task skipped (never 'overdue' or 'failed') and records one "
    "skipped outcome event. Suggests today's buffer or tomorrow morning for it "
    "(nothing is re-placed). An unknown task is a 404.",
)
async def skip_task(
    task_id: str,
    body: TaskDeleteRequest,
    request: Request,
) -> TaskResponse:
    """Anti-guilt skip — 'Slippery Deadlines'.

    Never says 'overdue' or 'failed'. A repeat skip, or a skip of a task that is
    already done, records nothing.
    """
    _require_supabase(request)
    container = get_container(request)
    await container.task_service.skip(body.user_id, task_id)
    task = await container.repos.tasks.get(body.user_id, task_id)
    if task is None:
        raise errors.NotFoundError("That task does not exist.", details={"task_id": task_id})
    if task.status is not TaskStatus.SKIPPED:
        return TaskResponse(
            task_id=task_id,
            status=task.status.value,
            message=(
                f"'{task.title}' is already {_ALREADY.get(task.status, 'on your list')}, "
                "so nothing changed."
            ),
        )

    # Buffer advice, not a claim: nothing re-places the task on the schedule here.
    from app.utils.pacing import compute_adaptive_daily_cap
    daily_cap = compute_adaptive_daily_cap(horizon_minutes=2880, total_task_minutes=480)
    buffer_minutes = daily_cap * 0.25  # 25% anti-guilt reserve
    if task.duration_minutes <= buffer_minutes:
        message = (
            f"Life happened — '{task.title}' is off the list for now. "
            f"It's short enough for today's buffer time if you want it back."
        )
    else:
        message = (
            f"Life happened — '{task.title}' is off the list for now, "
            f"and tomorrow morning suits it well. No stress."
        )

    return TaskResponse(
        task_id=task_id,
        status="skipped",
        message=message,
        replan_triggered=False,
    )


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Update a task",
    description=(
        "Update task fields (title, duration, difficulty, deadline). A status is a 422: "
        "status changes go through POST /tasks/{task_id}/complete or /skip."
    ),
)
async def update_task(
    task_id: str,
    body: TaskUpdateRequest,
    request: Request,
) -> TaskResponse:
    """Update mutable fields on a task.

    ``status`` is rejected with a 422 before the database is touched. An outcome is
    recorded by Complete or Skip, through TaskService (a compare-and-set plus one
    outcome event); a PATCH would move the status with no event.
    """
    if body.status is not None:
        raise errors.ValidationError(
            "A task's status changes through POST /api/v1/tasks/{task_id}/complete "
            "or /skip, not through an update.",
            details={"field": "status"},
        )
    supabase = _require_supabase(request)

    # Verify task exists and belongs to user
    result = (
        supabase.table("user_tasks")
        .select("*")
        .eq("task_id", task_id)
        .eq("user_id", body.user_id)
        .limit(1)
        .execute()
    )
    if not (result.data or []):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found for this user")

    update_payload: dict[str, Any] = {}
    if body.title is not None:
        update_payload["title"] = body.title
    if body.duration_minutes is not None:
        update_payload["duration_minutes"] = body.duration_minutes
    if body.difficulty_weight is not None:
        update_payload["difficulty_weight"] = body.difficulty_weight
    if body.deadline_hint is not None:
        update_payload["deadline_hint"] = body.deadline_hint

    if len(update_payload) == 0:
        return TaskResponse(
            task_id=task_id, status="unchanged", message="No fields to update."
        )

    supabase.table("user_tasks").update(update_payload).eq(
        "task_id", task_id
    ).eq("user_id", body.user_id).execute()

    # No replan runs: the background replan is off.
    return TaskResponse(
        task_id=task_id,
        status="updated",
        message="Task updated.",
        replan_triggered=False,
    )


@router.delete(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Delete a task",
    description="Remove a task permanently.",
)
async def delete_task(
    task_id: str,
    user_id: str = Query(..., description="User ID (required for IDOR protection)"),
    request: Request = None,
) -> TaskResponse:
    """Delete a task."""
    supabase = _require_supabase(request)

    # Verify exists
    result = (
        supabase.table("user_tasks")
        .select("*")
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

    return TaskResponse(
        task_id=task_id,
        status="deleted",
        message="Task removed.",
        replan_triggered=False,
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
    supabase = _require_supabase(request)

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
        description="Filter by status: pending, completed, skipped, missed",
    ),
    request: Request = None,
) -> dict:
    """List all tasks for a user, optionally filtered by status."""
    supabase = _require_supabase(request)

    query = (
        supabase.table("user_tasks")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(200)
    )
    if status:
        query = query.eq("status", status)

    result = query.execute()
    return {"tasks": result.data or [], "count": len(result.data or [])}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _record_completion_signal(
    supabase: Any,
    task_id: str,
    user_id: str,
    quality: int,
    actual_duration_minutes: Optional[int],
) -> None:
    """Record a task completion signal for future DKT/RL input.

    Writes only the columns ``task_completion_signals`` has; the actual duration
    rides in ``metadata``. A failed write is logged and never fails the completion.
    """
    if not supabase:
        return
    try:
        supabase.table("task_completion_signals").insert({
            "user_id": user_id,
            "task_id": task_id,
            "signal_type": "completed",
            "quality": quality,
            "metadata": {"actual_duration_minutes": actual_duration_minutes},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:
        logger.warning(f"Failed to record completion signal for task {task_id}: {exc}")
