"""Draft lifecycle: accept and reject are moves of the draft state machine.

Accepting a draft is the only write path to the user's tasks. It is one
repository call that writes the tasks and marks the draft ACCEPTED together, or
does neither. So a second accept (a double-click, or a REST accept followed by
"looks good" in chat) finds the draft closed and writes nothing, and a failed
write leaves the draft open for a retry.

Once the accept has committed, each task it gave a slot is recorded in the outcome
history: SCHEDULED the first time, RESCHEDULED when the slot moved.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from app.domain.entities import DraftSchedule, DraftStatus, TaskEvent, TaskEventType
from app.domain.errors import NotFoundError, ValidationError
from app.domain.events import DraftAccepted, EventBus
from app.domain.ports import DraftRepository, TaskEventRepository
from app.services.planning.task_rows import build_task_rows, to_task_chunk
from app.services.task_service import utc_now

logger = logging.getLogger(__name__)

# The events that give a task its slot; the latest one is the slot the task last had.
_SLOT_EVENTS = frozenset({TaskEventType.SCHEDULED, TaskEventType.RESCHEDULED})


class DraftService:
    """Moves drafts through their lifecycle and announces accepted ones."""

    def __init__(
        self,
        drafts: DraftRepository,
        events: EventBus,
        *,
        task_events: TaskEventRepository | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._drafts = drafts
        self._events = events
        self._task_events = task_events
        self._clock = clock

    async def accept(self, user_id: str, draft_id: str) -> int:
        """Write the draft's tasks and mark it ACCEPTED, atomically; return tasks written.

        Raises NotFoundError if the user has no such draft, InvalidStateTransition
        if it is no longer open (accepted, rejected, expired...), ValidationError
        if it holds no task that can be scheduled, and ConflictError if one of its
        task ids is already held by a task the user finished (or repeats).
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
        now = self._clock()
        await self._record_slots(user_id, draft_id, rows, now)

        await self._events.publish(
            DraftAccepted(
                occurred_at=now,
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

    async def _record_slots(
        self, user_id: str, draft_id: str, rows: list[dict], now: datetime
    ) -> None:
        """Store a SCHEDULED or RESCHEDULED event for each written row that has a slot.

        Best effort: the accept has already committed, so nothing here may fail it or
        undo it. A failure is logged as one warning per accept, and the next accept
        compares against whatever history was stored.
        """
        if self._task_events is None:
            return
        slotted = [row for row in rows if row.get("scheduled_start") is not None]
        if not slotted:
            return
        try:
            history = await self._task_events.list_for_user(user_id, types=set(_SLOT_EVENTS))
        except Exception as exc:  # the accept stands whatever the event store does
            logger.warning(
                "Draft %s accepted; its task event history could not be read (%s), "
                "so no slot was recorded",
                draft_id, type(exc).__name__,
            )
            return
        latest = {event.task_id: event for event in history}  # oldest first: the last wins

        failures: dict[str, int] = {}
        for row in slotted:
            try:
                event = _slot_event(user_id, row, latest.get(row["task_id"]), now)
                if event is not None:
                    await self._task_events.append(user_id, event)
            except Exception as exc:  # the accept stands whatever the event store does
                kind = type(exc).__name__
                failures[kind] = failures.get(kind, 0) + 1
        if failures:
            logger.warning(
                "Draft %s accepted; %d task event(s) could not be stored (%s)",
                draft_id,
                sum(failures.values()),
                ", ".join(f"{kind} x{count}" for kind, count in sorted(failures.items())),
            )


def _slot_event(
    user_id: str, row: dict, previous: TaskEvent | None, now: datetime
) -> TaskEvent | None:
    """The event a newly written slot calls for, given the task's latest slot event.

    No known slot: SCHEDULED. A different start: RESCHEDULED, with ``previous_start``.
    The same start: None, nothing happened to the task.
    """
    event = TaskEvent(
        user_id=user_id,
        task_id=row["task_id"],
        event_type=TaskEventType.SCHEDULED,
        occurred_at=now,
        scheduled_start=row["scheduled_start"],
        scheduled_end=row.get("scheduled_end"),
        metadata={
            "title": row.get("title"),
            "goal_id": row.get("goal_id"),
            "deadline_hint": row.get("deadline_hint"),
        },
    )
    known = previous.scheduled_start if previous is not None else None
    if known is None:
        return event
    if known == event.scheduled_start:
        return None
    return event.model_copy(
        update={
            "event_type": TaskEventType.RESCHEDULED,
            "metadata": {**event.metadata, "previous_start": known.isoformat()},
        }
    )


def _chunks_of(draft: DraftSchedule) -> list[dict]:
    """The draft's tasks, validated as TaskChunks; a task that cannot become one is skipped.

    Each keeps the goal_id the planner stamped on it. TaskChunk has no goal_id
    field, so validation alone would drop it and build_task_rows would have to
    guess the goal from the task_id.
    """
    chunks = []
    for index, raw in enumerate(draft.tasks):
        try:
            chunk = to_task_chunk(raw, index).model_dump()
        # ValueError covers pydantic's ValidationError; ArithmeticError covers int(inf).
        except (TypeError, ValueError, ArithmeticError):
            logger.warning(
                "Draft %s: skipping unparseable task %r", draft.id, raw.get("task_id")
            )
            continue
        chunk["goal_id"] = raw.get("goal_id")
        chunks.append(chunk)
    return chunks


def _slot_map(schedule: dict | None) -> dict | None:
    """The draft's task_id -> {start_min, end_min} map.

    A schedule stored as a whole ScheduleResponse payload keeps the map one
    level down, under "schedule".
    """
    if isinstance(schedule, dict) and "schedule" in schedule:
        return schedule.get("schedule")
    return schedule
