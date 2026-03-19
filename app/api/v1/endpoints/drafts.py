# app/api/v1/endpoints/drafts.py
"""Draft review/accept/reject endpoints."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from app.schemas.draft import (
    DraftAcceptRequest,
    DraftModifyRequest,
    DraftRejectRequest,
    DraftResponse,
    DraftComponentResponse,
)

router = APIRouter()


def _get_draft_store(request: Request):
    store = getattr(request.app.state, "draft_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Draft store not available")
    return store


@router.get(
    "/{draft_id}",
    response_model=DraftResponse,
    summary="Get draft state",
)
async def get_draft(draft_id: str, user_id: str, http_request: Request):
    """Retrieve current state of a draft for user review."""
    store = _get_draft_store(http_request)
    draft = store.get(draft_id, user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired")
    return DraftResponse(
        draft_id=draft.draft_id,
        user_id=draft.user_id,
        components={
            k: DraftComponentResponse(
                component_type=v.component_type,
                data=v.data,
                status=v.status,
            )
            for k, v in draft.components.items()
        },
        metadata=draft.metadata,
    )


@router.post(
    "/{draft_id}/accept",
    summary="Accept draft components",
)
async def accept_draft(
    draft_id: str, request: DraftAcceptRequest, http_request: Request
):
    """Accept specific components (or all) and persist to database."""
    store = _get_draft_store(http_request)
    draft = store.get(draft_id, request.user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired")

    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    components_to_accept = request.components
    if components_to_accept is None:
        components_to_accept = [
            k for k, v in draft.components.items() if v.status == "pending"
        ]

    accepted = []
    for key in components_to_accept:
        if key not in draft.components:
            continue
        comp = draft.components[key]
        if comp.status in ("rejected",):
            continue

        # Persist based on component type
        if key == "habits" and supabase:
            from app.services.extraction.behavioral_store import store_behavioral_constraint
            for habit in comp.data:
                if isinstance(habit, dict) and habit.get("raw_text"):
                    await store_behavioral_constraint(
                        raw_text=habit["raw_text"],
                        user_id=request.user_id,
                        supabase_client=supabase,
                    )

        elif key == "tasks" and supabase:
            import asyncio
            from app.api.v1.endpoints.reasoning import TaskChunk
            from app.services.analytical.control_policy import _persist_fused_tasks
            task_chunks = [TaskChunk(**t) for t in comp.data]
            # _persist_fused_tasks is sync — offload to thread to avoid blocking event loop
            await asyncio.to_thread(_persist_fused_tasks, request.user_id, task_chunks, supabase)

        elif key == "action_items" and supabase:
            for item in comp.data:
                try:
                    supabase.table("pending_action_items").insert({
                        "user_id": request.user_id,
                        "title": item.get("title", ""),
                        "summary": item.get("summary", ""),
                        "status": "accepted",
                    }).execute()
                except Exception as e:
                    print(f"[Drafts] Action item persist failed: {e}")

        store.accept_component(draft_id, request.user_id, key)
        accepted.append(key)

    return {"status": "ok", "accepted": accepted, "draft_id": draft_id}


@router.post(
    "/{draft_id}/reject",
    summary="Reject draft components",
)
async def reject_draft(
    draft_id: str, request: DraftRejectRequest, http_request: Request
):
    """Reject specific components (they won't be persisted)."""
    store = _get_draft_store(http_request)
    draft = store.get(draft_id, request.user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired")

    rejected = []
    for key in request.components:
        if store.reject_component(draft_id, request.user_id, key):
            rejected.append(key)

    return {"status": "ok", "rejected": rejected, "draft_id": draft_id}


@router.post(
    "/{draft_id}/modify",
    summary="Modify a draft component",
)
async def modify_draft(
    draft_id: str, request: DraftModifyRequest, http_request: Request
):
    """Update a specific component's data (e.g., edit a task, change a habit)."""
    store = _get_draft_store(http_request)
    if not store.update_component_data(
        draft_id, request.user_id, request.component, request.data
    ):
        raise HTTPException(status_code=404, detail="Draft or component not found")

    return {"status": "modified", "component": request.component, "draft_id": draft_id}


@router.delete(
    "/{draft_id}",
    summary="Discard a draft",
)
async def delete_draft(draft_id: str, user_id: str, http_request: Request):
    """Discard an entire draft (user decided not to proceed)."""
    store = _get_draft_store(http_request)
    if not store.delete(draft_id, user_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"status": "deleted", "draft_id": draft_id}
