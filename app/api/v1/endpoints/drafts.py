# app/api/v1/endpoints/drafts.py
"""Draft review/accept/reject endpoints.

Routing and parsing only. Accepting (write the tasks and mark the draft accepted,
in one transaction) and rejecting are ``DraftService`` moves, taken from the
composition root (``get_container``) — the same service the orchestrator's
``draft_action`` node reaches through ``app.services.draft_actions``.

A draft that is no longer open (already accepted, rejected or expired) cannot be
accepted, rejected or edited: ``InvalidStateTransition`` reaches the global
handler, which answers 409 in the error envelope.

Every handler passes ``user_id`` into ``DraftStore``, which filters on it, so a
draft belonging to somebody else reads as absent and answers 404 — never 403,
which would confirm the row exists.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.deps import get_container
from app.domain import errors
from app.schemas.draft import (
    DraftAcceptRequest,
    DraftChatRequest,
    DraftModifyRequest,
    DraftRearrangeRequest,
    DraftRejectRequest,
    DraftStateResponse,
    DraftTaskEdit,
)
from app.services.draft_store import is_open_draft

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


async def _refused_edit(store, draft_id: str, user_id: str, not_found: str) -> Exception:
    """Why an edit wrote nothing: a closed draft is a 409, anything else a 404.

    An edit must never reopen an accepted draft, or a second accept would write
    its tasks again.
    """
    draft = await asyncio.to_thread(store.get_draft, draft_id, user_id)
    if draft is not None and not is_open_draft(draft):
        return errors.InvalidStateTransition(
            "This draft is no longer open, so it cannot be changed.",
            details={"draft_id": draft_id, "from": draft.get("status"), "to": "modified"},
        )
    return HTTPException(status_code=404, detail=not_found)


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
    """Accept the draft: write its tasks to ``user_tasks`` and mark it accepted.

    This is the only REST path that commits a proposed schedule — the planning
    flow deliberately persists nothing until the user says yes. A second accept
    answers 409 and writes nothing.
    """
    await _load_draft(http_request, draft_id, request.user_id)
    drafts = get_container(http_request).draft_service
    try:
        task_count = await drafts.accept(request.user_id, draft_id)
    except errors.ValidationError:
        # Nothing in the draft can be scheduled; nothing was written.
        return {"status": "empty", "draft_id": draft_id, "task_count": 0}
    except errors.PersistenceError as exc:
        # The tasks did not land, so the draft is still open and the client
        # may retry. Answering 200 "accepted" here is how a user ends up with
        # an empty schedule and no idea why.
        raise errors.PersistenceError(
            "The schedule could not be saved. The draft is still open; try again."
        ) from exc
    return {"status": "accepted", "draft_id": draft_id, "task_count": task_count}


@router.post("/{draft_id}/reject", summary="Reject a draft")
async def reject_draft(draft_id: str, request: DraftRejectRequest, http_request: Request):
    """Reject the draft. Nothing is persisted to ``user_tasks``.

    A draft that is no longer open raises ``InvalidStateTransition`` (409).
    """
    await _load_draft(http_request, draft_id, request.user_id)
    await get_container(http_request).draft_service.reject(
        request.user_id, draft_id, request.reason
    )

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
        raise await _refused_edit(store, draft_id, request.user_id, _NOT_FOUND)
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
        raise await _refused_edit(store, draft_id, user_id, "Draft or task not found")

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

    updated = await asyncio.to_thread(store.replace_tasks, draft_id, request.user_id, reordered)
    if updated is None:
        raise await _refused_edit(store, draft_id, request.user_id, _NOT_FOUND)

    # TODO: re-solve with OR-Tools after a rearrange — the stored start times
    # are the solver's and no longer match the new order.
    return {
        "status": "rearranged",
        "draft_id": draft_id,
        "task_count": len(reordered),
        "needs_resolve": True,
    }


@router.post("/{draft_id}/chat", summary="Modify draft via natural language (not implemented)")
async def chat_modify_draft(draft_id: str, request: DraftChatRequest, http_request: Request):
    """501 — natural-language draft modification has no implementation.

    The handler used to dispatch to ``app.state.schedule_modifier`` and, when
    that was absent, answer ``{"status": "modified"}`` anyway. Nothing in the
    codebase ever assigns ``schedule_modifier``, so "modified" was the only
    reachable reply and it was always false: the draft came back unchanged
    while the caller was told the edit had landed.

    Nothing calls this today (``jarvis-frontend/lib/api.ts`` uses
    accept/reject/edit/rearrange/delete), so saying so plainly costs no caller.
    The conversational path to editing a plan is the orchestrator's negotiation
    flow at /api/v1/chat/v2/stream.
    """
    raise HTTPException(status_code=501, detail="draft chat modification not implemented")
