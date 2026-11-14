# app/api/v1/endpoints/drafts.py
"""Draft review/accept/reject endpoints.

Routing and parsing only. Every handler goes through the composition root's
``DraftService`` (``get_container``) — the same service the orchestrator's
``draft_action`` node uses, reached through ``app.services.draft_actions``.

A draft that is no longer open (already accepted, rejected or expired) cannot be
accepted, rejected, edited or discarded: the draft state machine raises
``InvalidStateTransition`` before anything is written, and the global handler answers
409 in the error envelope.

Every ``DraftService`` call takes ``user_id`` and the repository filters on it, so a
draft belonging to somebody else reads as absent and answers 404 — never 403, which
would confirm the row exists.
"""

import logging

from fastapi import APIRouter, Query, Request

from app.api.deps import container_of
from app.domain import errors
from app.domain.entities import DraftSchedule
from app.schemas.draft import (
    DraftAcceptRequest,
    DraftAcceptResponse,
    DraftDiscardResponse,
    DraftEditResponse,
    DraftModifyRequest,
    DraftRearrangeRequest,
    DraftRearrangeResponse,
    DraftRejectRequest,
    DraftRejectResponse,
    DraftScheduleView,
    DraftStateResponse,
    DraftTaskEdit,
)
from app.services.draft_actions import MOVED_ON_ACCEPT, accept_on_time
from app.services.draft_service import DraftService
from app.services.memory.memory_service import MemoryCandidate
from app.services.planning.task_rows import draft_slots

logger = logging.getLogger(__name__)

router = APIRouter()


def _drafts(request: Request) -> DraftService:
    """The composition root's ``DraftService``; 503 when the app has no container."""
    container = container_of(request)
    if container is None:
        raise errors.PersistenceError("Drafts are not available right now.")
    return container.draft_service


def _state_view(draft: DraftSchedule) -> DraftStateResponse:
    """A draft as the review UI reads it, with its solved slots' times."""
    slots = draft_slots(draft.tasks, draft.schedule, draft.horizon_start)
    return DraftStateResponse(
        draft_id=draft.id,
        user_id=draft.user_id,
        goal_id=draft.goal_id,
        tasks=draft.tasks,
        horizon_start=draft.horizon_start,
        status=draft.status,
        rejection_reason=draft.rejection_reason,
        created_at=draft.created_at.isoformat() if draft.created_at else None,
        schedule=(
            DraftScheduleView(horizon_start=draft.horizon_start, schedule=slots)
            if slots
            else None
        ),
        version=draft.version,
    )


@router.get(
    "/{draft_id}",
    response_model=DraftStateResponse,
    summary="Get draft state",
)
async def get_draft(draft_id: str, user_id: str, http_request: Request) -> DraftStateResponse:
    """Retrieve the current state of a draft for user review, with its solved slots' times."""
    return _state_view(await _drafts(http_request).get(user_id, draft_id))


@router.post(
    "/{draft_id}/accept",
    response_model=DraftAcceptResponse,
    summary="Accept a draft and persist its tasks",
)
async def accept_draft(
    draft_id: str, request: DraftAcceptRequest, http_request: Request
) -> DraftAcceptResponse:
    """Accept the draft: write its tasks to ``user_tasks`` and mark it accepted.

    This is the only REST path that commits a proposed schedule — the planning
    flow deliberately persists nothing until the user says yes. A second accept
    answers 409 and writes nothing, as does a plan whose every slot has gone by
    (``DraftOutdatedError``) and, when ``expected_version`` is given, a draft changed
    since that read (``StaleVersionError``, with the latest draft in its details). A
    plan some of whose slots have gone by is re-solved from now and then accepted
    (``moved``); when the rest no longer fits, 422 (``ScheduleInfeasibleError``) and the
    draft stays open.
    """
    drafts = _drafts(http_request)
    planner = container_of(http_request).planner
    try:
        # The chat accept's path too: times already gone by are moved first.
        outcome = await accept_on_time(
            drafts, planner, request.user_id, draft_id, expected_version=request.expected_version
        )
    except errors.StaleVersionError as exc:
        # "Here is the latest": the draft as it is now, read the way GET reads it.
        latest = _state_view(await drafts.get(request.user_id, draft_id))
        raise errors.StaleVersionError(
            exc.message, details={**(exc.details or {}), "draft": latest.model_dump(mode="json")}
        ) from exc
    except errors.ValidationError:
        # Nothing in the draft can be scheduled; nothing was written.
        return DraftAcceptResponse(status="empty", draft_id=draft_id, task_count=0)
    except errors.PersistenceError as exc:
        # The tasks did not land, so the draft is still open and the client
        # may retry. Answering 200 "accepted" here is how a user ends up with
        # an empty schedule and no idea why.
        raise errors.PersistenceError(
            "The schedule could not be saved. The draft is still open; try again."
        ) from exc
    return DraftAcceptResponse(
        status="accepted",
        draft_id=draft_id,
        task_count=outcome.written,
        moved=outcome.moved,
        message=MOVED_ON_ACCEPT if outcome.moved else None,
    )


@router.post("/{draft_id}/reject", response_model=DraftRejectResponse, summary="Reject a draft")
async def reject_draft(
    draft_id: str, request: DraftRejectRequest, http_request: Request
) -> DraftRejectResponse:
    """Reject the draft. Nothing is persisted to ``user_tasks``.

    A draft that is no longer open raises ``InvalidStateTransition`` (409).
    """
    await _drafts(http_request).reject(request.user_id, draft_id, request.reason)

    # The reason is behavioural signal — it is why the next plan should differ — and
    # the user's own words. The draft is rejected whether or not it is remembered.
    reason = (request.reason or "").strip()
    if reason:
        try:
            await container_of(http_request).memory_service.remember(
                request.user_id,
                MemoryCandidate(
                    memory_type="feedback",
                    content=f"Feedback on a proposed schedule: {reason}"[:2000],
                ),
                origin="draft_rejection",
            )
        except Exception as exc:  # the rejection stands whatever happens here
            logger.warning(
                "The reason for rejecting draft %s was not remembered (%s)",
                draft_id, type(exc).__name__, exc_info=exc,
            )

    return DraftRejectResponse(status="rejected", draft_id=draft_id)


@router.post(
    "/{draft_id}/modify",
    response_model=DraftEditResponse,
    response_model_exclude={"task_id", "updated_draft"},
    summary="Replace a draft's task list",
)
async def modify_draft(
    draft_id: str, request: DraftModifyRequest, http_request: Request
) -> DraftEditResponse:
    """Replace the draft's tasks wholesale.

    Only ``component == "tasks"`` is meaningful: draft_schedules stores a task
    array, and habits/action_items are no longer part of a draft at all.
    """
    if request.component != "tasks":
        raise errors.ValidationError(
            "Only the 'tasks' component is modifiable on a draft schedule.",
            details={"component": request.component},
        )
    if not isinstance(request.data, list):
        raise errors.ValidationError("'data' must be a list of tasks.")

    await _drafts(http_request).edit_tasks(request.user_id, draft_id, request.data)
    return DraftEditResponse(status="modified", component="tasks", draft_id=draft_id)


@router.delete("/{draft_id}", response_model=DraftDiscardResponse, summary="Discard a draft")
async def discard_draft(draft_id: str, user_id: str, http_request: Request) -> DraftDiscardResponse:
    """Discard an open draft (the user decided not to proceed): it expires, its row stays."""
    await _drafts(http_request).discard(user_id, draft_id)
    return DraftDiscardResponse(status="discarded", draft_id=draft_id)


@router.patch(
    "/{draft_id}/tasks/{task_id}",
    response_model=DraftEditResponse,
    response_model_exclude={"component"},
    summary="Edit one task in a draft",
)
async def edit_draft_task(
    draft_id: str,
    task_id: str,
    edits: DraftTaskEdit,
    request: Request,
    user_id: str = Query(..., description="User ID for authorization"),
) -> DraftEditResponse:
    """Edit a single task in a draft (404 when the draft holds no such task)."""
    edits_dict = edits.model_dump(exclude_none=True)
    if not edits_dict:
        raise errors.ValidationError("No edits provided.")

    draft = await _drafts(request).edit_task(user_id, draft_id, task_id, edits_dict)
    return DraftEditResponse(
        status="modified",
        draft_id=draft_id,
        task_id=task_id,
        updated_draft=draft.model_dump(mode="json"),
    )


@router.post(
    "/{draft_id}/rearrange",
    response_model=DraftRearrangeResponse,
    summary="Rearrange task order in draft",
)
async def rearrange_draft(
    draft_id: str, request: DraftRearrangeRequest, http_request: Request
) -> DraftRearrangeResponse:
    """Reorder tasks within a draft according to the user-specified order."""
    drafts = _drafts(http_request)
    draft = await drafts.get(request.user_id, draft_id)

    by_id = {t["task_id"]: t for t in draft.tasks if isinstance(t, dict) and t.get("task_id")}

    # Requested ids first, in order; anything unmentioned keeps its relative
    # place at the back rather than being silently dropped.
    reordered = [by_id.pop(tid) for tid in request.task_order if tid in by_id]
    reordered.extend(by_id.values())

    await drafts.edit_tasks(request.user_id, draft_id, reordered)

    # TODO: re-solve with OR-Tools after a rearrange — the stored start times
    # are the solver's and no longer match the new order.
    return DraftRearrangeResponse(
        status="rearranged",
        draft_id=draft_id,
        task_count=len(reordered),
        needs_resolve=True,
    )
