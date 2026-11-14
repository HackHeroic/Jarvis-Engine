"""Draft accept/resolve logic shared by the orchestrator node and the REST API.

Two callers act on the same draft rows and must behave identically:

* ``app/orchestrator/graph.py::handle_draft_action`` — the conversational
  "accept"/"reject" turn inside the LangGraph orchestrator.
* ``app/api/v1/endpoints/drafts.py`` — ``POST /api/v1/drafts/{id}/accept``,
  which the frontend calls from the review UI.

Accepting is the *only* moment v2 writes to ``user_tasks``: the planning
sub-graph proposes a draft and persists nothing. Accepting is
``DraftService.accept``, one atomic write, on the composition root's service:
the REST endpoint takes it from the container, the orchestrator node reaches it
through ``accept_draft_and_persist`` here.

Every store call takes ``user_id`` — ``DraftStore`` filters on it, so a draft
belonging to somebody else simply reads as absent (house IDOR rule).
"""

import asyncio
from typing import Any, Optional

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.core.runtime import registered_container
from app.domain import errors
from app.domain.events import NullEventBus
from app.services.draft_service import DraftService
# Moved to app.services.planning.task_rows; planning_graph still imports it from here.
from app.services.planning.task_rows import to_task_chunk  # noqa: F401


def draft_id_of(row: Any) -> Optional[str]:
    """Read the draft id out of a ``draft_schedules`` row.

    The table's primary key column is ``id`` (migration 20260329000002) and
    ``DraftStore.create_draft`` echoes the inserted row back verbatim — the
    locally generated uuid is *written* as ``id``, never returned as
    ``draft_id``. Accept both names so callers work against the live store and
    against any wrapper that hands back the friendlier one.
    """
    if not isinstance(row, dict):
        return None
    return row.get("draft_id") or row.get("id")


async def resolve_draft(
    draft_store: Any, user_id: str, draft_id: Optional[str] = None
) -> Optional[dict]:
    """Find the draft this turn is about: the named one, else the newest pending.

    ``draft_id`` is authoritative when the caller has one — orphaned pending
    rows can accumulate (a re-plan proposes a second draft without resolving the
    first), so ``get_pending_draft`` alone could act on the wrong schedule. It
    stays as the fallback for turns resumed without a draft_id in state.

    ``DraftStore`` is a synchronous Supabase client; ``to_thread`` keeps the
    round-trip off the event loop (same pattern as planning_graph.create_draft).
    """
    if draft_store is None:
        return None
    if draft_id:
        row = await asyncio.to_thread(draft_store.get_draft, draft_id, user_id)
        if row:
            return row
    return await asyncio.to_thread(draft_store.get_pending_draft, user_id)


def _count_persisted(supabase: Any, user_id: str, plan_id: str) -> int:
    """How many rows carrying *this* ``plan_id`` are in ``user_tasks`` now.

    Used by the deprecated v1 ``/accept-schedule`` to prove its non-atomic
    ``_persist_fused_tasks`` write landed. Draft acceptance needs no re-read: its
    write is one transaction that returns the row count.

    Filtering on plan_id rather than task_id is the whole point. Fused chunk
    lists deliberately carry pre-existing pending tasks forward (``fuse_tasks``
    merges them into the draft), and ``_persist_fused_tasks`` builds its rows
    *before* deleting the old ones — so after a total write failure the stale
    rows are still there, wearing the same task_ids. Matching on those would
    read an outage as a success, flip the draft to accepted, and take the retry
    path away with it. A plan_id is minted per call, so only rows this call
    wrote can carry it.
    """
    result = (
        supabase.table("user_tasks")
        .select("task_id")
        .eq("user_id", user_id)
        .eq("plan_id", plan_id)
        .execute()
    )
    return len(result.data or [])


async def accept_draft_and_persist(
    draft_store: Any, user_id: str, draft: dict, supabase: Any
) -> Optional[int]:
    """Accept the draft: write its tasks to ``user_tasks`` and mark it accepted, atomically.

    Returns:
        ``n > 0`` — that many tasks were written and the draft is now 'accepted'.
        ``0`` — the draft held nothing schedulable. Nothing was written and the
        status is untouched; there is nothing here to accept.
        ``None`` — the write did not land. The draft is still open, so
        ``get_pending_draft`` can find it and the user can retry; the caller
        must not report success.

    Raises:
        InvalidStateTransition: the draft is no longer open (already accepted,
            rejected or expired). Nothing was written; it must not be re-persisted.
        NotFoundError: the user has no such draft.
        ConflictError: a task id in the draft is already held by one of the
            user's finished tasks, or repeated. Nothing was written, and a retry
            cannot succeed.

    ``DraftService.accept`` writes the tasks and flips the status in one
    transaction, so they land together or not at all. A second accept therefore
    finds the draft closed instead of writing every task again. ``draft_store``
    is unused; it stays in the signature for the callers.
    """
    service = _draft_service(supabase)
    if service is None:
        logger.error(f"Draft {draft_id_of(draft)} not accepted: no Supabase client to write through")
        return None
    try:
        return await service.accept(user_id, draft_id_of(draft))
    except errors.ValidationError:
        logger.warning(f"Draft {draft_id_of(draft)} holds no persistable tasks — nothing accepted")
        return 0
    except errors.PersistenceError:
        logger.error(f"Draft {draft_id_of(draft)} accept did not land — left open for retry")
        return None


async def reject_draft(
    draft_store: Any, user_id: str, draft_id: str, reason: Optional[str], supabase: Any
) -> None:
    """Mark the draft REJECTED, keeping ``reason`` (the signal PEARL learns from).

    Returns only when this call rejected it. Raises ``InvalidStateTransition`` when
    the draft is no longer open — accepted from the review UI, say — and
    ``NotFoundError`` when the user has no such draft, so no caller can report a
    discard that did not happen. Goes through ``DraftService``; with no service to
    go through (no container, no Supabase client) the store rejects, and its
    ``False`` (absent or closed) raises the same way.
    """
    service = _draft_service(supabase)
    if service is not None:
        await service.reject(user_id, draft_id, reason)
        return
    if not await asyncio.to_thread(draft_store.reject_draft, draft_id, user_id, reason):
        raise errors.InvalidStateTransition(
            "That draft is no longer open.", details={"draft_id": draft_id}
        )


def task_repository(supabase: Any) -> Any:
    """The composition root's ``TaskRepository``; without a container, one over ``supabase``.

    The same no-container fallback as ``_draft_service`` (tests, scripts), kept in
    this one shim: nothing else outside the composition root builds a repository.
    """
    container = registered_container()
    if container is not None:
        return container.repos.tasks
    from app.infrastructure.persistence.supabase_repos import SupabaseTaskRepository

    return SupabaseTaskRepository(supabase)


def _draft_service(supabase: Any) -> Optional[DraftService]:
    """The composition root's ``DraftService``, whose bus delivers ``DraftAccepted``.

    With no container registered (tests, scripts) one is built here over
    ``supabase``, with nobody listening; ``None`` when there is no client either.
    """
    container = registered_container()
    if container is not None:
        return container.draft_service
    if supabase is None:
        return None
    # The no-container fallback: the one place outside the composition root that
    # builds a repository itself.
    from app.infrastructure.persistence.supabase_repos import (
        SupabaseDraftRepository,
        SupabaseTaskEventRepository,
        SupabaseTaskRepository,
    )

    return DraftService(
        SupabaseDraftRepository(supabase),
        NullEventBus(),
        task_events=SupabaseTaskEventRepository(supabase),
        tasks=SupabaseTaskRepository(supabase),
    )
