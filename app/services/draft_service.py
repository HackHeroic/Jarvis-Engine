"""Draft lifecycle: proposing, editing, accepting, rejecting and discarding a draft.

Every draft is stored and read through ``DraftRepository``, and every status change is a
move of the draft state machine (``DraftSchedule.transition``): a closed draft is refused
before anything is written.

Accepting a draft is the only write path to the user's tasks. It is one
repository call that writes the tasks and marks the draft ACCEPTED together, or
does neither. So a second accept (a double-click, or a REST accept followed by
"looks good" in chat) finds the draft closed and writes nothing, and a failed
write leaves the draft open for a retry.

Once the accept has committed, each task it gave a slot is recorded in the outcome
history: SCHEDULED the first time, RESCHEDULED when the slot moved.

An accept may name the ``version`` of the draft the client read; if the draft changed
since (an edit, a re-solve), nothing is written (``StaleVersionError``).

A plan whose every slot has already begun is never accepted (``DraftOutdatedError``):
it would only hand the slot sweep tasks to set aside, as if the user had. ``currency``
says how far a drafted re-plan has fallen behind (a slot begun, a task finished since),
for a caller that can draft it again instead.
"""

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.domain.entities import (
    DraftSchedule,
    DraftStatus,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from app.domain.errors import (
    DraftOutdatedError,
    NotFoundError,
    StaleVersionError,
    ValidationError,
)
from app.domain.events import DraftAccepted, EventBus
from app.domain.ports import DraftRepository, TaskEventRepository, TaskRepository
from app.services.planning.task_rows import build_task_rows, draft_slots, to_task_chunk
from app.services.task_service import as_aware, utc_now

logger = logging.getLogger(__name__)

# The events that give a task its slot; the latest one is the slot the task last had.
_SLOT_EVENTS = frozenset({TaskEventType.SCHEDULED, TaskEventType.RESCHEDULED})
# The statuses a re-plan places: still to do, or set aside and given a new slot.
_OPEN_TASK_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.MISSED})
_ALL_PASSED = (
    "Every time in this plan has already gone by, so I didn't put it in place."
    " Ask me for a fresh plan and I'll fit it around now."
)
PLAN_CHANGED = "This plan changed since you looked — here is the latest."


def check_version(draft: DraftSchedule, expected: str | None) -> None:
    """Refuse a draft that is no longer the one the client read (``StaleVersionError``).

    ``expected`` None: the caller named no version, so there is nothing to compare.
    """
    if expected is not None and expected != draft.version:
        raise StaleVersionError(
            PLAN_CHANGED,
            details={"draft_id": draft.id, "expected_version": expected, "version": draft.version},
        )


@dataclass(frozen=True)
class DraftCurrency:
    """How a drafted re-plan of the user's own tasks stands now.

    ``started``: its tasks whose slot has already begun. ``not_open``: its tasks the
    user no longer has pending or set aside (done, skipped, or gone since).
    ``set_aside``: its tasks set aside now, which a fresh draft re-slots.
    """

    started: frozenset[str]
    not_open: frozenset[str]
    set_aside: frozenset[str]

    @property
    def current(self) -> bool:
        """Whether accepting it now would put in place exactly what it says."""
        return not self.started and not self.not_open


class DraftService:
    """Moves drafts through their lifecycle and announces accepted ones."""

    def __init__(
        self,
        drafts: DraftRepository,
        events: EventBus,
        *,
        task_events: TaskEventRepository | None = None,
        tasks: TaskRepository | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._drafts = drafts
        self._events = events
        self._task_events = task_events
        self._tasks = tasks
        self._clock = clock

    async def propose(
        self,
        user_id: str,
        *,
        tasks: list[dict],
        horizon_start: str,
        goal_id: str | None,
        schedule: dict | None,
    ) -> DraftSchedule:
        """Store a new PENDING draft for the user to review; nothing reaches their tasks."""
        return await self._drafts.create(
            user_id, tasks=tasks, horizon_start=horizon_start, goal_id=goal_id, schedule=schedule
        )

    async def get(self, user_id: str, draft_id: str) -> DraftSchedule:
        """The user's draft; NotFoundError if they have no such draft."""
        draft = await self._drafts.get(user_id, draft_id)
        if draft is None:
            raise NotFoundError("That draft does not exist.", details={"draft_id": draft_id})
        return draft

    async def open_draft(
        self, user_id: str, draft_id: str | None = None
    ) -> DraftSchedule | None:
        """The draft a turn is about: the named one (whatever its status), else the user's
        newest open one; None when there is neither.

        The named draft wins: orphaned open drafts can pile up (a re-plan proposes a
        second without resolving the first), so the newest open one is only the fallback.
        """
        if draft_id:
            draft = await self._drafts.get(user_id, draft_id)
            if draft is not None:
                return draft
        return await self._drafts.get_open(user_id)

    async def edit_tasks(self, user_id: str, draft_id: str, tasks: list[dict]) -> DraftSchedule:
        """Replace the draft's task list and mark it MODIFIED; return the draft as stored.

        A closed draft is refused by the state machine before anything is written
        (InvalidStateTransition): an edit must never reopen an accepted draft, or a second
        accept would write its tasks again. NotFoundError if the user has no such draft.
        """
        draft = await self.get(user_id, draft_id)
        draft.transition(DraftStatus.MODIFIED)
        await self._drafts.replace_tasks(user_id, draft_id, tasks)
        return await self._drafts.set_status(user_id, draft_id, DraftStatus.MODIFIED)

    async def edit_task(
        self, user_id: str, draft_id: str, task_id: str, edits: dict
    ) -> DraftSchedule:
        """Apply ``edits`` (its non-None values) to one task of the draft, as ``edit_tasks``.

        NotFoundError, writing nothing, when the draft holds no task ``task_id``.
        """
        draft = await self.get(user_id, draft_id)
        draft.transition(DraftStatus.MODIFIED)  # a closed draft first: 409 before 404
        tasks = copy.deepcopy(draft.tasks)
        task = next((t for t in tasks if t.get("task_id") == task_id), None)
        if task is None:
            raise NotFoundError(
                "That task is not in this draft.",
                details={"draft_id": draft_id, "task_id": task_id},
            )
        task.update({key: value for key, value in edits.items() if value is not None})
        return await self.edit_tasks(user_id, draft_id, tasks)

    async def discard(self, user_id: str, draft_id: str) -> DraftSchedule:
        """The user set the draft aside: it becomes EXPIRED and its row stays.

        InvalidStateTransition if it is already closed, NotFoundError if absent.
        """
        return await self._drafts.set_status(user_id, draft_id, DraftStatus.EXPIRED)

    async def accept(
        self, user_id: str, draft_id: str, *, expected_version: str | None = None
    ) -> int:
        """Write the draft's tasks and mark it ACCEPTED, atomically; return tasks written.

        Raises NotFoundError if the user has no such draft, InvalidStateTransition
        if it is no longer open (accepted, rejected, expired...), StaleVersionError if
        ``expected_version`` is given and the draft has changed since it was read,
        ValidationError if it holds no task that can be scheduled, DraftOutdatedError
        if every one of its slots has already begun (a plan only of the past; nothing is
        written and the draft stays as it was), and ConflictError if one of its task ids
        is already held by a task the user finished (or repeats).
        """
        draft = await self.get(user_id, draft_id)
        # Fails fast on a closed draft. accept_atomic checks again under its lock,
        # and that second check is what stops a concurrent accept.
        draft.transition(DraftStatus.ACCEPTED)
        check_version(draft, expected_version)

        chunks = _chunks_of(draft)
        if not chunks:
            raise ValidationError("Draft holds nothing schedulable.", details={"draft_id": draft_id})
        starts = slot_starts(draft)
        if starts and all(start <= as_aware(self._clock()) for start in starts.values()):
            raise DraftOutdatedError(_ALL_PASSED, details={"draft_id": draft_id})
        _, rows = build_task_rows(
            user_id, chunks, schedule=slot_map(draft.schedule), horizon_start=draft.horizon_start
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

    async def reschedule(self, user_id: str, draft_id: str, schedule: dict) -> DraftSchedule:
        """Put a re-solved schedule on an open draft (InvalidStateTransition if closed)."""
        return await self._drafts.replace_schedule(user_id, draft_id, schedule)

    async def reject(
        self, user_id: str, draft_id: str, reason: str | None = None
    ) -> DraftSchedule:
        """Mark the draft REJECTED, keeping the reason; raise InvalidStateTransition if closed."""
        return await self._drafts.set_status(user_id, draft_id, DraftStatus.REJECTED, reason=reason)

    async def expire(self, user_id: str, draft_id: str) -> DraftSchedule:
        """Mark an open draft EXPIRED (it no longer holds); InvalidStateTransition if closed."""
        return await self._drafts.set_status(user_id, draft_id, DraftStatus.EXPIRED)

    async def currency(self, user_id: str, draft: DraftSchedule) -> DraftCurrency:
        """How the drafted re-plan stands now: slots begun, tasks no longer open.

        For a draft of the user's own tasks (the morning brief's, a rule's redraft):
        a task id the user does not hold pending or set aside counts as not open. One
        bounded read of the user's open tasks (their newest TASK_LIST_LIMIT).
        """
        if self._tasks is None:
            raise RuntimeError("DraftService needs a TaskRepository to read a draft's currency.")
        now = as_aware(self._clock())
        started = frozenset(t for t, start in slot_starts(draft).items() if start <= now)
        statuses = {
            task.task_id: task.status
            for task in await self._tasks.list_by_status(user_id, _OPEN_TASK_STATUSES)
        }
        drafted = {raw.get("task_id") for raw in draft.tasks} - {None}
        return DraftCurrency(
            started=started,
            not_open=frozenset(t for t in drafted if t not in statuses),
            set_aside=frozenset(t for t in drafted if statuses.get(t) is TaskStatus.MISSED),
        )

    async def _record_slots(
        self, user_id: str, draft_id: str, rows: list[dict], now: datetime
    ) -> None:
        """Store a SCHEDULED or RESCHEDULED event for each written row that has a slot.

        One read of those tasks' slot history (narrowed to their ids, so a read cap
        cannot drop a task's latest slot), then one bulk write. Best effort: the accept
        has already committed, so nothing here may fail it or undo it. A failure is one
        warning, and the next accept compares against whatever history was stored.
        """
        if self._task_events is None:
            return
        slotted = [row for row in rows if row.get("scheduled_start") is not None]
        if not slotted:
            return
        try:
            history = await self._task_events.list_for_user(
                user_id,
                types=set(_SLOT_EVENTS),
                task_ids={row["task_id"] for row in slotted},
            )
            latest = {event.task_id: event for event in history}  # oldest first: last wins
            events = []
            for row in slotted:
                event = _slot_event(user_id, row, latest.get(row["task_id"]), now)
                if event is not None:
                    events.append(event)
            if events:
                await self._task_events.append_many(user_id, events)
        except Exception as exc:  # the accept stands whatever the event store does
            logger.warning(
                "Draft %s accepted; its task events could not be recorded (%s)",
                draft_id, type(exc).__name__,
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


def slot_map(schedule: dict | None) -> dict | None:
    """The draft's task_id -> {start_min, end_min} map.

    A schedule stored as a whole ScheduleResponse payload keeps the map one
    level down, under "schedule".
    """
    if isinstance(schedule, dict) and "schedule" in schedule:
        return schedule.get("schedule")
    return schedule


def slot_starts(draft: DraftSchedule) -> dict[str, datetime]:
    """Each placed task's slot start as an instant (``horizon_start`` + its minutes)."""
    slots = draft_slots(draft.tasks, slot_map(draft.schedule), draft.horizon_start) or {}
    return {task_id: datetime.fromisoformat(slot["start"]) for task_id, slot in slots.items()}
