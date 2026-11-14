"""Task outcomes: every Complete, Skip and miss becomes one append-only task event.

The outcome history (``task_events``) is what learning reads, so each outcome is
recorded once and only once. Every move is a compare-and-set on the task's status:
a double-click, or the slot sweep racing a late completion, lets exactly one writer
through, and the loser writes nothing. "Missed" is never asked about. It is inferred
when a slot has ended, the grace period has passed and no outcome was recorded.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import app.core.config as config
from app.domain import errors
from app.domain.entities import Task, TaskEvent, TaskEventType, TaskStatus
from app.domain.events import (
    DeadlineMissed,
    DomainEvent,
    EventBus,
    TaskCompleted,
    TaskMissed,
    TaskSkipped,
)
from app.domain.ports import Sweep, TaskEventRepository, TaskRepository

logger = logging.getLogger(__name__)

# A lost compare-and-set is retried against the status that won, up to this many tries.
_CAS_ATTEMPTS = 3

# Outcomes that settle a slot: completed, skipped, or missed (the slot passed). One
# recorded since the slot began keeps the sweep away; learning counts only these.
SLOT_OUTCOMES = frozenset({TaskEventType.COMPLETED, TaskEventType.SKIPPED, TaskEventType.MISSED})


def utc_now() -> datetime:
    """The current instant, in UTC."""
    return datetime.now(timezone.utc)


def as_aware(moment: datetime) -> datetime:
    """``moment`` as an aware instant. Stored instants are UTC, so a naive one is read so."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def latest_outcome_per_slot(events: Iterable[TaskEvent]) -> list[TaskEvent]:
    """The outcome each slot came to, oldest first: what learning counts.

    A slot is a task's ``(task_id, scheduled_start)``. It can have more than one
    outcome: the sweep marks it missed, then the user completes it after all. It
    counts once, as its latest outcome by ``occurred_at``. Events that are not
    outcomes are dropped. Outcomes with no ``scheduled_start`` are kept, one per task:
    the latest of them, as that task's one unscheduled slot. A reader that needs an
    hour skips them.
    """
    latest: dict[tuple[str, datetime | None], TaskEvent] = {}
    for event in sorted(events, key=lambda e: as_aware(e.occurred_at)):
        if event.event_type in SLOT_OUTCOMES:
            start = as_aware(event.scheduled_start) if event.scheduled_start is not None else None
            latest[(event.task_id, start)] = event
    return sorted(latest.values(), key=lambda e: as_aware(e.occurred_at))


@dataclass(frozen=True)
class _Move:
    """One legal outcome: the status it sets, the statuses it may start from, its event."""

    target: TaskStatus
    allowed_from: frozenset[TaskStatus]
    event_type: TaskEventType


_COMPLETE = _Move(
    TaskStatus.COMPLETED,
    # "I actually did that": a missed or skipped task can still be completed.
    frozenset({TaskStatus.PENDING, TaskStatus.MISSED, TaskStatus.SKIPPED}),
    TaskEventType.COMPLETED,
)
_SKIP = _Move(TaskStatus.SKIPPED, frozenset({TaskStatus.PENDING}), TaskEventType.SKIPPED)
_DEADLINE_MISS = _Move(
    TaskStatus.MISSED, frozenset({TaskStatus.PENDING}), TaskEventType.DEADLINE_MISSED
)
_SLOT_MISS = _Move(TaskStatus.MISSED, frozenset({TaskStatus.PENDING}), TaskEventType.MISSED)


class TaskService:
    """Records what happened to the user's tasks, one event per outcome.

    Each method returns the stored event, or ``None`` when nothing was written: the
    task was already in that state, or the move is not legal from where it is.
    """

    def __init__(
        self,
        tasks: TaskRepository,
        task_events: TaskEventRepository,
        bus: EventBus,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._tasks = tasks
        self._events = task_events
        self._bus = bus
        self._clock = clock

    async def complete(
        self,
        user_id: str,
        task_id: str,
        *,
        actual_duration_minutes: int | None = None,
        quality: int | None = None,
    ) -> TaskEvent | None:
        """Mark the task completed (from pending, missed or skipped); NotFoundError if absent."""
        task = await self._get(user_id, task_id)
        now = self._clock()
        return await self._record(
            task,
            _COMPLETE,
            now,
            {"actual_duration_minutes": actual_duration_minutes, "quality": quality},
            TaskCompleted(occurred_at=now, user_id=user_id, task_id=task_id),
        )

    async def skip(
        self, user_id: str, task_id: str, *, reason: str | None = None
    ) -> TaskEvent | None:
        """Mark a pending task skipped, keeping the reason; NotFoundError if absent."""
        task = await self._get(user_id, task_id)
        now = self._clock()
        return await self._record(
            task,
            _SKIP,
            now,
            {"reason": reason},
            TaskSkipped(occurred_at=now, user_id=user_id, task_id=task_id, reason=reason),
        )

    async def mark_deadline_missed(self, user_id: str, task_id: str) -> TaskEvent | None:
        """Set a pending task aside because its deadline passed; NotFoundError if absent."""
        task = await self._get(user_id, task_id)
        now = self._clock()
        return await self._record(
            task,
            _DEADLINE_MISS,
            now,
            {},
            DeadlineMissed(occurred_at=now, user_id=user_id, task_id=task_id),
        )

    async def sweep_passed_slots(self, user_id: str, now: datetime) -> list[TaskEvent]:
        """Mark missed each pending task whose slot passed with no outcome; return the events.

        A slot has passed once ``scheduled_end + SLOT_GRACE_MINUTES <= now``, compared as
        instants (never by date, so a slot just after the user's local midnight is no
        different). Slots that started up to ``SLOT_SWEEP_LOOKBACK_DAYS`` ago are
        considered. A completed, skipped or missed event recorded since the slot began
        leaves the task alone. A task that changes under the sweep is left for the next pass.
        """
        lookback = timedelta(days=config.SLOT_SWEEP_LOOKBACK_DAYS)
        grace = timedelta(minutes=config.SLOT_GRACE_MINUTES)
        passed = [
            task
            for task in await self._tasks.list_scheduled_between(user_id, now - lookback, now)
            if task.status == TaskStatus.PENDING
            and task.scheduled_end is not None
            and task.scheduled_end + grace <= now
        ]
        if not passed:
            return []

        outcomes = await self._events.list_for_user(
            user_id, since=min(task.scheduled_start for task in passed), types=set(SLOT_OUTCOMES)
        )
        latest_outcome = {event.task_id: event.occurred_at for event in outcomes}  # oldest first

        missed: list[TaskEvent] = []
        for task in passed:
            settled_at = latest_outcome.get(task.task_id)
            if settled_at is not None and settled_at >= task.scheduled_start:
                continue
            try:
                event = await self._record(
                    task,
                    _SLOT_MISS,
                    now,
                    {},
                    TaskMissed(occurred_at=now, user_id=user_id, task_id=task.task_id),
                )
            except (errors.NotFoundError, errors.StaleVersionError):
                logger.info("Task changed during the slot sweep; the next pass decides")
                continue
            if event is not None:
                missed.append(event)
        return missed

    async def _get(self, user_id: str, task_id: str) -> Task:
        task = await self._tasks.get(user_id, task_id)
        if task is None:
            raise errors.NotFoundError("That task does not exist.", details={"task_id": task_id})
        return task

    async def _move(self, task: Task, move: _Move) -> tuple[Task, TaskStatus] | None:
        """Compare-and-set ``task`` to ``move.target``.

        Returns the moved task and the status it moved from, or None if nothing
        changed. A lost race re-reads the task and tries again from the status that
        won, while the move is still legal from it.
        """
        attempts = 0
        while task.status != move.target and task.status in move.allowed_from:
            try:
                moved = await self._tasks.update_status(
                    task.user_id, task.task_id, move.target, expected=task.status
                )
                return moved, task.status
            except errors.StaleVersionError:
                attempts += 1
                if attempts >= _CAS_ATTEMPTS:
                    raise
                task = await self._get(task.user_id, task.task_id)
        return None

    async def _record(
        self,
        task: Task,
        move: _Move,
        now: datetime,
        details: dict,
        announcement: DomainEvent,
    ) -> TaskEvent | None:
        """Make the move; if it happened, store its event, then publish ``announcement``.

        The status and the event are two writes. If the event cannot be stored the
        status is set back before the error is re-raised, so the outcome is not lost:
        a retry finds the task where it was and records it.
        """
        result = await self._move(task, move)
        if result is None:
            return None
        moved, previous = result
        event = TaskEvent(
            user_id=moved.user_id,
            task_id=moved.task_id,
            event_type=move.event_type,
            occurred_at=now,
            scheduled_start=moved.scheduled_start,
            scheduled_end=moved.scheduled_end,
            metadata={
                "title": moved.title,
                "goal_id": moved.goal_id,
                "deadline_hint": moved.deadline_hint,
                **details,
            },
        )
        try:
            stored = await self._events.append(moved.user_id, event)
        except errors.PersistenceError:
            await self._set_back(moved, previous, move.target)
            raise
        await self._bus.publish(announcement)
        return stored

    async def _set_back(self, moved: Task, previous: TaskStatus, target: TaskStatus) -> None:
        """Undo a move whose event was not stored (compare-and-set from ``target``).

        Best effort: if the undo fails too, it is logged and the caller re-raises the
        event write's error.
        """
        try:
            await self._tasks.update_status(moved.user_id, moved.task_id, previous, expected=target)
        except errors.JarvisError as exc:
            logger.warning(
                "Could not set task %s back to %s after its event was lost: %s",
                moved.task_id, previous.value, exc.code,
            )


class SlotPassedSweep(Sweep):
    """Runs ``TaskService.sweep_passed_slots`` for every user, once a minute."""

    name: ClassVar[str] = "slot_passed"
    interval: ClassVar[timedelta] = timedelta(minutes=1)

    def __init__(self, tasks: TaskService) -> None:
        self._tasks = tasks

    async def run(self, user_ids: list[str], now: datetime) -> int:
        """Sweep each user's passed slots; return how many tasks were marked missed.

        Failures are counted and logged once per run, as one warning: while the
        database is down this runs every minute for every user, and a traceback each
        time would bury everything else in the log.
        """
        changed = 0
        failures: dict[str, int] = {}
        for user_id in user_ids:
            try:
                changed += len(await self._tasks.sweep_passed_slots(user_id, now))
            except Exception as exc:  # one user's failure never stops the others
                kind = type(exc).__name__
                failures[kind] = failures.get(kind, 0) + 1
        if failures:
            logger.warning(
                "Slot sweep could not finish for %d of %d users (%s)",
                sum(failures.values()),
                len(user_ids),
                ", ".join(f"{kind} x{count}" for kind, count in sorted(failures.items())),
            )
        return changed
