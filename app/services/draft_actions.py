"""Draft accept/resolve logic shared by the orchestrator node and the REST API.

Two callers act on the same drafts and must behave identically:

* ``app/orchestrator/graph.py::handle_draft_action`` — the conversational
  "accept"/"reject" turn inside the LangGraph orchestrator.
* ``app/api/v1/endpoints/drafts.py`` — ``POST /api/v1/drafts/{id}/accept``,
  which the frontend calls from the review UI.

Accepting is the *only* moment anything writes to ``user_tasks``: the planning
sub-graph proposes a draft and persists nothing. Accepting is
``DraftService.accept``, one atomic write. Both callers hand these functions the
composition root's ``DraftService`` (and its ``DraftPlanner``, for a draft some of
whose times have gone by); nothing here looks a collaborator up.

Every ``DraftService`` call takes ``user_id`` and the repository filters on it, so a
draft belonging to somebody else simply reads as absent (house IDOR rule).
"""

from dataclasses import dataclass
from typing import Optional

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain import errors
from app.domain.clock import utc_now
from app.domain.entities import DraftSchedule, DraftStatus
from app.domain.ports import DraftPlanner
from app.services.draft_service import DraftService, check_version, slot_starts
from app.services.task_service import as_aware


async def resolve_draft(
    drafts: DraftService, user_id: str, draft_id: Optional[str] = None
) -> Optional[DraftSchedule]:
    """Find the draft this turn is about: the named one, else the newest open one.

    ``draft_id`` is authoritative when the caller has one — orphaned open drafts
    can accumulate (a re-plan proposes a second draft without resolving the first),
    so the newest open one alone could act on the wrong schedule. It stays as the
    fallback for turns resumed without a draft_id in state.
    """
    return await drafts.open_draft(user_id, draft_id)


# What the user is told when an accept moved times that had gone by, or could not.
MOVED_ON_ACCEPT = "A few of those times had already gone by, so I moved them — it's locked in now."
NO_ROOM_ON_ACCEPT = (
    "A few of those times had already gone by, and the rest no longer fits around now, so"
    " I haven't locked it in. The draft is still open: tell me what to drop or move."
)


@dataclass(frozen=True)
class AcceptOutcome:
    """What accepting a draft did: tasks written, and whether passed times were moved."""

    written: int
    moved: bool = False


async def accept_on_time(
    drafts: DraftService,
    planner: DraftPlanner,
    user_id: str,
    draft_id: str,
    *,
    expected_version: Optional[str] = None,
) -> AcceptOutcome:
    """Accept the draft, never with a slot that has already begun (both accept paths).

    No slot begun: ``DraftService.accept`` as drafted. Some begun: the draft's own tasks
    are re-solved from now first (``DraftPlanner.reslot_from_now``: every rule a plan
    keeps, on the user's clock, leaning on their learned focus), the new times put on
    the draft and the draft accepted, ``moved``. Written as drafted, the passed slots would be set
    aside by the next sweep as if the user had, feeding PEARL and the focus profile
    evidence that never happened. Every slot begun: ``DraftOutdatedError``, as before.

    ``expected_version``: the draft's version the client read. If the draft has changed
    since, StaleVersionError and nothing is written; None compares nothing.

    Raises ScheduleInfeasibleError (NO_ROOM_ON_ACCEPT) when the re-solve finds no room,
    and LLMUnavailableError when the user's habits could not be read for it: either
    way nothing is accepted and the draft stays open. Otherwise as
    ``DraftService.accept``.
    """
    draft = await drafts.get(user_id, draft_id)
    draft.transition(DraftStatus.ACCEPTED)  # a closed draft fails fast, as accept does
    check_version(draft, expected_version)
    starts = slot_starts(draft)
    now = as_aware(utc_now())
    begun = [task for task, start in starts.items() if start <= now]
    if not begun or len(begun) == len(starts):
        written = await drafts.accept(user_id, draft_id, expected_version=expected_version)
        return AcceptOutcome(written=written)
    schedule = await planner.reslot_from_now(user_id, draft)
    if schedule is None:
        raise errors.ScheduleInfeasibleError(NO_ROOM_ON_ACCEPT, details={"draft_id": draft_id})
    await drafts.reschedule(user_id, draft_id, schedule)
    return AcceptOutcome(written=await drafts.accept(user_id, draft_id), moved=True)


async def accept_draft_on_time(
    drafts: DraftService, planner: DraftPlanner, user_id: str, draft: DraftSchedule
) -> Optional[AcceptOutcome]:
    """Accept the draft from chat: write its tasks to ``user_tasks`` and mark it accepted,
    atomically, saying whether passed times were moved (``accept_on_time``).

    Returns:
        ``written > 0`` — that many tasks were written and the draft is now 'accepted'.
        ``written == 0`` — the draft held nothing schedulable. Nothing was written and
        the status is untouched; there is nothing here to accept.
        ``None`` — the write did not land. The draft is still open, so
        ``resolve_draft`` can find it and the user can retry; the caller must not
        report success.

    Raises as ``accept_on_time``: InvalidStateTransition when the draft is no longer
    open (nothing written; it must not be re-persisted), NotFoundError when the user has
    no such draft, ConflictError when a task id is already held by one of the user's
    finished tasks (nothing written, and a retry cannot succeed).
    """
    try:
        return await accept_on_time(drafts, planner, user_id, draft.id)
    except errors.ValidationError:
        logger.warning(f"Draft {draft.id} holds no persistable tasks — nothing accepted")
        return AcceptOutcome(written=0)
    except errors.PersistenceError:
        logger.error(f"Draft {draft.id} accept did not land — left open for retry")
        return None


async def reject_draft(
    drafts: DraftService, user_id: str, draft_id: str, reason: Optional[str]
) -> None:
    """Mark the draft REJECTED, keeping ``reason`` (the signal PEARL learns from).

    Returns only when this call rejected it. Raises ``InvalidStateTransition`` when
    the draft is no longer open — accepted from the review UI, say — and
    ``NotFoundError`` when the user has no such draft, so no caller can report a
    discard that did not happen.
    """
    await drafts.reject(user_id, draft_id, reason)
