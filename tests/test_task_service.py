"""TaskService: every Complete / Skip / miss is one append-only task event.

"Missed" is never asked about: the slot sweep infers it once a slot has ended and the
grace period has passed with no outcome recorded. Each move is a compare-and-set on
the task's status, so a double-click or a sweep racing a late completion writes
exactly one event.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain import errors
from app.domain.entities import Task, TaskEvent, TaskEventType, TaskStatus
from app.domain.events import (
    DeadlineMissed,
    DomainEvent,
    EventBus,
    EventListener,
    TaskCompleted,
    TaskMissed,
    TaskSkipped,
)
from app.domain.ports import TaskEventRepository, TaskRepository
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
from app.services.task_service import SlotPassedSweep, TaskService

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 14, 15, 0, tzinfo=IST)
U1 = "alice"


# --- Builders and fakes --------------------------------------------------------


def _task(
    task_id: str = "t1",
    *,
    status: TaskStatus = TaskStatus.PENDING,
    start: datetime | None = None,
    minutes: int = 25,
    user_id: str = U1,
) -> Task:
    """A task as draft acceptance stores it: slot instants in UTC."""
    start = (start or NOW - timedelta(hours=2)).astimezone(timezone.utc)
    return Task(
        user_id=user_id,
        task_id=task_id,
        goal_id="g1",
        title=f"Revise {task_id}",
        duration_minutes=minutes,
        difficulty_weight=0.5,
        status=status,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=minutes),
        deadline_hint="2026-11-20",
    )


def _ended_ago(minutes_ago: int, task_id: str = "t1", *, user_id: str = U1) -> Task:
    """A 25-minute task whose slot ended ``minutes_ago`` minutes before NOW."""
    return _task(task_id, start=NOW - timedelta(minutes=minutes_ago + 25), user_id=user_id)


class _Recorder(EventListener):
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.events.append(event)


class _DelegatingTasks(TaskRepository):
    """Passes every call through to ``inner``; subclasses intercept one."""

    def __init__(self, inner: TaskRepository) -> None:
        self._inner = inner

    async def list_pending(self, user_id):
        return await self._inner.list_pending(user_id)

    async def get(self, user_id, task_id):
        return await self._inner.get(user_id, task_id)

    async def list_scheduled_between(self, user_id, start, end):
        return await self._inner.list_scheduled_between(user_id, start, end)

    async def update_status(self, user_id, task_id, status, *, expected=None):
        return await self._inner.update_status(user_id, task_id, status, expected=expected)

    async def reschedule(self, user_id, task_id, start, end):
        return await self._inner.reschedule(user_id, task_id, start, end)


class _FailingEvents(TaskEventRepository):
    """An event log whose writes fail while ``fail`` is set (the database dropped)."""

    def __init__(self, inner: TaskEventRepository) -> None:
        self._inner = inner
        self.fail = True

    async def append(self, user_id, event):
        if self.fail:
            raise errors.PersistenceError("Could not reach the database.")
        return await self._inner.append(user_id, event)

    async def list_for_user(self, user_id, *, since=None, types=None):
        return await self._inner.list_for_user(user_id, since=since, types=types)


class _YieldingTasks(_DelegatingTasks):
    """Yields to the event loop before each read, so two concurrent clicks interleave."""

    async def get(self, user_id, task_id):
        await asyncio.sleep(0)
        return await super().get(user_id, task_id)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def repos(store) -> Repositories:
    return RepositoryFactory.in_memory(store)


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def bus(recorder) -> EventBus:
    bus = EventBus()
    bus.subscribe(DomainEvent, recorder)
    return bus


@pytest.fixture
def service(repos, bus) -> TaskService:
    return TaskService(repos.tasks, repos.task_events, bus, clock=lambda: NOW)


def _seed(store: InMemoryStore, *tasks: Task) -> None:
    store.tasks.extend(tasks)


async def _events(repos: Repositories, user_id: str = U1) -> list[TaskEvent]:
    return await repos.task_events.list_for_user(user_id)


async def _status(repos: Repositories, task_id: str = "t1", user_id: str = U1) -> TaskStatus:
    return (await repos.tasks.get(user_id, task_id)).status


# --- Complete --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete__pending_task__status_completed_and_one_completed_event_with_slot_and_metadata(
    service, repos, store, recorder
):
    task = _task()
    _seed(store, task)

    event = await service.complete(U1, "t1", actual_duration_minutes=20, quality=4)

    assert event.event_type == TaskEventType.COMPLETED
    assert event.id is not None and event.occurred_at == NOW
    assert event.scheduled_start == task.scheduled_start
    assert event.scheduled_end == task.scheduled_end
    assert set(event.metadata) >= {
        "title", "goal_id", "deadline_hint", "actual_duration_minutes", "quality"
    }
    assert event.metadata["title"] == "Revise t1" and event.metadata["goal_id"] == "g1"
    assert event.metadata["actual_duration_minutes"] == 20 and event.metadata["quality"] == 4
    assert await _status(repos) == TaskStatus.COMPLETED
    assert await _events(repos) == [event]
    (published,) = recorder.events
    assert isinstance(published, TaskCompleted)
    assert (published.user_id, published.task_id, published.occurred_at) == (U1, "t1", NOW)


@pytest.mark.asyncio
async def test_complete__double_click__second_returns_none_and_one_event(repos, store, bus, recorder):
    _seed(store, _task())
    # Both clicks read the task as pending before either writes: the compare-and-set
    # is what lets exactly one of them through.
    service = TaskService(_YieldingTasks(repos.tasks), repos.task_events, bus, clock=lambda: NOW)

    results = await asyncio.gather(service.complete(U1, "t1"), service.complete(U1, "t1"))

    assert sorted(r is None for r in results) == [False, True]
    assert len(await _events(repos)) == 1
    assert [type(e) for e in recorder.events] == [TaskCompleted]
    assert await service.complete(U1, "t1") is None  # a third, later click
    assert len(await _events(repos)) == 1


@pytest.mark.asyncio
async def test_complete__missed_task__allowed_and_status_completed(service, repos, store):
    _seed(store, _task(status=TaskStatus.MISSED))

    event = await service.complete(U1, "t1")

    assert event.event_type == TaskEventType.COMPLETED
    assert await _status(repos) == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_complete__skipped_task__allowed_and_status_completed(service, repos, store):
    _seed(store, _task(status=TaskStatus.SKIPPED))  # "I actually did that"

    event = await service.complete(U1, "t1")

    assert event.event_type == TaskEventType.COMPLETED
    assert await _status(repos) == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_complete__event_write_fails__status_restored_and_error_raised(
    repos, store, bus, recorder
):
    # Status and event are two writes. If the event is lost the status must go back,
    # or the outcome is gone for good: a retry would find the task already completed.
    _seed(store, _task(status=TaskStatus.SKIPPED))
    events = _FailingEvents(repos.task_events)
    service = TaskService(repos.tasks, events, bus, clock=lambda: NOW)

    with pytest.raises(errors.PersistenceError):
        await service.complete(U1, "t1")

    assert await _status(repos) == TaskStatus.SKIPPED  # the status it came from
    assert await _events(repos) == []
    assert recorder.events == []
    events.fail = False
    retried = await service.complete(U1, "t1")
    assert retried.event_type == TaskEventType.COMPLETED
    assert await _events(repos) == [retried]


@pytest.mark.asyncio
async def test_complete__unknown_task__not_found(service, repos):
    with pytest.raises(errors.NotFoundError):
        await service.complete(U1, "nope")

    assert await _events(repos) == []


# --- Skip --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip__pending__skipped_event_with_reason_and_task_skipped_published(
    service, repos, store, recorder
):
    _seed(store, _task())

    event = await service.skip(U1, "t1", reason="too tired")

    assert event.event_type == TaskEventType.SKIPPED
    assert set(event.metadata) >= {"title", "goal_id", "deadline_hint", "reason"}
    assert event.metadata["reason"] == "too tired"
    assert await _status(repos) == TaskStatus.SKIPPED
    (published,) = recorder.events
    assert isinstance(published, TaskSkipped)
    assert (published.task_id, published.reason) == ("t1", "too tired")


@pytest.mark.asyncio
async def test_skip__completed_task__returns_none_no_event(service, repos, store, recorder):
    _seed(store, _task(status=TaskStatus.COMPLETED))

    assert await service.skip(U1, "t1") is None

    assert await _events(repos) == []
    assert await _status(repos) == TaskStatus.COMPLETED
    assert recorder.events == []


# --- Slot sweep --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep__slot_ended_31_min_ago__missed_event_and_status(service, repos, store, recorder):
    task = _ended_ago(31)
    _seed(store, task)

    (event,) = await service.sweep_passed_slots(U1, NOW)

    assert event.event_type == TaskEventType.MISSED
    assert event.occurred_at == NOW
    assert (event.scheduled_start, event.scheduled_end) == (task.scheduled_start, task.scheduled_end)
    assert set(event.metadata) >= {"title", "goal_id", "deadline_hint"}
    assert await _status(repos) == TaskStatus.MISSED
    assert await _events(repos) == [event]
    (published,) = recorder.events
    assert isinstance(published, TaskMissed) and published.task_id == "t1"


@pytest.mark.asyncio
async def test_sweep__slot_ended_29_min_ago__untouched(service, repos, store, recorder):
    _seed(store, _ended_ago(29))

    assert await service.sweep_passed_slots(U1, NOW) == []

    assert await _status(repos) == TaskStatus.PENDING
    assert await _events(repos) == []
    assert recorder.events == []


@pytest.mark.asyncio
async def test_sweep__completed_inside_grace__never_marked_missed(repos, store, bus):
    # t1: the user ticks it off 10 minutes after its slot ended, inside the grace.
    # t2: its completion is recorded inside the grace but the task row still reads
    # pending (the status write had not landed when the sweep looked).
    t1, t2 = _ended_ago(10, "t1"), _ended_ago(10, "t2")
    _seed(store, t1, t2)
    service = TaskService(repos.tasks, repos.task_events, bus, clock=lambda: NOW)
    await service.complete(U1, "t1")
    await repos.task_events.append(
        U1,
        TaskEvent(
            user_id=U1,
            task_id="t2",
            event_type=TaskEventType.COMPLETED,
            occurred_at=NOW,
            scheduled_start=t2.scheduled_start,
            scheduled_end=t2.scheduled_end,
        ),
    )
    later = NOW + timedelta(minutes=25)  # both slots ended 35 minutes ago by now

    assert await service.sweep_passed_slots(U1, later) == []

    assert await _status(repos, "t1") == TaskStatus.COMPLETED
    assert await _status(repos, "t2") == TaskStatus.PENDING
    assert [e.event_type for e in await _events(repos)] == [TaskEventType.COMPLETED] * 2


@pytest.mark.asyncio
async def test_sweep__outcome_before_reslot__new_slot_still_marked_missed(service, repos, store):
    # The task was missed in an earlier slot and re-slotted (pending again, later
    # slot). That old outcome predates the new slot, so it does not settle it.
    task = _ended_ago(31)
    _seed(store, task)
    await repos.task_events.append(
        U1,
        TaskEvent(
            user_id=U1,
            task_id="t1",
            event_type=TaskEventType.MISSED,
            occurred_at=task.scheduled_start - timedelta(days=1),
            scheduled_start=task.scheduled_start - timedelta(days=1, minutes=30),
            scheduled_end=task.scheduled_end - timedelta(days=1, minutes=30),
        ),
    )

    (event,) = await service.sweep_passed_slots(U1, NOW)

    assert event.event_type == TaskEventType.MISSED
    assert event.scheduled_start == task.scheduled_start
    assert await _status(repos) == TaskStatus.MISSED
    assert len(await _events(repos)) == 2


@pytest.mark.asyncio
async def test_sweep__race_with_completion__stale_version_swallowed_one_event(repos, store, bus):
    _seed(store, _ended_ago(31))
    user = TaskService(repos.tasks, repos.task_events, bus, clock=lambda: NOW)

    class _CompletionLandsFirst(_DelegatingTasks):
        """The user's Complete lands between the sweep's read and its write."""

        landed = False

        async def update_status(self, user_id, task_id, status, *, expected=None):
            if status == TaskStatus.MISSED and not self.landed:
                self.landed = True
                await user.complete(user_id, task_id)
            return await super().update_status(user_id, task_id, status, expected=expected)

    sweeper = TaskService(
        _CompletionLandsFirst(repos.tasks), repos.task_events, bus, clock=lambda: NOW
    )

    assert await sweeper.sweep_passed_slots(U1, NOW) == []

    assert await _status(repos) == TaskStatus.COMPLETED
    assert [e.event_type for e in await _events(repos)] == [TaskEventType.COMPLETED]


@pytest.mark.asyncio
async def test_sweep__twice__idempotent(service, repos, store):
    _seed(store, _ended_ago(31))

    first = await service.sweep_passed_slots(U1, NOW)
    second = await service.sweep_passed_slots(U1, NOW + timedelta(minutes=1))

    assert len(first) == 1 and second == []
    assert len(await _events(repos)) == 1


@pytest.mark.asyncio
async def test_sweep__slot_just_after_local_midnight__uses_instants(service, repos, store):
    # 00:15-00:40 IST on the 15th is 18:45-19:10 UTC on the 14th: a different date on
    # each clock. The sweep compares instants, so the date never decides.
    slot = _task("t1", start=datetime(2026, 11, 15, 0, 15, tzinfo=IST))
    not_yet = _task("t2", start=datetime(2026, 11, 15, 0, 50, tzinfo=IST))  # ends 01:15
    _seed(store, slot, not_yet)
    now = datetime(2026, 11, 15, 1, 15, tzinfo=IST)

    (event,) = await service.sweep_passed_slots(U1, now)

    assert event.task_id == "t1"
    assert event.scheduled_start == datetime(2026, 11, 14, 18, 45, tzinfo=timezone.utc)
    assert await _status(repos, "t1") == TaskStatus.MISSED
    assert await _status(repos, "t2") == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_slot_passed_sweep__one_users_failure__others_still_swept(repos, store, bus, caplog):
    _seed(store, _ended_ago(31, user_id="alice"), _ended_ago(31, user_id="carol"))

    class _BobUnreadable(_DelegatingTasks):
        async def list_scheduled_between(self, user_id, start, end):
            if user_id == "bob":
                raise errors.PersistenceError("Could not reach the database.")
            return await super().list_scheduled_between(user_id, start, end)

    sweep = SlotPassedSweep(
        TaskService(_BobUnreadable(repos.tasks), repos.task_events, bus, clock=lambda: NOW)
    )

    with caplog.at_level(logging.INFO, logger="app.services.task_service"):
        changed = await sweep.run(["alice", "bob", "carol"], NOW)

    assert changed == 2
    assert await _status(repos, user_id="alice") == TaskStatus.MISSED
    assert await _status(repos, user_id="carol") == TaskStatus.MISSED
    (warning,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning.levelno == logging.WARNING and "1 of 3" in warning.getMessage()
    assert (SlotPassedSweep.name, SlotPassedSweep.interval) == ("slot_passed", timedelta(minutes=1))


@pytest.mark.asyncio
async def test_slot_passed_sweep__db_down__one_warning_with_count_and_no_traceback(
    repos, bus, caplog
):
    class _Unreachable(_DelegatingTasks):
        async def list_scheduled_between(self, user_id, start, end):
            raise errors.PersistenceError("Could not reach the database.")

    sweep = SlotPassedSweep(
        TaskService(_Unreachable(repos.tasks), repos.task_events, bus, clock=lambda: NOW)
    )

    with caplog.at_level(logging.INFO, logger="app.services.task_service"):
        assert await sweep.run(["alice", "bob", "carol"], NOW) == 0

    # Once per run, not a traceback per user per tick while the database is down.
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "3 of 3" in record.getMessage() and "PersistenceError" in record.getMessage()
    assert record.exc_info is None


# --- Deadline misses ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_deadline_missed__pending__deadline_missed_event_and_status_missed(
    service, repos, store, recorder
):
    _seed(store, _task())

    event = await service.mark_deadline_missed(U1, "t1")

    assert event.event_type == TaskEventType.DEADLINE_MISSED
    assert set(event.metadata) >= {"title", "goal_id", "deadline_hint"}
    assert await _status(repos) == TaskStatus.MISSED
    (published,) = recorder.events
    assert isinstance(published, DeadlineMissed) and published.task_id == "t1"
    assert await service.mark_deadline_missed(U1, "t1") is None  # already set aside


@pytest.mark.asyncio
async def test_detect_and_mark_missed__with_task_service__writes_deadline_missed_events():
    from app.services.analytical.missed_deadlines import detect_and_mark_missed
    from tests.fakes import FakeSupabase

    def row(task_id: str, deadline: str) -> dict:
        return {
            "id": f"row-{task_id}", "user_id": U1, "task_id": task_id, "title": f"Revise {task_id}",
            "status": "pending", "duration_minutes": 25, "difficulty_weight": 0.5,
            "deadline_hint": deadline,
        }

    fake = FakeSupabase({"user_tasks": [row("t1", "2020-01-01"), row("t2", "2999-01-01")]})
    repos = RepositoryFactory.supabase(fake)
    service = TaskService(repos.tasks, repos.task_events, EventBus(), clock=lambda: NOW)

    overdue = await detect_and_mark_missed(U1, fake, task_service=service)

    assert [o["task_id"] for o in overdue] == ["t1"]
    (event,) = fake.rows["task_events"]
    assert (event["task_id"], event["event_type"]) == ("t1", "deadline_missed")
    statuses = {r["task_id"]: r["status"] for r in fake.rows["user_tasks"]}
    assert statuses == {"t1": "missed", "t2": "pending"}


@pytest.mark.asyncio
async def test_detect_and_mark_missed__one_task_errors__others_still_marked_and_returned():
    from app.services.analytical.missed_deadlines import detect_and_mark_missed
    from tests.fakes import FakeSupabase

    rows = [
        {"id": f"row-{t}", "user_id": U1, "task_id": t, "title": f"Revise {t}",
         "status": "pending", "deadline_hint": "2020-01-01"}
        for t in ("t1", "t2", "t3")
    ]

    class _FlakyService:
        """Marks every task but t2, which fails with an error nobody planned for."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def mark_deadline_missed(self, user_id, task_id):
            self.calls.append(task_id)
            if task_id == "t2":
                raise RuntimeError("unexpected")
            return TaskEvent(
                user_id=user_id, task_id=task_id, event_type=TaskEventType.DEADLINE_MISSED,
                occurred_at=NOW,
            )

    service = _FlakyService()

    overdue = await detect_and_mark_missed(U1, FakeSupabase({"user_tasks": rows}), task_service=service)

    assert service.calls == ["t1", "t2", "t3"]
    assert [o["task_id"] for o in overdue] == ["t1", "t3"]
