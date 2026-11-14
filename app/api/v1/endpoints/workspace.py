"""Proactive Task Workspace endpoint — cache-first."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.deps import container_of
from app.domain import errors
from app.schemas.workspace import TaskWorkspace
from app.services.analytical.workspace_builder import (
    get_or_build_task_workspace,
    invalidate_workspace_cache,
)

router = APIRouter()


@router.get("/{task_id}/workspace", response_model=TaskWorkspace)
async def get_task_workspace(
    task_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User ID (required for IDOR protection)"),
    prompt: Optional[str] = Query(
        default=None,
        description="Optional user question for dynamic practice surfacing",
    ),
    refresh: bool = Query(
        default=False,
        description="Force a rebuild even if a cached workspace exists",
    ),
) -> TaskWorkspace:
    """Get cached workspace, or build + persist (24h TTL) if missing/stale/refresh."""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    # The goals the WOOP card reads, and the bus a web search is metered onto: the
    # composition root's; without one the card keeps the task's title and nothing is metered.
    container = container_of(http_request)
    try:
        return await get_or_build_task_workspace(
            user_id=user_id,
            task_id=task_id,
            user_prompt=prompt,
            refresh=refresh,
            goals=container.repos.goals if container is not None else None,
            bus=container.events if container is not None else None,
        )
    except errors.JarvisError:
        raise  # already says what went wrong, with its own status
    except Exception as exc:
        # Not only a database call (RAG, web search and an LLM run here too), so
        # a plain 500. The handler logs the chained cause; its text stays off the wire.
        raise errors.JarvisError("Could not build the task workspace.") from exc


@router.delete("/{task_id}/workspace")
async def clear_task_workspace(
    task_id: str,
    user_id: str = Query(..., description="User ID"),
) -> dict:
    """Invalidate the cached workspace for a task. Next GET will rebuild."""
    await invalidate_workspace_cache(user_id, task_id)
    return {"status": "invalidated"}
