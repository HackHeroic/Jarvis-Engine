"""Proactive Task Workspace endpoint."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.schemas.workspace import TaskWorkspace
from app.services.analytical.workspace_builder import build_task_workspace

router = APIRouter()


@router.get("/{task_id}/workspace", response_model=TaskWorkspace)
async def get_task_workspace(
    task_id: str,
    user_id: str = Query(..., description="User ID (required for IDOR protection)"),
    prompt: Optional[str] = Query(
        default=None,
        description="Optional user question for dynamic practice surfacing",
    ),
) -> TaskWorkspace:
    """Get proactive workspace for a task: RAG chunks, learning-style links, practice assets."""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    try:
        workspace = await build_task_workspace(
            user_id=user_id,
            task_id=task_id,
            user_prompt=prompt,
        )
        return workspace
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
