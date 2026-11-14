"""Task lifecycle endpoints: complete, skip, list.

Complete and Skip are outcomes: they go through ``TaskService``, which records
each one as a single append-only task event (with the quality and actual duration of
a completion: ADR-11's one outcome record). The list reads ``TaskRepository``. A
task's row is written by draft acceptance alone, so no route here edits or deletes one.
"""

from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_container, require_supabase
from app.domain import errors
from app.domain.entities import Task, TaskStatus
from app.domain.ports import TASK_LIST_LIMIT

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


# The longest skip reason kept: a sentence or two, never an essay.
SKIP_REASON_MAX_CHARS = 500


class TaskSkipRequest(BaseModel):
    """Skip a task, optionally saying why."""

    user_id: str = Field(..., description="User ID (required for IDOR protection)")
    reason: Optional[str] = Field(
        default=None,
        max_length=SKIP_REASON_MAX_CHARS,
        description="Why it was set aside, in the user's words; kept on the skip's event.",
    )


class TaskListResponse(BaseModel):
    """The user's tasks, newest first."""

    tasks: list[Task] = Field(
        description=f"The user's tasks, newest first (at most {TASK_LIST_LIMIT})."
    )
    count: int = Field(ge=0, description="How many tasks came back.")


class TaskResponse(BaseModel):
    """Standard task response."""

    task_id: str
    status: str
    message: str
    # Always False: no task endpoint starts a replan (the v1 background replan was
    # retired with the v1 chat). Kept because the frontend reads it.
    replan_triggered: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _not_found(task_id: str) -> errors.NotFoundError:
    return errors.NotFoundError("That task does not exist.", details={"task_id": task_id})


# What a task that is no longer pending already is, in the user's words.
_ALREADY = {TaskStatus.COMPLETED: "done", TaskStatus.MISSED: "set aside"}


# ---------------------------------------------------------------------------
# Endpoints
#
# Every route first requires the database (``require_supabase``), Complete and Skip
# too: with Supabase unreachable the container runs on in-memory repositories, where
# every task is unknown, and a 404 would tell the user their task is gone.
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
    """Complete a task; its completed event carries the quality and actual duration."""
    require_supabase(request)
    await get_container(request).task_service.complete(
        body.user_id,
        task_id,
        actual_duration_minutes=body.actual_duration_minutes,
        quality=body.quality,
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
    body: TaskSkipRequest,
    request: Request,
) -> TaskResponse:
    """Anti-guilt skip — 'Slippery Deadlines'.

    Never says 'overdue' or 'failed'. The reason, if given, is kept on the skipped
    event. A repeat skip, or a skip of a task that is already done, records nothing.
    """
    require_supabase(request)
    container = get_container(request)
    await container.task_service.skip(body.user_id, task_id, reason=body.reason)
    task = await container.repos.tasks.get(body.user_id, task_id)
    if task is None:
        raise _not_found(task_id)
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


@router.get(
    "/",
    response_model=TaskListResponse,
    summary="List tasks for a user",
    description=(
        f"The user's newest {TASK_LIST_LIMIT} tasks (every status, or only `status`), "
        "newest first."
    ),
)
async def list_tasks(
    request: Request,
    user_id: str = Query(..., description="User ID"),
    status: Optional[TaskStatus] = Query(
        default=None,
        description="Only tasks in this status: pending, completed, skipped or missed.",
    ),
) -> TaskListResponse:
    """List the user's tasks, optionally only those in one status; 503 without a database."""
    require_supabase(request)
    statuses = frozenset({status}) if status is not None else None
    tasks = await get_container(request).repos.tasks.list_for_user(user_id, statuses=statuses)
    return TaskListResponse(tasks=tasks, count=len(tasks))
