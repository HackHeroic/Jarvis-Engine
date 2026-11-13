"""DraftService: accept is atomic and idempotent; the draft lifecycle is a state machine.

Accepting a draft is the one write path to the user's tasks. A second accept,
whether from a double-click or a REST accept followed by "looks good" in chat,
must find the draft closed and write nothing. It must never duplicate the tasks.
"""

import asyncio
from uuid import uuid4

import pytest

from app.domain import errors
from app.domain.entities import DraftStatus, TaskStatus
from app.domain.events import DomainEvent, DraftAccepted, EventBus, EventListener, NullEventBus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
from app.services.draft_service import DraftService
from tests.fakes import FakeSupabase

U1 = "u1"
HORIZON = "2026-08-13T08:00:00+00:00"


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


async def _draft(repos, tasks: list[dict] | None = None, user_id: str = U1):
    return await repos.drafts.create(
        user_id,
        tasks=tasks if tasks is not None else _tasks("g_t1", "g_t2"),
        horizon_start=HORIZON,
        goal_id="g",
        schedule=None,
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


# --- reject -----------------------------------------------------------------


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

    from app.api.v1.endpoints.reasoning import ImplementationIntention, TaskChunk
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


# --- the legacy entry point ---------------------------------------------------


def _store_with_draft(supabase: FakeSupabase) -> tuple:
    from app.services.draft_store import DraftStore

    store = DraftStore(supabase_client=supabase)
    row = store.create_draft(user_id=U1, tasks=_tasks("g_t1", "g_t2"), horizon_start=HORIZON)
    return store, row


@pytest.mark.asyncio
async def test_accept_draft_and_persist__already_accepted__raises_invalid_state_transition():
    from app.services.draft_actions import accept_draft_and_persist

    supabase = FakeSupabase()
    store, row = _store_with_draft(supabase)
    assert await accept_draft_and_persist(store, U1, row, supabase) == 2

    with pytest.raises(errors.InvalidStateTransition):
        await accept_draft_and_persist(store, U1, row, supabase)

    assert sorted(r["task_id"] for r in supabase.rows["user_tasks"]) == ["g_t1", "g_t2"]


@pytest.mark.asyncio
async def test_accept_draft_and_persist__database_unreachable__returns_none_and_leaves_draft_open():
    """None is the "retry" answer, so the draft has to still be open for the retry."""
    from app.services.draft_actions import accept_draft_and_persist

    class _RpcDown(FakeSupabase):
        def rpc(self, name, params):
            raise ConnectionError("connection refused")

    supabase = _RpcDown()
    store, row = _store_with_draft(supabase)

    assert await accept_draft_and_persist(store, U1, row, supabase) is None
    assert store.get_draft(row["id"], U1)["status"] == "pending"
    assert supabase.rows.get("user_tasks", []) == []
