"""DraftService: accept is atomic and idempotent; the draft lifecycle is a state machine.

Accepting a draft is the one write path to the user's tasks. A second accept,
whether from a double-click or a REST accept followed by "looks good" in chat,
must find the draft closed and write nothing. It must never duplicate the tasks.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.domain import errors
from app.domain.entities import DraftStatus, TaskEvent, TaskEventType, TaskStatus
from app.domain.events import DomainEvent, DraftAccepted, EventBus, EventListener, NullEventBus
from app.domain.ports import TaskEventRepository
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
from app.services.draft_service import DraftService
from tests.behaviour.conftest import blame_phrases_in
from tests.fakes import FakeSupabase

U1 = "u1"
HORIZON = "2026-08-13T08:00:00+00:00"
H0 = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)  # HORIZON as an instant
NOW = datetime(2026, 8, 13, 7, 0, tzinfo=timezone.utc)


def _tasks(*task_ids: str) -> list[dict]:
    return [{"task_id": t, "title": f"do {t}", "duration_minutes": 25} for t in task_ids]


class _Recorder(EventListener):
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.events.append(event)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def repos(store):
    return RepositoryFactory.in_memory(store)


@pytest.fixture
def service(repos) -> DraftService:
    return DraftService(repos.drafts, NullEventBus())


async def _draft(
    repos, tasks: list[dict] | None = None, user_id: str = U1, schedule: dict | None = None
):
    return await repos.drafts.create(
        user_id,
        tasks=tasks if tasks is not None else _tasks("g_t1", "g_t2"),
        horizon_start=HORIZON,
        goal_id="g",
        schedule=schedule,
    )


def _slots(**starts: int) -> dict:
    """A solver map: each task_id -> a 25-minute slot that starts that many minutes in."""
    return {task: {"start_min": start, "end_min": start + 25} for task, start in starts.items()}


def _at(minute: int) -> datetime:
    return H0 + timedelta(minutes=minute)


def _recording_service(repos) -> DraftService:
    return DraftService(
        repos.drafts, NullEventBus(), task_events=repos.task_events, clock=lambda: NOW
    )


# --- accept -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept__open_draft__persists_and_publishes_draft_accepted(repos):
    bus, recorder = EventBus(), _Recorder()
    bus.subscribe(DraftAccepted, recorder)
    draft = await _draft(repos)

    written = await DraftService(repos.drafts, bus).accept(U1, draft.id)

    assert written == 2
    pending = await repos.tasks.list_pending(U1)
    assert sorted(t.task_id for t in pending) == ["g_t1", "g_t2"]
    assert len({t.plan_id for t in pending}) == 1 and pending[0].plan_id
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.ACCEPTED
    [event] = recorder.events
    assert (event.user_id, event.draft_id, event.task_count) == (U1, draft.id, 2)


@pytest.mark.asyncio
async def test_accept__twice__second_raises_and_tasks_not_duplicated(repos, store):
    """The user finished a task between the two accepts. A second write would
    delete only the pending rows and re-insert the whole draft, so the finished
    task would come back as a pending twin."""
    bus, recorder = EventBus(), _Recorder()
    bus.subscribe(DraftAccepted, recorder)
    service = DraftService(repos.drafts, bus)
    draft = await _draft(repos)
    await service.accept(U1, draft.id)
    await repos.tasks.update_status(U1, "g_t1", TaskStatus.COMPLETED)

    with pytest.raises(errors.InvalidStateTransition):
        await service.accept(U1, draft.id)

    assert sorted(t.task_id for t in store.tasks if t.user_id == U1) == ["g_t1", "g_t2"]
    assert (await repos.tasks.get(U1, "g_t1")).status is TaskStatus.COMPLETED
    assert len(recorder.events) == 1


@pytest.mark.asyncio
async def test_accept__concurrent_double_click__exactly_one_succeeds(repos, service):
    draft = await _draft(repos)

    results = await asyncio.gather(
        service.accept(U1, draft.id), service.accept(U1, draft.id), return_exceptions=True
    )

    assert sorted(type(r).__name__ for r in results) == ["InvalidStateTransition", "int"]
    assert [r for r in results if isinstance(r, int)] == [2]
    assert len(await repos.tasks.list_pending(U1)) == len(draft.tasks)


@pytest.mark.asyncio
async def test_accept__missing_draft__raises_not_found(service):
    with pytest.raises(errors.NotFoundError):
        await service.accept(U1, str(uuid4()))


@pytest.mark.asyncio
async def test_accept__other_users_draft__raises_not_found_and_writes_nothing(repos, service):
    draft = await _draft(repos, user_id="someone_else")

    with pytest.raises(errors.NotFoundError):
        await service.accept(U1, draft.id)

    assert await repos.tasks.list_pending(U1) == []
    assert (await repos.drafts.get("someone_else", draft.id)).status is DraftStatus.PENDING


@pytest.mark.asyncio
async def test_accept__empty_draft__raises_validation_error(repos, service):
    unparseable = [
        {"task_id": "x", "title": None},  # TaskChunk rejects it (a ValueError)
        {"task_id": "y", "duration_minutes": float("inf")},  # int(inf): an OverflowError
    ]
    for tasks in ([], unparseable):
        draft = await _draft(repos, tasks)

        with pytest.raises(errors.ValidationError, match="Draft holds nothing schedulable."):
            await service.accept(U1, draft.id)

        assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.PENDING
    assert await repos.tasks.list_pending(U1) == []


@pytest.mark.asyncio
async def test_accept__preserves_completed_tasks(repos, service):
    first = await _draft(repos, _tasks("old_t1", "old_t2"))
    await service.accept(U1, first.id)
    await repos.tasks.update_status(U1, "old_t1", TaskStatus.COMPLETED)

    second = await _draft(repos, _tasks("new_t1"))
    await service.accept(U1, second.id)

    assert (await repos.tasks.get(U1, "old_t1")).status is TaskStatus.COMPLETED
    assert [t.task_id for t in await repos.tasks.list_pending(U1)] == ["new_t1"]


# --- accept: scheduled and rescheduled events ---------------------------------


@pytest.mark.asyncio
async def test_accept__new_tasks__one_scheduled_event_each_with_slot(repos):
    """A task the solver placed gets one SCHEDULED event; one it did not place, none."""
    draft = await _draft(repos, _tasks("g_t1", "g_t2", "g_t3"), schedule=_slots(g_t1=0, g_t2=30))

    assert await _recording_service(repos).accept(U1, draft.id) == 3

    events = await repos.task_events.list_for_user(U1)
    slots = sorted((e.task_id, e.event_type, e.scheduled_start, e.scheduled_end) for e in events)
    assert slots == [
        ("g_t1", TaskEventType.SCHEDULED, _at(0), _at(25)),
        ("g_t2", TaskEventType.SCHEDULED, _at(30), _at(55)),
    ]
    assert {e.occurred_at for e in events} == {NOW}
    by_task = {e.task_id: e for e in events}
    assert by_task["g_t1"].metadata == {"title": "do g_t1", "goal_id": "g", "deadline_hint": None}


@pytest.mark.asyncio
async def test_accept__replan_moves_a_task__rescheduled_event_with_previous_start(repos):
    """The slot is compared with the task's LATEST slot event, not its first."""
    service = _recording_service(repos)
    for minute in (0, 60, 0):  # 08:00, then moved to 09:00, then back to 08:00
        draft = await _draft(repos, _tasks("g_t1"), schedule=_slots(g_t1=minute))
        await service.accept(U1, draft.id)

    events = await repos.task_events.list_for_user(U1)
    moves = [(e.event_type, e.scheduled_start, e.metadata.get("previous_start")) for e in events]
    assert moves == [
        (TaskEventType.SCHEDULED, _at(0), None),
        (TaskEventType.RESCHEDULED, _at(60), _at(0).isoformat()),
        (TaskEventType.RESCHEDULED, _at(0), _at(60).isoformat()),
    ]
    assert events[1].scheduled_end == _at(85)
    assert events[1].metadata["title"] == "do g_t1"


@pytest.mark.asyncio
async def test_accept__replan_same_slot__no_new_event(repos):
    service = _recording_service(repos)
    for _ in range(2):
        draft = await _draft(repos, schedule=_slots(g_t1=0, g_t2=30))
        await service.accept(U1, draft.id)

    events = await repos.task_events.list_for_user(U1)
    assert sorted((e.task_id, e.event_type) for e in events) == [
        ("g_t1", TaskEventType.SCHEDULED),
        ("g_t2", TaskEventType.SCHEDULED),
    ]


@pytest.mark.asyncio
async def test_accept__history_past_the_read_cap__same_slot_still_no_new_event():
    """Supabase answers at most max_rows rows per read. Newer events about other
    tasks must not crowd this task's latest slot out of the comparison."""
    supabase = FakeSupabase()
    supabase.max_rows = 3
    repos = RepositoryFactory.supabase(supabase)
    service = _recording_service(repos)
    first = await _draft(repos, _tasks("g_t1"), schedule=_slots(g_t1=0))
    await service.accept(U1, first.id)
    for n in range(5):  # newer history, all about other tasks
        await repos.task_events.append(U1, TaskEvent(
            user_id=U1, task_id=f"other_{n}", event_type=TaskEventType.SCHEDULED,
            occurred_at=NOW + timedelta(minutes=n + 1), scheduled_start=_at(0),
        ))

    again = await _draft(repos, _tasks("g_t1"), schedule=_slots(g_t1=0))
    await service.accept(U1, again.id)

    ours = await repos.task_events.list_for_user(U1, task_ids={"g_t1"})
    assert [e.event_type for e in ours] == [TaskEventType.SCHEDULED]


class _SpyEvents(TaskEventRepository):
    """Delegates to ``inner`` and records each call's name and keyword arguments."""

    def __init__(self, inner: TaskEventRepository) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    async def append(self, user_id, event):
        self.calls.append(("append", {}))
        return await self._inner.append(user_id, event)

    async def append_many(self, user_id, events):
        self.calls.append(("append_many", {"events": list(events)}))
        return await self._inner.append_many(user_id, events)

    async def list_for_user(self, user_id, *, since=None, types=None, task_ids=None):
        self.calls.append(("list_for_user", {"types": types, "task_ids": task_ids}))
        return await self._inner.list_for_user(
            user_id, since=since, types=types, task_ids=task_ids
        )


@pytest.mark.asyncio
async def test_accept__records_slots__one_bounded_read_and_one_bulk_write(repos):
    spy = _SpyEvents(repos.task_events)
    service = DraftService(repos.drafts, NullEventBus(), task_events=spy, clock=lambda: NOW)
    draft = await _draft(repos, _tasks("g_t1", "g_t2", "g_t3"), schedule=_slots(g_t1=0, g_t2=30))

    await service.accept(U1, draft.id)

    assert [name for name, _ in spy.calls] == ["list_for_user", "append_many"]
    read, write = spy.calls[0][1], spy.calls[1][1]
    assert read == {
        "types": {TaskEventType.SCHEDULED, TaskEventType.RESCHEDULED},
        "task_ids": {"g_t1", "g_t2"},  # the slotted tasks only
    }
    assert sorted(e.task_id for e in write["events"]) == ["g_t1", "g_t2"]


class _EventStoreDown(TaskEventRepository):
    """A task event store whose ``failing`` call raises as an unreachable database does."""

    def __init__(self, failing: str) -> None:
        self._failing = failing

    async def append(self, user_id: str, event: TaskEvent) -> TaskEvent:
        raise errors.PersistenceError("The database could not be reached.")

    async def append_many(self, user_id: str, events: list[TaskEvent]) -> list[TaskEvent]:
        if self._failing == "append_many":
            raise errors.PersistenceError("The database could not be reached.")
        return list(events)

    async def list_for_user(
        self, user_id, *, since=None, types=None, task_ids=None
    ) -> list[TaskEvent]:
        if self._failing == "list_for_user":
            raise errors.PersistenceError("The database could not be reached.")
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["append_many", "list_for_user"])
async def test_accept__event_store_down__accept_still_succeeds_and_logs(repos, caplog, failing):
    service = DraftService(
        repos.drafts, NullEventBus(), task_events=_EventStoreDown(failing), clock=lambda: NOW
    )
    draft = await _draft(repos, schedule=_slots(g_t1=0, g_t2=30))

    with caplog.at_level(logging.WARNING, logger="app.services.draft_service"):
        written = await service.accept(U1, draft.id)

    assert written == 2
    assert sorted(t.task_id for t in await repos.tasks.list_pending(U1)) == ["g_t1", "g_t2"]
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.ACCEPTED
    assert "task event" in caplog.text
    assert any(
        r.levelno == logging.WARNING and "task event" in r.getMessage() for r in caplog.records
    )


# --- reject -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept__every_slot_already_started__draft_outdated_and_nothing_written(repos):
    """Accepting a plan whose every time has gone by would only hand the slot sweep
    tasks to set aside: refused, with words that say so, and nothing written."""
    draft = await _draft(repos, schedule=_slots(g_t1=0, g_t2=30))
    service = DraftService(repos.drafts, NullEventBus(), clock=lambda: _at(30))  # g_t2 starts now

    with pytest.raises(errors.DraftOutdatedError) as raised:
        await service.accept(U1, draft.id)

    assert raised.value.http_status == 409
    assert raised.value.message == (
        "Every time in this plan has already gone by, so I didn't put it in place."
        " Ask me for a fresh plan and I'll fit it around now."
    )
    assert blame_phrases_in(raised.value.message) == []
    assert await repos.tasks.list_pending(U1) == []
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.PENDING


@pytest.mark.asyncio
async def test_accept__some_slots_already_started__accepted_as_drafted(repos):
    draft = await _draft(repos, schedule=_slots(g_t1=0, g_t2=60))
    service = DraftService(repos.drafts, NullEventBus(), clock=lambda: _at(30))

    assert await service.accept(U1, draft.id) == 2
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.ACCEPTED


@pytest.mark.asyncio
async def test_currency__started_slot_and_finished_task__reported(repos):
    """What makes a drafted plan out of date: a slot that has begun, and a task in it
    that is no longer pending or set aside (done or skipped since)."""
    first = await _draft(repos, tasks=_tasks("g_t1", "g_t2", "g_t3"))
    await DraftService(repos.drafts, NullEventBus()).accept(U1, first.id)
    await repos.tasks.update_status(U1, "g_t2", TaskStatus.COMPLETED)
    await repos.tasks.update_status(U1, "g_t3", TaskStatus.MISSED)
    draft = await _draft(
        repos, tasks=_tasks("g_t1", "g_t2", "g_t3"), schedule=_slots(g_t1=0, g_t2=60, g_t3=90)
    )
    service = DraftService(
        repos.drafts, NullEventBus(), tasks=repos.tasks, clock=lambda: _at(10)
    )

    currency = await service.currency(U1, draft)

    assert currency.started == frozenset({"g_t1"})
    assert currency.not_open == frozenset({"g_t2"})
    assert currency.set_aside == frozenset({"g_t3"})
    assert currency.current is False
    later = await service.currency(U1, draft.model_copy(update={"tasks": _tasks("g_t3")}))
    assert later.started == frozenset() and later.current is True  # g_t3's slot is still ahead


@pytest.mark.asyncio
async def test_reject__open_draft__rejects_and_keeps_reason(repos, service):
    draft = await _draft(repos)

    rejected = await service.reject(U1, draft.id, "too heavy")

    assert rejected.status is DraftStatus.REJECTED
    assert rejected.rejection_reason == "too heavy"
    assert await repos.tasks.list_pending(U1) == []


@pytest.mark.asyncio
async def test_reject__accepted_draft__raises_invalid_state_transition(repos, service):
    draft = await _draft(repos)
    await service.accept(U1, draft.id)

    with pytest.raises(errors.InvalidStateTransition):
        await service.reject(U1, draft.id, "changed my mind")

    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.ACCEPTED


# --- row building -------------------------------------------------------------


def test_build_task_rows__schedule_map__sets_wall_clock_times():
    from app.services.planning.task_rows import build_task_rows

    plan_id, rows = build_task_rows(
        U1,
        _tasks("g_t1", "g_t2"),
        schedule={"g_t1": {"start_min": 200, "end_min": 225}},
        horizon_start=HORIZON,
    )

    assert rows[0]["scheduled_start"] == "2026-08-13T11:20:00+00:00"  # 08:00 + 200 min
    assert rows[0]["scheduled_end"] == "2026-08-13T11:45:00+00:00"
    assert "scheduled_start" not in rows[1]  # not in the solver's map: stays timeless
    assert {r["plan_id"] for r in rows} == {plan_id}
    assert {r["goal_id"] for r in rows} == {"g"}


def test_build_task_rows__slot_at_minute_zero__gets_wall_clock_times():
    """Minute 0 is the start of the horizon, not a missing value."""
    from app.services.planning.task_rows import build_task_rows

    _, rows = build_task_rows(
        U1,
        _tasks("g_t1", "g_t2"),
        schedule={"g_t1": {"start_min": 0, "end_min": 25}, "g_t2": {"start": 0, "end": 25}},
        horizon_start=HORIZON,
    )

    for row in rows:
        assert row["scheduled_start"] == "2026-08-13T08:00:00+00:00"
        assert row["scheduled_end"] == "2026-08-13T08:25:00+00:00"


def test_build_task_rows__representative_chunks__exact_rows():
    """Pins the exact user_tasks row shape, so any drift in the builder shows here.

    Accept writes these rows through the accept_draft database function, whose
    column list is built from these keys.
    """
    from datetime import datetime

    from app.schemas.decomposition import ImplementationIntention, TaskChunk
    from app.services.planning.task_rows import build_task_rows

    chunks = [
        TaskChunk(  # an object chunk, scheduled with start_min/end_min
            task_id="exam_t1", title="Read ch1", duration_minutes=25, difficulty_weight=0.7,
            completion_criteria="Summarise ch1 from memory", deadline_hint="2026-08-20",
            implementation_intention=ImplementationIntention(
                obstacle_trigger="phone buzzes", behavioral_response="airplane mode"
            ),
        ),
        {  # a dict chunk with a dependency and keywords, scheduled with start/end
            "task_id": "exam_t2", "title": "Problems", "duration_minutes": 20,
            "difficulty_weight": 0.4, "dependencies": ["exam_t1"],
            "completion_criteria": "Solve 3 problems", "topic_keywords": ["calculus"],
        },
        {"task_id": "loose", "title": "No goal prefix", "duration_minutes": 10},  # unscheduled
    ]
    schedule = {"exam_t1": {"start_min": 60, "end_min": 85}, "exam_t2": {"start": 90, "end": 110}}

    plan_id, rows = build_task_rows(U1, chunks, schedule=schedule, horizon_start=HORIZON)

    assert {row.pop("plan_id") for row in rows} == {plan_id}
    created = {row.pop("created_at") for row in rows}
    assert len(created) == 1 and datetime.fromisoformat(created.pop()).tzinfo is not None
    assert rows == [
        {
            "user_id": U1, "task_id": "exam_t1", "title": "Read ch1", "status": "pending",
            "duration_minutes": 25, "difficulty_weight": 0.7, "dependencies": [],
            "deadline_hint": "2026-08-20", "goal_id": "exam",
            "completion_criteria": "Summarise ch1 from memory",
            "implementation_intention": {
                "obstacle_trigger": "phone buzzes", "behavioral_response": "airplane mode",
            },
            "scheduled_start": "2026-08-13T09:00:00+00:00",
            "scheduled_end": "2026-08-13T09:25:00+00:00",
        },
        {
            "user_id": U1, "task_id": "exam_t2", "title": "Problems", "status": "pending",
            "duration_minutes": 20, "difficulty_weight": 0.4, "dependencies": ["exam_t1"],
            "deadline_hint": None, "goal_id": "exam", "completion_criteria": "Solve 3 problems",
            "topic_keywords": ["calculus"],
            "scheduled_start": "2026-08-13T09:30:00+00:00",
            "scheduled_end": "2026-08-13T09:50:00+00:00",
        },
        {
            "user_id": U1, "task_id": "loose", "title": "No goal prefix", "status": "pending",
            "duration_minutes": 10, "difficulty_weight": None, "dependencies": [],
            "deadline_hint": None,
        },
    ]


def test_build_task_rows__explicit_goal_id__wins_over_id_prefix():
    from app.services.planning.task_rows import build_task_rows

    chunk = {"task_id": "exam_t1", "goal_id": "thesis", "title": "x", "duration_minutes": 25}

    _, [row] = build_task_rows(U1, [chunk], schedule=None, horizon_start=HORIZON)

    assert row["goal_id"] == "thesis"


@pytest.mark.asyncio
async def test_build_task_rows__multi_word_goal__matches_draft_goal_id(repos, service):
    """One plan, one goal: `learn_dsa_…` used to be filed under `learn`.

    Through accept, so the goal_id has to survive coercion into a TaskChunk too
    (TaskChunk has no goal_id field).
    """
    tasks = [
        {"task_id": f"learn_dsa_ab12cd_task_{n}", "goal_id": "learn_dsa", "title": f"step {n}",
         "duration_minutes": 25}
        for n in (1, 2)
    ]
    draft = await repos.drafts.create(
        U1, tasks=tasks, horizon_start=HORIZON, goal_id="learn_dsa", schedule=None
    )

    await service.accept(U1, draft.id)

    assert [t.goal_id for t in await repos.tasks.list_pending(U1)] == [draft.goal_id] * 2


def test_build_task_rows__legacy_dict_without_goal_id__falls_back_to_prefix():
    """A chunk written before goal_id travelled still gets one, read off its id."""
    from app.services.planning.task_rows import build_task_rows

    _, rows = build_task_rows(
        U1, _tasks("exam_t1", "loose"), schedule=None, horizon_start=HORIZON
    )

    assert rows[0]["goal_id"] == "exam"
    assert "goal_id" not in rows[1]  # no prefix to read: the row names no goal


# --- the legacy entry point ---------------------------------------------------


async def _container_with_draft(supabase: FakeSupabase) -> tuple:
    from tests.fakes import build_test_container

    container = build_test_container(supabase)
    draft = await container.draft_service.propose(
        U1, tasks=_tasks("g_t1", "g_t2"), horizon_start=HORIZON, goal_id=None, schedule=None
    )
    return container, draft


@pytest.mark.asyncio
async def test_accept_draft_and_persist__already_accepted__raises_invalid_state_transition():
    from app.services.draft_actions import accept_draft_and_persist

    supabase = FakeSupabase()
    container, draft = await _container_with_draft(supabase)
    drafts, planner = container.draft_service, container.planner
    assert await accept_draft_and_persist(drafts, planner, U1, draft) == 2

    with pytest.raises(errors.InvalidStateTransition):
        await accept_draft_and_persist(drafts, planner, U1, draft)

    assert sorted(r["task_id"] for r in supabase.rows["user_tasks"]) == ["g_t1", "g_t2"]


@pytest.mark.asyncio
async def test_accept_draft_and_persist__database_unreachable__returns_none_and_leaves_draft_open():
    """None is the "retry" answer, so the draft has to still be open for the retry."""
    from app.services.draft_actions import accept_draft_and_persist

    class _RpcDown(FakeSupabase):
        def rpc(self, name, params):
            raise ConnectionError("connection refused")

    supabase = _RpcDown()
    container, draft = await _container_with_draft(supabase)

    assert await accept_draft_and_persist(
        container.draft_service, container.planner, U1, draft
    ) is None
    (row,) = supabase.rows["draft_schedules"]
    assert row["status"] == "pending"
    assert supabase.rows.get("user_tasks", []) == []
