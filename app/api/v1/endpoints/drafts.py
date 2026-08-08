# app/api/v1/endpoints/drafts.py
"""Draft review/accept/reject endpoints.

Routing and parsing only — the accept two-step (flip the draft's status, then
persist its tasks) lives in ``app.services.draft_actions`` because the
orchestrator's ``draft_action`` node performs exactly the same operation on the
conversational path.

Every handler passes ``user_id`` into ``DraftStore``, which filters on it, so a
draft belonging to somebody else reads as absent and answers 404 — never 403,
which would confirm the row exists.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas.draft import (
    DraftAcceptRequest,
    DraftChatRequest,
    DraftModifyRequest,
    DraftRearrangeRequest,
    DraftRejectRequest,
    DraftStateResponse,
    DraftTaskEdit,
)
from app.services.draft_actions import accept_draft_and_persist

router = APIRouter()

_NOT_FOUND = "Draft not found or expired"


def _get_draft_store(request: Request):
    store = getattr(request.app.state, "draft_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Draft store not available")
    return store


async def _load_draft(request: Request, draft_id: str, user_id: str) -> tuple:
    """Fetch the draft or 404. Returns ``(store, row)``.

    DraftStore is a synchronous Supabase client, so every call goes through
    ``to_thread`` — a bare call would block the event loop for the whole HTTP
    round-trip (house rule, same as planning_graph.create_draft).
    """
    store = _get_draft_store(request)
    draft = await asyncio.to_thread(store.get_draft, draft_id, user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return store, draft


def _supabase_of(request: Request):
    db_client = getattr(request.app.state, "db_client", None)
    return getattr(db_client, "supabase", None) if db_client else None


@router.get(
    "/{draft_id}",
    response_model=DraftStateResponse,
    summary="Get draft state",
)
async def get_draft(draft_id: str, user_id: str, http_request: Request):
    """Retrieve the current state of a draft for user review."""
    _, draft = await _load_draft(http_request, draft_id, user_id)
    return DraftStateResponse(
        draft_id=draft["id"],
        user_id=draft["user_id"],
        goal_id=draft.get("goal_id"),
        tasks=draft.get("tasks") or [],
        horizon_start=draft.get("horizon_start"),
        status=draft.get("status", "pending"),
        rejection_reason=draft.get("rejection_reason"),
        created_at=str(draft["created_at"]) if draft.get("created_at") else None,
    )


@router.post("/{draft_id}/accept", summary="Accept a draft and persist its tasks")
async def accept_draft(draft_id: str, request: DraftAcceptRequest, http_request: Request):
    """Accept the draft: mark it accepted and write its tasks to ``user_tasks``.

    This is the only REST path that commits a proposed schedule — the planning
    flow deliberately persists nothing until the user says yes.
    """
    store, draft = await _load_draft(http_request, draft_id, request.user_id)
    task_count = await accept_draft_and_persist(
        store, request.user_id, draft, _supabase_of(http_request)
    )
    return {"status": "accepted", "draft_id": draft_id, "task_count": task_count}


@router.post("/{draft_id}/reject", summary="Reject a draft")
async def reject_draft(draft_id: str, request: DraftRejectRequest, http_request: Request):
    """Reject the draft. Nothing is persisted to ``user_tasks``."""
    store, _ = await _load_draft(http_request, draft_id, request.user_id)
    await asyncio.to_thread(store.reject_draft, draft_id, request.user_id, request.reason)

    # The reason is behavioural signal — it is why the next plan should differ.
    memory_store = getattr(http_request.app.state, "memory_store", None)
    if memory_store and request.reason:
        asyncio.create_task(
            memory_store.store_memory(
                user_id=request.user_id,
                memory_type="feedback",
                content=f"User rejected schedule draft: {request.reason}",
                confidence=0.5,
            )
        )

    return {"status": "rejected", "draft_id": draft_id}


@router.post("/{draft_id}/modify", summary="Replace a draft's task list")
async def modify_draft(draft_id: str, request: DraftModifyRequest, http_request: Request):
    """Replace the draft's tasks wholesale.

    Only ``component == "tasks"`` is meaningful: draft_schedules stores a task
    array, and habits/action_items are no longer part of a draft at all.
    """
    if request.component != "tasks":
        raise HTTPException(
            status_code=400,
            detail="Only the 'tasks' component is modifiable on a draft schedule",
        )
    if not isinstance(request.data, list):
        raise HTTPException(status_code=400, detail="'data' must be a list of tasks")

    store, _ = await _load_draft(http_request, draft_id, request.user_id)
    updated = await asyncio.to_thread(store.replace_tasks, draft_id, request.user_id, request.data)
    if updated is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return {"status": "modified", "component": "tasks", "draft_id": draft_id}


@router.delete("/{draft_id}", summary="Discard a draft")
async def delete_draft(draft_id: str, user_id: str, http_request: Request):
    """Discard an entire draft (user decided not to proceed)."""
    store, _ = await _load_draft(http_request, draft_id, user_id)
    await asyncio.to_thread(store.delete_draft, draft_id, user_id)
    return {"status": "deleted", "draft_id": draft_id}


@router.patch("/{draft_id}/tasks/{task_id}", summary="Edit one task in a draft")
async def edit_draft_task(
    draft_id: str,
    task_id: str,
    edits: DraftTaskEdit,
    user_id: str = Query(..., description="User ID for authorization"),
    request: Request = None,
):
    """Edit a single task in a draft."""
    store = _get_draft_store(request)

    edits_dict = edits.model_dump(exclude_none=True)
    if not edits_dict:
        raise HTTPException(status_code=400, detail="No edits provided")

    result = await asyncio.to_thread(
        store.edit_task_in_draft, draft_id, user_id, task_id, edits_dict
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Draft or task not found")

    return {"status": "modified", "draft_id": draft_id, "task_id": task_id, "updated_draft": result}


@router.post("/{draft_id}/rearrange", summary="Rearrange task order in draft")
async def rearrange_draft(draft_id: str, request: DraftRearrangeRequest, http_request: Request):
    """Reorder tasks within a draft according to the user-specified order."""
    store, draft = await _load_draft(http_request, draft_id, request.user_id)

    tasks = draft.get("tasks") or []
    by_id = {t["task_id"]: t for t in tasks if isinstance(t, dict) and t.get("task_id")}

    # Requested ids first, in order; anything unmentioned keeps its relative
    # place at the back rather than being silently dropped.
    reordered = [by_id.pop(tid) for tid in request.task_order if tid in by_id]
    reordered.extend(by_id.values())

    await asyncio.to_thread(store.replace_tasks, draft_id, request.user_id, reordered)

    # TODO: re-solve with OR-Tools after a rearrange — the stored start times
    # are the solver's and no longer match the new order.
    return {
        "status": "rearranged",
        "draft_id": draft_id,
        "task_count": len(reordered),
        "needs_resolve": True,
    }


@router.post("/{draft_id}/chat", summary="Modify draft via natural language")
async def chat_modify_draft(draft_id: str, request: DraftChatRequest, http_request: Request):
    """Apply a natural language modification to a draft schedule."""
    await _load_draft(http_request, draft_id, request.user_id)

    schedule_modifier = getattr(http_request.app.state, "schedule_modifier", None)
    if schedule_modifier:
        modified_draft = await schedule_modifier.modify(
            draft_id=draft_id,
            user_id=request.user_id,
            message=request.message,
        )
        return {"status": "modified", "draft_id": draft_id, "result": modified_draft}

    return {"status": "modified", "draft_id": draft_id}
