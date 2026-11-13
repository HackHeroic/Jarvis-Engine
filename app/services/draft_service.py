"""Draft lifecycle: accept and reject are moves of the draft state machine.

Accepting a draft is the only write path to the user's tasks. It is one
repository call that writes the tasks and marks the draft ACCEPTED together, or
does neither. So a second accept (a double-click, or a REST accept followed by
"looks good" in chat) finds the draft closed and writes nothing, and a failed
write leaves the draft open for a retry.
"""

import logging
from datetime import datetime, timezone

from app.domain.entities import DraftSchedule, DraftStatus
from app.domain.errors import NotFoundError, ValidationError
from app.domain.events import DraftAccepted, EventBus
from app.domain.ports import DraftRepository
from app.services.planning.task_rows import build_task_rows, to_task_chunk

logger = logging.getLogger(__name__)


class DraftService:
    """Moves drafts through their lifecycle and announces accepted ones."""

    def __init__(self, drafts: DraftRepository, events: EventBus) -> None:
        self._drafts = drafts
        self._events = events

    async def accept(self, user_id: str, draft_id: str) -> int:
        """Write the draft's tasks and mark it ACCEPTED, atomically; return tasks written.

        Raises NotFoundError if the user has no such draft, InvalidStateTransition
        if it is no longer open (accepted, rejected, expired...), and
        ValidationError if it holds no task that can be scheduled.
        """
        draft = await self._drafts.get(user_id, draft_id)
        if draft is None:
            raise NotFoundError("That draft does not exist.", details={"draft_id": draft_id})
        # Fails fast on a closed draft. accept_atomic checks again under its lock,
        # and that second check is what stops a concurrent accept.
        draft.transition(DraftStatus.ACCEPTED)

        chunks = _chunks_of(draft)
        if not chunks:
            raise ValidationError("Draft holds nothing schedulable.", details={"draft_id": draft_id})
        _, rows = build_task_rows(
            user_id, chunks, schedule=_slot_map(draft.schedule), horizon_start=draft.horizon_start
        )
        written = await self._drafts.accept_atomic(user_id, draft_id, rows)

        await self._events.publish(
            DraftAccepted(
                occurred_at=datetime.now(timezone.utc),
                user_id=user_id,
                draft_id=draft_id,
                task_count=written,
            )
        )
        return written

    async def reject(
        self, user_id: str, draft_id: str, reason: str | None = None
    ) -> DraftSchedule:
        """Mark the draft REJECTED, keeping the reason; raise InvalidStateTransition if closed."""
        return await self._drafts.set_status(user_id, draft_id, DraftStatus.REJECTED, reason=reason)


def _chunks_of(draft: DraftSchedule) -> list:
    """The draft's tasks as TaskChunks; a task that cannot become one is skipped."""
    chunks = []
    for index, raw in enumerate(draft.tasks):
        try:
            chunks.append(to_task_chunk(raw, index))
        except (TypeError, ValueError):  # ValueError covers pydantic's ValidationError
            logger.warning(
                "Draft %s: skipping unparseable task %r", draft.id, raw.get("task_id")
            )
    return chunks


def _slot_map(schedule: dict | None) -> dict | None:
    """The draft's task_id -> {start_min, end_min} map.

    A schedule stored as a whole ScheduleResponse payload keeps the map one
    level down, under "schedule".
    """
    if isinstance(schedule, dict) and "schedule" in schedule:
        return schedule.get("schedule")
    return schedule
