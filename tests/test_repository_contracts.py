"""Repository contract suite: every test runs against BOTH repository families.

The ``repos`` fixture is parametrised over the in-memory family and the Supabase
family (driven by ``FakeSupabase``), and the tests only ever talk to the ports.
The same assertions passing for both is the Liskov-substitution evidence: a
caller holding a ``TaskRepository`` cannot tell which family it was given.
"""

import asyncio
import itertools
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.domain import errors
from app.domain.entities import (
    AutonomyMode,
    BehavioralConstraint,
    ChatMessage,
    ConsentDecision,
    ConversationSession,
    ConsentReasonKind,
    DraftStatus,
    Goal,
    GoalDeadlineUpdate,
    HabitTracker,
    IngestedDocument,
    LLMLocation,
    LLMUsageEvent,
    Memory,
    MemorySource,
    MemoryStatus,
    StoredFile,
    Suggestion,
    SuggestionStatus,
    TaskEvent,
    TaskEventType,
    TaskMaterialLink,
    TaskStatus,
)
from app.domain.ports import (
    DOCUMENT_LIST_LIMIT,
    TASK_LIST_LIMIT,
    ConstraintRepository,
    DocumentFileStore,
    DocumentRepository,
    DraftRepository,
    GoalRepository,
    HabitTrackerRepository,
    MemoryRepository,
    ProfileRepository,
    SuggestionRepository,
    TaskEventRepository,
    TaskRepository,
    UsageRepository,
)
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
from app.infrastructure.persistence.supabase_repos import SupabaseMemoryRepository
from tests.fakes import FakeSupabase

U1, U2 = "user-1", "user-2"
T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def memory_store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def fake_client() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture(params=["in_memory", "supabase_fake"])
def repos(request, memory_store, fake_client) -> Repositories:
    if request.param == "in_memory":
        return RepositoryFactory.in_memory(memory_store)
    return RepositoryFactory.supabase(fake_client)


@pytest.fixture
def expire_draft(memory_store, fake_client):
    """Backdate a draft's expiry in whichever store holds it.

    No port moves ``expires_at`` (time passing does), so this is the one place
    the suite reaches behind the ports. It writes to both stores; only the one
    behind the current ``repos`` holds the draft.
    """

    def _expire(draft_id: str) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        for i, draft in enumerate(memory_store.drafts):
            if draft.id == draft_id:
                memory_store.drafts[i] = draft.model_copy(update={"expires_at": past})
        for row in fake_client.rows.get("draft_schedules", []):
            if row.get("id") == draft_id:
                row["expires_at"] = past.isoformat()

    return _expire


@pytest.fixture
def seed_goal(memory_store, fake_client):
    """Store a goal in whichever store backs ``repos``.

    No port writes a goal (the database's ensure_goal trigger creates its row), so the
    suite reaches behind the ports here, writing both stores, as ``expire_draft`` does.
    """

    def _seed(goal: Goal) -> Goal:
        memory_store.goals.append(goal)
        fake_client.rows.setdefault("goals", []).append(goal.model_dump(mode="json"))
        return goal

    return _seed


@pytest.fixture
def seed_tracker(memory_store, fake_client):
    """Store a habit tracker in whichever store backs ``repos``, each one newer than
    the last: any state, even one no port writes (a review's, or a second tracker for
    a rule), so the suite writes both stores."""
    ages = itertools.count()

    def _seed(user_id: str, **fields) -> HabitTracker:
        tracker = HabitTracker(id=str(uuid4()), user_id=user_id, **fields)
        memory_store.habit_trackers.append(tracker)
        created_at = (T0 + timedelta(seconds=next(ages))).isoformat()
        fake_client.rows.setdefault("habit_trackers", []).append(
            {**tracker.model_dump(mode="json"), "created_at": created_at}
        )
        return tracker

    return _seed


# --- Builders ----------------------------------------------------------------


def _row(task_id: str, *, status: str = "pending", start: datetime | None = None) -> dict:
    """A user_tasks row shaped like the ones draft acceptance writes."""
    row = {
        "task_id": task_id,
        "title": f"Task {task_id}",
        "goal_id": "g1",
        "plan_id": "plan-1",
        "status": status,
        "duration_minutes": 25,
        "difficulty_weight": 0.5,
        "dependencies": [],
        "deadline_hint": None,
        "completion_criteria": "Explain it back",
    }
    if start is not None:
        row["scheduled_start"] = start.isoformat()
        row["scheduled_end"] = (start + timedelta(minutes=25)).isoformat()
    return row


async def _new_draft(repos: Repositories, user_id: str = U1):
    return await repos.drafts.create(
        user_id,
        tasks=[{"task_id": "g1_t1", "title": "Read chapter 1", "duration_minutes": 25}],
        horizon_start=T0.isoformat(),
        goal_id="g1",
        schedule={"g1_t1": {"start_min": 0, "end_min": 25}},
    )


async def _seed_tasks(repos: Repositories, user_id: str, rows: list[dict]) -> None:
    """Write tasks the only way user_tasks is ever written: by accepting a draft."""
    draft = await _new_draft(repos, user_id)
    await repos.drafts.accept_atomic(user_id, draft.id, rows)


def _event(user_id: str, event_type: TaskEventType, at: datetime, task_id: str = "t1") -> TaskEvent:
    return TaskEvent(user_id=user_id, task_id=task_id, event_type=event_type, occurred_at=at)


def _constraint(user_id: str, *, source: str = "said") -> BehavioralConstraint:
    return BehavioralConstraint(
        id=str(uuid4()),
        user_id=user_id,
        raw_text="No work after 10 PM",
        constraint_type="sleep",
        recurrence="daily",
        structured_semantics={"start_min": 1320, "end_min": 1440},
        source=source,
    )


def _suggestion(
    user_id: str, *, trigger: str = "evening_review", created_at: datetime = T0
) -> Suggestion:
    return Suggestion(
        user_id=user_id,
        trigger=trigger,
        kind="reschedule",
        title="Move tomorrow's hard task",
        body="You usually fade after 9 PM; shall I move it to the morning?",
        action={"task_id": "g1_t1"},
        status=SuggestionStatus.PENDING,
        consent_behavior="ask",
        consent_reason="Moves a scheduled task.",
        created_at=created_at,
    )


def _usage(
    user_id: str | None,
    location: LLMLocation,
    input_tokens: int,
    output_tokens: int,
    *,
    pii: int = 0,
    at: datetime = T0,
) -> LLMUsageEvent:
    return LLMUsageEvent(
        user_id=user_id,
        task="voice_of_jarvis",
        role="fast" if location is LLMLocation.LOCAL else "cloud",
        provider="local" if location is LLMLocation.LOCAL else "claude",
        model="gemma-4-e4b" if location is LLMLocation.LOCAL else "claude-sonnet-5-5",
        location=location,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=120,
        pii_redactions=pii,
        created_at=at,
    )


def _memory(
    user_id: str,
    content: str = "Prefers deep work before noon",
    *,
    memory_type: str = "preference",
    status: MemoryStatus = MemoryStatus.ACTIVE,
    **fields,
) -> Memory:
    return Memory(
        user_id=user_id,
        memory_type=memory_type,
        content=content,
        status=status,
        provenance=fields.pop("provenance", MemorySource.SAID),
        strength_as_of=T0,
        created_at=fields.pop("created_at", T0),
        **fields,
    )


def _plan_update(
    user_id: str, goal_id: str, context: str | None, *, at: datetime | None = None
) -> GoalDeadlineUpdate:
    return GoalDeadlineUpdate(
        user_id=user_id,
        goal_id=goal_id,
        source="ingestion",
        deadline_date=date(2026, 12, 1),
        deadline_raw="by December",
        context_snippet=context,
        created_at=at,
    )


def _vec(x: float, y: float) -> list[float]:
    """A 384-dimension embedding pointing (x, y) in its first two dimensions."""
    return [x, y] + [0.0] * 382


ACTIVE = frozenset({MemoryStatus.ACTIVE})


# --- Factory -----------------------------------------------------------------


def test_factory__both_families__implement_every_port():
    ports = {
        "tasks": TaskRepository,
        "task_events": TaskEventRepository,
        "drafts": DraftRepository,
        "constraints": ConstraintRepository,
        "profiles": ProfileRepository,
        "suggestions": SuggestionRepository,
        "usage": UsageRepository,
        "memories": MemoryRepository,
        "goals": GoalRepository,
        "habit_trackers": HabitTrackerRepository,
        "documents": DocumentRepository,
        "document_files": DocumentFileStore,
    }
    for family in (RepositoryFactory.in_memory(), RepositoryFactory.supabase(FakeSupabase())):
        for name, port in ports.items():
            assert isinstance(getattr(family, name), port), f"{name} is not a {port.__name__}"


# --- Tasks -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks__list_pending__only_this_users_pending(repos):
    await _seed_tasks(repos, U1, [_row("a"), _row("b", status="completed")])
    await _seed_tasks(repos, U2, [_row("c")])

    pending = await repos.tasks.list_pending(U1)

    assert [t.task_id for t in pending] == ["a"]
    assert all(t.user_id == U1 and t.status is TaskStatus.PENDING for t in pending)


@pytest.mark.asyncio
async def test_tasks__update_status__expected_mismatch__raises_stale_version(repos):
    await _seed_tasks(repos, U1, [_row("a")])

    with pytest.raises(errors.StaleVersionError):
        await repos.tasks.update_status(U1, "a", TaskStatus.COMPLETED, expected=TaskStatus.SKIPPED)

    assert (await repos.tasks.get(U1, "a")).status is TaskStatus.PENDING
    done = await repos.tasks.update_status(U1, "a", TaskStatus.COMPLETED, expected=TaskStatus.PENDING)
    assert done.status is TaskStatus.COMPLETED
    assert (await repos.tasks.get(U1, "a")).status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_tasks__update_status__expected_start_moved__stale_and_unchanged(repos):
    """The slot sweep's compare-and-set: the status AND the slot it read. A task re-slotted
    meanwhile (same id, new start) is left alone. Starts compare as instants: the row
    holds the user's offset, the caller passes UTC."""
    slot = T0.astimezone(ZoneInfo("Asia/Kolkata"))
    await _seed_tasks(repos, U1, [_row("a", start=slot)])

    with pytest.raises(errors.StaleVersionError):
        await repos.tasks.update_status(
            U1, "a", TaskStatus.MISSED, expected=TaskStatus.PENDING,
            expected_start=T0 - timedelta(days=1),
        )

    assert (await repos.tasks.get(U1, "a")).status is TaskStatus.PENDING
    missed = await repos.tasks.update_status(
        U1, "a", TaskStatus.MISSED, expected=TaskStatus.PENDING, expected_start=T0
    )
    assert missed.status is TaskStatus.MISSED


@pytest.mark.asyncio
async def test_tasks__update_status__unknown_task__raises_not_found(repos):
    await _seed_tasks(repos, U2, [_row("theirs")])

    with pytest.raises(errors.NotFoundError):
        await repos.tasks.update_status(U1, "missing", TaskStatus.COMPLETED)
    with pytest.raises(errors.NotFoundError):
        await repos.tasks.update_status(U1, "theirs", TaskStatus.COMPLETED)

    assert (await repos.tasks.get(U2, "theirs")).status is TaskStatus.PENDING


@pytest.mark.asyncio
async def test_tasks__list_scheduled_between__window_filter(repos):
    await _seed_tasks(
        repos,
        U1,
        [
            _row("before", start=T0 - timedelta(minutes=1)),
            _row("at_start", start=T0),
            _row("inside", start=T0 + timedelta(hours=2)),
            _row("at_end", start=T0 + timedelta(hours=5)),
            _row("unscheduled"),
        ],
    )
    await _seed_tasks(repos, U2, [_row("theirs", start=T0 + timedelta(hours=1))])

    found = await repos.tasks.list_scheduled_between(U1, T0, T0 + timedelta(hours=5))

    assert [t.task_id for t in found] == ["at_start", "inside"]


@pytest.mark.asyncio
async def test_tasks__legacy_pacing_pushed__reads_as_skipped_and_matches_expected(repos):
    await _seed_tasks(repos, U1, [_row("pushed", status="pacing_pushed")])

    assert (await repos.tasks.get(U1, "pushed")).status is TaskStatus.SKIPPED
    done = await repos.tasks.update_status(
        U1, "pushed", TaskStatus.COMPLETED, expected=TaskStatus.SKIPPED
    )
    assert done.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_tasks__list_by_status__filters_and_isolates(repos):
    await _seed_tasks(repos, U1, [_row("old_missed", status="missed")])
    await _seed_tasks(
        repos,
        U1,
        [
            _row("pending"),
            _row("done", status="completed"),
            _row("new_missed", status="missed"),
            _row("pushed", status="pacing_pushed"),
        ],
    )
    await _seed_tasks(repos, U2, [_row("theirs", status="missed")])

    missed = await repos.tasks.list_by_status(U1, frozenset({TaskStatus.MISSED}))
    either = await repos.tasks.list_by_status(
        U1, frozenset({TaskStatus.PENDING, TaskStatus.MISSED})
    )
    skipped = await repos.tasks.list_by_status(U1, frozenset({TaskStatus.SKIPPED}))

    assert [t.task_id for t in missed] == ["new_missed", "old_missed"]  # newest first
    assert {t.task_id for t in either} == {"pending", "new_missed", "old_missed"}
    assert [t.task_id for t in skipped] == ["pushed"]  # a legacy status reads as its meaning
    assert all(t.user_id == U1 for t in either)
    assert await repos.tasks.list_by_status(U1, frozenset()) == []
    assert [t.task_id for t in await repos.tasks.list_by_status(
        U2, frozenset({TaskStatus.MISSED})
    )] == ["theirs"]


@pytest.mark.asyncio
async def test_tasks__list_for_user__newest_first_bounded_and_status_filtered(repos):
    # One accept each, oldest first (an accept replaces the user's pending tasks).
    await _seed_tasks(repos, U1, [_row("done", status="completed")])
    await _seed_tasks(repos, U1, [_row("set_aside", status="skipped")])
    await _seed_tasks(repos, U1, [_row("pushed", status="pacing_pushed")])
    await _seed_tasks(repos, U1, [_row("pending")])
    await _seed_tasks(repos, U2, [_row("theirs")])

    every = await repos.tasks.list_for_user(U1)
    skipped = await repos.tasks.list_for_user(U1, statuses=frozenset({TaskStatus.SKIPPED}))

    assert [t.task_id for t in every] == ["pending", "pushed", "set_aside", "done"]
    assert all(t.user_id == U1 for t in every)
    assert [t.task_id for t in skipped] == ["pushed", "set_aside"]  # legacy reads as its meaning
    assert await repos.tasks.list_for_user(U1, statuses=frozenset()) == []
    assert [t.task_id for t in await repos.tasks.list_for_user(U2)] == ["theirs"]

    # Past the bound, the newest TASK_LIST_LIMIT come back: only the oldest are left out.
    await _seed_tasks(
        repos, U1, [_row(f"bulk_{i:03}", status="completed") for i in range(TASK_LIST_LIMIT)]
    )
    bounded = await repos.tasks.list_for_user(U1)
    assert len(bounded) == TASK_LIST_LIMIT
    assert {t.task_id for t in bounded} == {f"bulk_{i:03}" for i in range(TASK_LIST_LIMIT)}


@pytest.mark.asyncio
async def test_tasks__get__carries_its_planned_criteria_keywords_and_if_then_plan(repos):
    intention = {"obstacle_trigger": "Phone", "behavioral_response": "Put it in the drawer"}
    await _seed_tasks(repos, U1, [
        {**_row("planned"), "topic_keywords": ["graphs", "bfs"], "implementation_intention": intention},
        {**_row("bare"), "completion_criteria": None},
    ])

    planned, bare = await repos.tasks.get(U1, "planned"), await repos.tasks.get(U1, "bare")

    assert (planned.completion_criteria, planned.topic_keywords, planned.implementation_intention) == (
        "Explain it back", ["graphs", "bfs"], intention,
    )
    assert (bare.completion_criteria, bare.topic_keywords, bare.implementation_intention) == (
        None, [], None,
    )
    assert await repos.tasks.get(U2, "planned") is None


@pytest.mark.asyncio
async def test_tasks__bring_back__missed_to_pending_deadline_cleared(repos):
    await _seed_tasks(repos, U1, [{**_row("set_aside", status="missed"), "deadline_hint": "2026-11-01"}])

    back = await repos.tasks.bring_back(U1, "set_aside")

    assert (back.task_id, back.status, back.deadline_hint) == ("set_aside", TaskStatus.PENDING, None)
    assert await repos.tasks.get(U1, "set_aside") == back
    with pytest.raises(errors.NotFoundError):
        await repos.tasks.bring_back(U2, "set_aside")  # another user's task is no task


@pytest.mark.asyncio
async def test_tasks__bring_back__not_missed__stale_version(repos):
    await _seed_tasks(repos, U1, [{**_row("planned"), "deadline_hint": "2026-11-01"}])

    with pytest.raises(errors.StaleVersionError):
        await repos.tasks.bring_back(U1, "planned")
    with pytest.raises(errors.NotFoundError):
        await repos.tasks.bring_back(U1, "missing")

    kept = await repos.tasks.get(U1, "planned")
    assert (kept.status, kept.deadline_hint) == (TaskStatus.PENDING, "2026-11-01")


# --- Task events -------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_events__append_then_list__ordered_by_time(repos):
    later = await repos.task_events.append(
        U1, _event(U1, TaskEventType.COMPLETED, T0 + timedelta(hours=1))
    )
    await repos.task_events.append(U1, _event(U1, TaskEventType.SCHEDULED, T0))
    await repos.task_events.append(U1, _event(U1, TaskEventType.SKIPPED, T0 + timedelta(hours=2)))

    events = await repos.task_events.list_for_user(U1)

    assert later.id is not None
    assert [e.occurred_at for e in events] == [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    assert later in events


@pytest.mark.asyncio
async def test_task_events__list__filters_type_and_since(repos):
    for offset, kind in [
        (0, TaskEventType.COMPLETED),
        (1, TaskEventType.COMPLETED),
        (2, TaskEventType.SKIPPED),
        (3, TaskEventType.MISSED),
    ]:
        await repos.task_events.append(U1, _event(U1, kind, T0 + timedelta(hours=offset)))

    events = await repos.task_events.list_for_user(
        U1,
        since=T0 + timedelta(hours=1),
        types={TaskEventType.COMPLETED, TaskEventType.MISSED},
    )

    assert [(e.event_type, e.occurred_at) for e in events] == [
        (TaskEventType.COMPLETED, T0 + timedelta(hours=1)),
        (TaskEventType.MISSED, T0 + timedelta(hours=3)),
    ]


@pytest.mark.asyncio
async def test_task_events__other_user__invisible(repos):
    await repos.task_events.append(U1, _event(U1, TaskEventType.COMPLETED, T0))

    assert await repos.task_events.list_for_user(U2) == []


@pytest.mark.asyncio
async def test_task_events__list__filters_task_ids(repos):
    for task_id, offset in [("a", 0), ("b", 1), ("a", 2), ("c", 3)]:
        at = T0 + timedelta(hours=offset)
        await repos.task_events.append(U1, _event(U1, TaskEventType.SCHEDULED, at, task_id))
    await repos.task_events.append(U2, _event(U2, TaskEventType.SCHEDULED, T0, "a"))

    events = await repos.task_events.list_for_user(U1, task_ids={"a", "c"})

    assert [(e.task_id, e.occurred_at) for e in events] == [
        ("a", T0),
        ("a", T0 + timedelta(hours=2)),
        ("c", T0 + timedelta(hours=3)),
    ]
    assert await repos.task_events.list_for_user(U1, task_ids=set()) == []


@pytest.mark.asyncio
async def test_task_events__list__read_cap__drops_oldest_never_newest(repos, fake_client):
    """PostgREST answers at most max_rows rows per read (supabase/config.toml).

    Whatever a family leaves out of one read, it is the oldest events: the newest
    always come back, still oldest first. The cap only binds the Supabase family.
    """
    fake_client.max_rows = 3
    times = [T0 + timedelta(minutes=m) for m in range(5)]
    for m, at in enumerate(times):
        await repos.task_events.append(U1, _event(U1, TaskEventType.SCHEDULED, at, f"t{m}"))

    returned = [e.occurred_at for e in await repos.task_events.list_for_user(U1)]

    assert len(returned) >= 3
    assert returned == times[-len(returned):]


@pytest.mark.asyncio
async def test_task_events__append_many__stores_all_with_ids(repos):
    events = [
        _event(U1, TaskEventType.SCHEDULED, T0, "a"),
        _event(U1, TaskEventType.RESCHEDULED, T0 + timedelta(minutes=1), "b"),
    ]

    stored = await repos.task_events.append_many(U1, events)

    assert [(e.task_id, e.event_type) for e in stored] == [
        ("a", TaskEventType.SCHEDULED),
        ("b", TaskEventType.RESCHEDULED),
    ]
    assert all(e.id for e in stored) and len({e.id for e in stored}) == 2
    assert await repos.task_events.list_for_user(U1) == stored
    assert await repos.task_events.append_many(U1, []) == []


@pytest.mark.asyncio
async def test_task_events__append_many__one_bad_event__raises_validation_and_writes_nothing(repos):
    events = [
        _event(U1, TaskEventType.SCHEDULED, T0, "a"),
        _event(U2, TaskEventType.SCHEDULED, T0, "b"),  # another user's
    ]

    with pytest.raises(errors.ValidationError):
        await repos.task_events.append_many(U1, events)

    assert await repos.task_events.list_for_user(U1) == []
    assert await repos.task_events.list_for_user(U2) == []


# --- Ownership ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_repos__entity_for_other_user__raises_validation_and_writes_nothing(repos):
    with pytest.raises(errors.ValidationError):
        await repos.task_events.append(U1, _event(U2, TaskEventType.COMPLETED, T0))
    with pytest.raises(errors.ValidationError):
        await repos.constraints.add(U1, _constraint(U2))
    with pytest.raises(errors.ValidationError):
        await repos.suggestions.add(U1, _suggestion(U2))

    for user_id in (U1, U2):
        assert await repos.task_events.list_for_user(user_id) == []
        assert await repos.constraints.list_active(user_id) == []
        assert await repos.suggestions.list(user_id) == []


# --- Drafts ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drafts__create_get__roundtrip(repos):
    created = await _new_draft(repos)

    got = await repos.drafts.get(U1, created.id)

    assert got == created
    assert got.status is DraftStatus.PENDING
    assert got.goal_id == "g1" and got.horizon_start == T0.isoformat()
    assert got.tasks == [{"task_id": "g1_t1", "title": "Read chapter 1", "duration_minutes": 25}]
    assert got.schedule == {"g1_t1": {"start_min": 0, "end_min": 25}}
    assert got.expires_at is not None and got.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_drafts__replace_tasks__persists(repos):
    draft = await _new_draft(repos)
    revised = [{"task_id": "g1_t2", "title": "Practice set", "duration_minutes": 20}]

    updated = await repos.drafts.replace_tasks(U1, draft.id, revised)

    assert updated.tasks == revised
    assert (await repos.drafts.get(U1, draft.id)).tasks == revised
    with pytest.raises(errors.NotFoundError):
        await repos.drafts.replace_tasks(U2, draft.id, [])


@pytest.mark.asyncio
async def test_drafts__replace_schedule__open_draft_only_and_user_scoped(repos):
    draft = await _new_draft(repos)
    moved = {"g1_t1": {"start_min": 200, "end_min": 225}}

    updated = await repos.drafts.replace_schedule(U1, draft.id, moved)

    assert updated.schedule == moved and updated.status is draft.status
    assert (await repos.drafts.get(U1, draft.id)).schedule == moved
    with pytest.raises(errors.NotFoundError):
        await repos.drafts.replace_schedule(U2, draft.id, {})
    await repos.drafts.accept_atomic(U1, draft.id, [_row("x")])
    with pytest.raises(errors.InvalidStateTransition):
        await repos.drafts.replace_schedule(U1, draft.id, {"g1_t1": {"start_min": 0, "end_min": 25}})
    assert (await repos.drafts.get(U1, draft.id)).schedule == moved


@pytest.mark.asyncio
async def test_drafts__caller_mutates_tasks_after_write__stored_draft_unchanged(repos):
    created_tasks = [{"task_id": "g1_t1", "title": "Read chapter 1"}]
    draft = await repos.drafts.create(
        U1, tasks=created_tasks, horizon_start=T0.isoformat(), goal_id="g1", schedule=None
    )
    created_tasks[0]["title"] = "mutated after create"
    assert (await repos.drafts.get(U1, draft.id)).tasks == [
        {"task_id": "g1_t1", "title": "Read chapter 1"}
    ]

    revised = [{"task_id": "g1_t2", "title": "Practice set"}]
    await repos.drafts.replace_tasks(U1, draft.id, revised)
    revised[0]["title"] = "mutated after replace"
    revised.append({"task_id": "g1_t3", "title": "Sneaked in"})

    assert (await repos.drafts.get(U1, draft.id)).tasks == [
        {"task_id": "g1_t2", "title": "Practice set"}
    ]


@pytest.mark.asyncio
async def test_drafts__get_open__returns_modified_draft(repos):
    draft = await _new_draft(repos)
    await repos.drafts.set_status(U1, draft.id, DraftStatus.MODIFIED)

    open_draft = await repos.drafts.get_open(U1)

    assert open_draft is not None
    assert open_draft.id == draft.id and open_draft.status is DraftStatus.MODIFIED
    assert await repos.drafts.get_open(U2) is None


@pytest.mark.asyncio
async def test_drafts__get_open__two_open_drafts__returns_newest(repos):
    older = await _new_draft(repos)
    newer = await _new_draft(repos)
    await repos.drafts.set_status(U1, older.id, DraftStatus.MODIFIED)

    assert (await repos.drafts.get_open(U1)).id == newer.id

    await repos.drafts.set_status(U1, newer.id, DraftStatus.REJECTED)
    assert (await repos.drafts.get_open(U1)).id == older.id


@pytest.mark.asyncio
async def test_drafts__set_status__illegal__raises_invalid_state_transition(repos):
    draft = await _new_draft(repos)
    await repos.drafts.set_status(U1, draft.id, DraftStatus.REJECTED, reason="Too much at once")

    with pytest.raises(errors.InvalidStateTransition):
        await repos.drafts.set_status(U1, draft.id, DraftStatus.ACCEPTED)

    stored = await repos.drafts.get(U1, draft.id)
    assert stored.status is DraftStatus.REJECTED
    assert stored.rejection_reason == "Too much at once"
    assert await repos.drafts.get_open(U1) is None


@pytest.mark.asyncio
async def test_drafts__other_user_get__none(repos):
    draft = await _new_draft(repos)

    assert await repos.drafts.get(U2, draft.id) is None
    assert await repos.drafts.get(U1, str(uuid4())) is None
    assert await repos.drafts.get(U1, "not-a-uuid") is None
    with pytest.raises(errors.NotFoundError):
        await repos.drafts.set_status(U2, draft.id, DraftStatus.REJECTED)


@pytest.mark.asyncio
async def test_drafts__accept_atomic__writes_rows_and_marks_accepted(repos):
    await _seed_tasks(repos, U1, [_row("old"), _row("done", status="completed")])
    draft = await _new_draft(repos)

    written = await repos.drafts.accept_atomic(U1, draft.id, [_row("x"), _row("y")])

    assert written == 2
    assert sorted(t.task_id for t in await repos.tasks.list_pending(U1)) == ["x", "y"]
    assert (await repos.tasks.get(U1, "done")).status is TaskStatus.COMPLETED
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.ACCEPTED


@pytest.mark.asyncio
async def test_drafts__accept_atomic__second_call__raises_invalid_state_transition_and_no_duplicates(
    repos,
):
    draft = await _new_draft(repos)
    rows = [_row("x"), _row("y")]
    assert await repos.drafts.accept_atomic(U1, draft.id, rows) == 2

    with pytest.raises(errors.InvalidStateTransition):
        await repos.drafts.accept_atomic(U1, draft.id, rows)

    assert sorted(t.task_id for t in await repos.tasks.list_pending(U1)) == ["x", "y"]


@pytest.mark.asyncio
async def test_drafts__accept_atomic__concurrent_calls__exactly_one_succeeds(repos):
    draft = await _new_draft(repos)
    rows = [_row("x"), _row("y")]

    results = await asyncio.gather(
        repos.drafts.accept_atomic(U1, draft.id, rows),
        repos.drafts.accept_atomic(U1, draft.id, rows),
        return_exceptions=True,
    )

    assert sorted(type(r).__name__ for r in results) == ["InvalidStateTransition", "int"]
    assert len(await repos.tasks.list_pending(U1)) == 2


@pytest.mark.asyncio
async def test_drafts__accept_atomic__concurrent_task_edit__edit_lands_first_or_is_refused(repos):
    """An edit racing an accept either lands before it (and the accept does not write
    stale tasks back over it) or is refused because the draft has closed."""
    draft = await _new_draft(repos)
    edited = [{"task_id": "g1_t9", "title": "Edited while accepting", "duration_minutes": 10}]

    accepted, edit = await asyncio.gather(
        repos.drafts.accept_atomic(U1, draft.id, [_row("x")]),
        repos.drafts.replace_tasks(U1, draft.id, edited),
        return_exceptions=True,
    )

    assert accepted == 1
    stored = await repos.drafts.get(U1, draft.id)
    assert stored.status is DraftStatus.ACCEPTED
    if isinstance(edit, BaseException):
        assert isinstance(edit, errors.InvalidStateTransition)
        assert stored.tasks == draft.tasks
    else:
        assert stored.tasks == edited


@pytest.mark.asyncio
async def test_drafts__replace_tasks__accepted_draft__raises_invalid_state_transition(repos):
    draft = await _new_draft(repos)
    await repos.drafts.accept_atomic(U1, draft.id, [_row("x")])

    with pytest.raises(errors.InvalidStateTransition):
        await repos.drafts.replace_tasks(U1, draft.id, [{"task_id": "g1_t9", "title": "Late edit"}])

    assert (await repos.drafts.get(U1, draft.id)).tasks == draft.tasks


@pytest.mark.asyncio
async def test_drafts__accept_atomic__two_open_drafts__the_other_is_superseded(repos):
    """Parity with the accept_draft SQL function: the accepted plan replaces the others."""
    first = await _new_draft(repos)
    second = await _new_draft(repos)
    other_users = await repos.drafts.create(
        U2, tasks=[], horizon_start=T0.isoformat(), goal_id="g1", schedule=None
    )

    await repos.drafts.accept_atomic(U1, first.id, [_row("x")])

    assert (await repos.drafts.get(U1, second.id)).status is DraftStatus.SUPERSEDED
    assert (await repos.drafts.get(U2, other_users.id)).status is DraftStatus.PENDING
    with pytest.raises(errors.InvalidStateTransition):
        await repos.drafts.accept_atomic(U1, second.id, [_row("y")])
    assert [t.task_id for t in await repos.tasks.list_pending(U1)] == ["x"]


@pytest.mark.asyncio
async def test_drafts__accept_atomic__expired__raises(repos, expire_draft):
    draft = await _new_draft(repos)
    expire_draft(draft.id)

    with pytest.raises(errors.InvalidStateTransition):
        await repos.drafts.accept_atomic(U1, draft.id, [_row("x")])

    assert await repos.tasks.list_pending(U1) == []
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.PENDING


@pytest.mark.asyncio
async def test_drafts__accept_atomic__missing_or_other_users_draft__raises_not_found(repos):
    draft = await _new_draft(repos)

    with pytest.raises(errors.NotFoundError):
        await repos.drafts.accept_atomic(U1, str(uuid4()), [_row("x")])
    with pytest.raises(errors.NotFoundError):
        await repos.drafts.accept_atomic(U2, draft.id, [_row("x")])

    assert await repos.tasks.list_pending(U2) == []
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.PENDING


@pytest.mark.asyncio
async def test_drafts__accept_atomic__datetime_valued_row__stored_as_its_instant(repos):
    draft = await _new_draft(repos)
    row = {
        **_row("x"),
        "scheduled_start": T0,
        "scheduled_end": T0 + timedelta(minutes=25),
        "created_at": datetime.now(timezone.utc),
    }

    assert await repos.drafts.accept_atomic(U1, draft.id, [row]) == 1

    task = await repos.tasks.get(U1, "x")
    assert (task.scheduled_start, task.scheduled_end) == (T0, T0 + timedelta(minutes=25))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_row",
    [{**_row("x"), "user_id": U2}, {**_row("x"), "id": "not-a-uuid"}],
    ids=["other_users_row", "non_uuid_id"],
)
async def test_drafts__accept_atomic__row_for_other_user_or_bad_id__raises_validation_and_writes_nothing(  # noqa: E501
    repos, bad_row
):
    await _seed_tasks(repos, U1, [_row("existing")])
    draft = await _new_draft(repos)

    with pytest.raises(errors.ValidationError):
        await repos.drafts.accept_atomic(U1, draft.id, [_row("y"), bad_row])

    assert [t.task_id for t in await repos.tasks.list_pending(U1)] == ["existing"]
    assert await repos.tasks.list_pending(U2) == []
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [[_row("y"), _row("done")], [_row("y"), _row("y")]],
    ids=["id_a_finished_task_holds", "id_twice_in_one_plan"],
)
async def test_drafts__accept_atomic__task_id_already_taken__raises_conflict_and_writes_nothing(
    repos, rows
):
    """user_tasks is UNIQUE (user_id, task_id), and accept deletes only pending rows.

    A finished task keeps its id, so a plan that reuses it, or repeats an id, can
    never be written. That is a conflict with the user's tasks, not an outage.
    """
    await _seed_tasks(repos, U1, [_row("done", status="completed"), _row("open")])
    draft = await _new_draft(repos)

    with pytest.raises(errors.ConflictError) as raised:
        await repos.drafts.accept_atomic(U1, draft.id, rows)

    assert type(raised.value) is errors.ConflictError  # not InvalidStateTransition
    assert raised.value.message == "That plan clashes with tasks you already have."
    assert [t.task_id for t in await repos.tasks.list_pending(U1)] == ["open"]
    assert (await repos.tasks.get(U1, "done")).status is TaskStatus.COMPLETED
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.PENDING


@pytest.mark.asyncio
async def test_drafts__accept_atomic__reslots_missed_row_with_same_task_id(repos):
    """A task whose slot passed comes back in a new plan under its own id.

    Accept replaces the user's missed row for that id, so the unique key does not
    clash; the row is pending again, with the new slot. Another user's row is untouched.
    """
    await _seed_tasks(repos, U1, [_row("m", status="missed", start=T0)])
    await _seed_tasks(repos, U2, [_row("m", status="missed", start=T0)])
    draft = await _new_draft(repos)
    later = T0 + timedelta(days=1)

    assert await repos.drafts.accept_atomic(U1, draft.id, [_row("m", start=later)]) == 1

    task = await repos.tasks.get(U1, "m")
    assert task.status is TaskStatus.PENDING
    assert (task.scheduled_start, task.scheduled_end) == (later, later + timedelta(minutes=25))
    window = (T0 - timedelta(days=1), T0 + timedelta(days=2))
    assert [t.task_id for t in await repos.tasks.list_scheduled_between(U1, *window)] == ["m"]
    assert (await repos.tasks.get(U2, "m")).status is TaskStatus.MISSED


@pytest.mark.asyncio
@pytest.mark.parametrize("taken", ["done", "s"], ids=["completed_id", "skipped_id"])
async def test_drafts__accept_atomic__reslot_with_taken_id__raises_conflict_and_writes_nothing(
    repos, taken
):
    """The re-slot delete is part of the one transaction: a clash elsewhere in the
    plan undoes it, so the missed row is still there, still missed, slot unchanged."""
    await _seed_tasks(
        repos,
        U1,
        [
            _row("m", status="missed", start=T0),
            _row("done", status="completed"),
            _row("s", status="skipped"),
            _row("open"),
        ],
    )
    draft = await _new_draft(repos)

    with pytest.raises(errors.ConflictError):
        await repos.drafts.accept_atomic(
            U1, draft.id, [_row("m", start=T0 + timedelta(days=1)), _row(taken)]
        )

    kept = await repos.tasks.get(U1, "m")
    assert (kept.status, kept.scheduled_start) == (TaskStatus.MISSED, T0)
    assert [t.task_id for t in await repos.tasks.list_pending(U1)] == ["open"]
    assert (await repos.drafts.get(U1, draft.id)).status is DraftStatus.PENDING


@pytest.mark.asyncio
async def test_drafts__accept_atomic__unrelated_missed_row_kept(repos):
    """Only the missed rows the plan re-slots are replaced; the others stay set aside."""
    await _seed_tasks(repos, U1, [_row("m", status="missed", start=T0), _row("open")])
    draft = await _new_draft(repos)

    assert await repos.drafts.accept_atomic(U1, draft.id, [_row("x")]) == 1

    kept = await repos.tasks.get(U1, "m")
    assert (kept.status, kept.scheduled_start) == (TaskStatus.MISSED, T0)
    assert [t.task_id for t in await repos.tasks.list_pending(U1)] == ["x"]


@pytest.mark.asyncio
async def test_drafts__accept_atomic__not_before__read_back_as_that_instant(repos):
    """A task's earliest start survives the accept: list_pending and get return it."""
    held = datetime(2026, 11, 17, 18, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    draft = await _new_draft(repos)

    await repos.drafts.accept_atomic(
        U1, draft.id, [{**_row("held"), "not_before": held.isoformat()}, _row("free")]
    )

    pending = {t.task_id: t for t in await repos.tasks.list_pending(U1)}
    assert pending["held"].not_before == held
    assert pending["held"].not_before.utcoffset() is not None
    assert pending["free"].not_before is None
    assert (await repos.tasks.get(U1, "held")).not_before == held


# --- Constraints -------------------------------------------------------------


@pytest.mark.asyncio
async def test_constraints__add_list_delete__user_scoped(repos):
    said = await repos.constraints.add(U1, _constraint(U1))
    inferred = await repos.constraints.add(U1, _constraint(U1, source="inferred"))

    assert sorted(c.id for c in await repos.constraints.list_active(U1)) == sorted(
        [said.id, inferred.id]
    )
    assert inferred in await repos.constraints.list_active(U1)
    assert await repos.constraints.list_active(U2) == []
    assert await repos.constraints.delete(U2, said.id) is False
    assert await repos.constraints.delete(U1, said.id) is True
    assert await repos.constraints.delete(U1, said.id) is False
    assert await repos.constraints.list_active(U1) == [inferred]


@pytest.mark.asyncio
async def test_constraints__set_semantics__replaces_only_the_users_rule(repos):
    rule = await repos.constraints.add(U1, _constraint(U1))
    other = await repos.constraints.add(U1, _constraint(U1))
    semantics = {"start_min": 1320, "end_min": 1440, "memory_id": str(uuid4())}

    updated = await repos.constraints.set_semantics(U1, rule.id, semantics)

    assert updated == rule.model_copy(update={"structured_semantics": semantics})
    listed = {c.id: c for c in await repos.constraints.list_active(U1)}
    assert listed[rule.id].structured_semantics == semantics
    assert listed[other.id] == other
    assert await repos.constraints.set_semantics(U2, rule.id, {}) is None  # another user's
    assert await repos.constraints.set_semantics(U1, str(uuid4()), {}) is None
    assert await repos.constraints.set_semantics(U1, "not-a-uuid", {}) is None
    assert {c.id: c for c in await repos.constraints.list_active(U1)}[rule.id] == updated


@pytest.mark.asyncio
async def test_constraints__add__store_stamps_created_at(repos):
    before = datetime.now(timezone.utc)

    added = await repos.constraints.add(U1, _constraint(U1))

    assert added.created_at is not None and added.created_at >= before
    (listed,) = await repos.constraints.list_active(U1)
    assert listed.created_at == added.created_at


@pytest.mark.asyncio
async def test_constraints__list_active__read_cap__drops_oldest_never_newest(repos, fake_client):
    """Whatever PostgREST's row cap leaves out of the read, it is the oldest rules: the
    newest always come back, still oldest first. The cap only binds the Supabase family."""
    fake_client.max_rows = 3
    times = [T0 + timedelta(minutes=m) for m in range(5)]
    for at in times:
        await repos.constraints.add(U1, _constraint(U1).model_copy(update={"created_at": at}))

    returned = [c.created_at for c in await repos.constraints.list_active(U1)]

    assert len(returned) >= 3
    assert returned == times[-len(returned):]


# --- Goals -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goals__latest_context__newest_update_for_that_goal_only(repos):
    await repos.goals.add_deadline_update(U1, _plan_update(U1, "g1", "first", at=T0))
    await repos.goals.add_deadline_update(
        U1, _plan_update(U1, "g1", "second", at=T0 + timedelta(hours=1))
    )
    await repos.goals.add_deadline_update(
        U1, _plan_update(U1, "g2", "another goal", at=T0 + timedelta(hours=2))
    )
    await repos.goals.add_deadline_update(
        U2, _plan_update(U2, "g1", "their goal", at=T0 + timedelta(hours=3))
    )

    assert await repos.goals.latest_context(U1, "g1") == "second"
    assert await repos.goals.latest_context(U1, "g2") == "another goal"
    assert await repos.goals.latest_context(U1, "unknown") is None
    assert await repos.goals.latest_context(U2, "g2") is None


@pytest.mark.asyncio
async def test_goals__add_deadline_update__user_scoped(repos, seed_goal):
    with pytest.raises(errors.ValidationError):
        await repos.goals.add_deadline_update(U1, _plan_update(U2, "g1", "not U1's"))
    assert await repos.goals.latest_context(U2, "g1") is None  # nothing was written

    await repos.goals.add_deadline_update(U1, _plan_update(U1, "g1", "mine"))

    assert await repos.goals.latest_context(U1, "g1") == "mine"
    assert await repos.goals.latest_context(U2, "g1") is None
    thesis = seed_goal(Goal(id="g1", user_id=U1, title="Thesis"))
    assert await repos.goals.get(U1, "g1") == thesis
    assert await repos.goals.get(U2, "g1") is None


# --- Habit trackers ------------------------------------------------------------


@pytest.mark.asyncio
async def test_trackers__record_review__other_users_tracker__not_found(repos, seed_tracker):
    mine = seed_tracker(U1, repetitions=2, ef=2.5, next_interval_days=6)
    review = {"repetitions": 3, "ef": 2.6, "next_interval_days": 15, "quality": 5, "done_at": T0}

    for user_id, tracker_id in [(U2, mine.id), (U1, str(uuid4())), (U1, "not-a-uuid")]:
        with pytest.raises(errors.NotFoundError):
            await repos.habit_trackers.record_review(user_id, tracker_id, **review)

    assert await repos.habit_trackers.get(U1, mine.id) == mine  # untouched
    assert await repos.habit_trackers.get(U2, mine.id) is None
    done = await repos.habit_trackers.record_review(U1, mine.id, **review)
    assert done == mine.model_copy(update={
        "repetitions": 3, "ef": 2.6, "next_interval_days": 15, "quality_last": 5,
        "last_done_at": T0,
    })
    assert await repos.habit_trackers.get(U1, mine.id) == done


@pytest.mark.asyncio
async def test_trackers__list_for_user__only_theirs(repos, seed_tracker):
    first = seed_tracker(U1)
    second = seed_tracker(U1, last_done_at=T0, repetitions=1)
    seed_tracker(U2)

    listed = await repos.habit_trackers.list_for_user(U1)

    assert listed == [second, first]  # newest first
    assert await repos.habit_trackers.list_for_user("nobody") == []


@pytest.mark.asyncio
async def test_trackers__add_for_constraint__one_per_user_and_rule_counting_from_started_at(repos):
    rule = str(uuid4())
    learned = T0 - timedelta(days=3)

    first = await repos.habit_trackers.add_for_constraint(U1, rule, started_at=learned)
    again = await repos.habit_trackers.add_for_constraint(U1, rule, started_at=T0)
    theirs = await repos.habit_trackers.add_for_constraint(U2, rule, started_at=T0)

    assert again == first  # the one already there, nothing written
    assert (first.user_id, first.constraint_id, first.last_done_at) == (U1, rule, learned)
    assert (first.repetitions, first.quality_last, first.next_interval_days) == (0, None, 1)
    assert first.ef == 2.5 and first.habit_id is None
    assert theirs.id != first.id and theirs.user_id == U2
    assert await repos.habit_trackers.list_for_user(U1) == [first]
    assert await repos.habit_trackers.get(U1, first.id) == first


@pytest.mark.asyncio
async def test_trackers__add_for_constraint__id_not_a_uuid__validation_error_nothing_stored(repos):
    with pytest.raises(errors.ValidationError):
        await repos.habit_trackers.add_for_constraint(U1, "rule-1", started_at=T0)

    assert await repos.habit_trackers.list_for_user(U1) == []


@pytest.mark.asyncio
async def test_trackers__delete_for_constraint__only_this_users_trackers_of_that_rule(
    repos, seed_tracker
):
    rule, other_rule = str(uuid4()), str(uuid4())
    await repos.habit_trackers.add_for_constraint(U1, rule, started_at=T0)
    seed_tracker(U1, constraint_id=rule)  # a second one, as a race between processes leaves
    kept = await repos.habit_trackers.add_for_constraint(U1, other_rule, started_at=T0)
    theirs = await repos.habit_trackers.add_for_constraint(U2, rule, started_at=T0)

    assert await repos.habit_trackers.delete_for_constraint(U1, rule) == 2

    assert await repos.habit_trackers.list_for_user(U1) == [kept]
    assert await repos.habit_trackers.list_for_user(U2) == [theirs]
    assert await repos.habit_trackers.delete_for_constraint(U1, rule) == 0
    assert await repos.habit_trackers.delete_for_constraint(U1, "not-a-uuid") == 0


# --- Profiles ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_profiles__get_or_create__defaults(repos):
    profile = await repos.profiles.get_or_create(U1)

    assert profile.user_id == U1
    assert profile.autonomy_mode is AutonomyMode.ASK_FIRST
    assert profile.wake_time == time(7, 0)
    assert profile.timezone == "Asia/Kolkata"
    assert await repos.profiles.get_or_create(U1) == profile
    assert await repos.profiles.list_user_ids() == [U1]


@pytest.mark.asyncio
async def test_profiles__update__persists(repos):
    await repos.profiles.get_or_create(U1)

    updated = await repos.profiles.update(
        U1, autonomy_mode=AutonomyMode.AUTO_LOW_RISK, wake_time=time(6, 30), display_name="Tony"
    )

    assert await repos.profiles.get_or_create(U1) == updated
    assert updated.autonomy_mode is AutonomyMode.AUTO_LOW_RISK
    assert updated.wake_time == time(6, 30) and updated.display_name == "Tony"
    assert (await repos.profiles.get_or_create(U2)).autonomy_mode is AutonomyMode.ASK_FIRST
    with pytest.raises(errors.ValidationError):
        await repos.profiles.update(U1, favourite_colour="red")
    with pytest.raises(errors.ValidationError):
        await repos.profiles.update(U1, autonomy_mode="reckless")


# --- Suggestions -------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggestions__add_list_by_status(repos):
    first = await repos.suggestions.add(U1, _suggestion(U1, created_at=T0))
    second = await repos.suggestions.add(U1, _suggestion(U1, created_at=T0 + timedelta(hours=1)))
    await repos.suggestions.add(U2, _suggestion(U2))

    dismissed = await repos.suggestions.set_status(U1, second.id, SuggestionStatus.DISMISSED)

    assert first.id is not None and dismissed.status is SuggestionStatus.DISMISSED
    assert await repos.suggestions.list(U1, status=SuggestionStatus.PENDING) == [first]
    assert [s.id for s in await repos.suggestions.list(U1)] == [second.id, first.id]
    assert await repos.suggestions.get(U1, second.id) == dismissed
    assert await repos.suggestions.get(U2, first.id) is None
    with pytest.raises(errors.NotFoundError):
        await repos.suggestions.set_status(U2, first.id, SuggestionStatus.ACCEPTED)


@pytest.mark.asyncio
async def test_suggestions__exists_today__true_after_add(repos):
    day = T0.date()
    assert await repos.suggestions.exists_today(U1, "evening_review", day) is False

    await repos.suggestions.add(U1, _suggestion(U1, trigger="evening_review", created_at=T0))

    assert await repos.suggestions.exists_today(U1, "evening_review", day) is True
    assert await repos.suggestions.exists_today(U1, "evening_review", day + timedelta(days=1)) is False
    assert await repos.suggestions.exists_today(U1, "morning_brief", day) is False
    assert await repos.suggestions.exists_today(U2, "evening_review", day) is False


@pytest.mark.asyncio
async def test_suggestions__exists_today__expired_by_a_newer_plan_still_counts_no_fit_does_not(
    repos,
):
    """Replaced by a newer plan (out of date), today's brief was still made: no second
    one that day, even after a restart. One that expired because its plan no longer fit
    leaves room for a fresh one."""
    from app.domain.entities import SuggestionExpiry

    replaced = await repos.suggestions.add(U1, _suggestion(U1, trigger="morning_brief"))
    moved = await repos.suggestions.set_status(
        U1, replaced.id, SuggestionStatus.EXPIRED, expected=SuggestionStatus.PENDING,
        expired_because=SuggestionExpiry.OUT_OF_DATE,
    )
    no_fit = await repos.suggestions.add(U2, _suggestion(U2, trigger="morning_brief"))
    await repos.suggestions.set_status(
        U2, no_fit.id, SuggestionStatus.EXPIRED, expected=SuggestionStatus.PENDING,
        expired_because=SuggestionExpiry.NO_FIT,
    )

    assert moved.action == {**replaced.action, "expired_because": "out_of_date"}
    assert await repos.suggestions.exists_today(U1, "morning_brief", T0.date()) is True
    assert await repos.suggestions.exists_today(U2, "morning_brief", T0.date()) is False
    stored = await repos.suggestions.get(U2, no_fit.id)
    assert stored.action["expired_because"] == "no_fit" and stored.status is SuggestionStatus.EXPIRED


@pytest.mark.asyncio
async def test_suggestions__exists_today__expired_suggestion_does_not_count(repos):
    """A brief that expired (its plan no longer fit) leaves room for a fresh one that day."""
    stored = await repos.suggestions.add(U1, _suggestion(U1, trigger="morning_brief"))
    await repos.suggestions.set_status(
        U1, stored.id, SuggestionStatus.EXPIRED, expected=SuggestionStatus.PENDING
    )

    assert await repos.suggestions.exists_today(U1, "morning_brief", T0.date()) is False
    await repos.suggestions.add(U1, _suggestion(U1, trigger="morning_brief"))
    assert await repos.suggestions.exists_today(U1, "morning_brief", T0.date()) is True


@pytest.mark.asyncio
async def test_suggestions__exists_today__ist_half_past_midnight_is_new_local_day(repos):
    """00:30 on 15 November in India is 19:00 on 14 November in UTC.

    Read on the user's clock it belongs to the 15th, so a brief made then is the
    15th's, and the 14th's brief (made at 23:30 IST) does not count for the 15th.
    """
    ist = "Asia/Kolkata"
    late_on_14th = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)  # 23:30 IST, 14th
    await repos.suggestions.add(U1, _suggestion(U1, trigger="morning_brief", created_at=late_on_14th))
    day_14, day_15 = late_on_14th.date(), late_on_14th.date() + timedelta(days=1)

    assert await repos.suggestions.exists_today(U1, "morning_brief", day_15, tz=ist) is False

    half_past_midnight = datetime(2026, 11, 14, 19, 0, tzinfo=timezone.utc)  # 00:30 IST, 15th
    await repos.suggestions.add(
        U1, _suggestion(U1, trigger="morning_brief", created_at=half_past_midnight)
    )

    assert await repos.suggestions.exists_today(U1, "morning_brief", day_15, tz=ist) is True
    assert await repos.suggestions.exists_today(U1, "morning_brief", day_14, tz=ist) is True
    # Both fall on the 14th by the UTC date, and neither on the 15th.
    assert await repos.suggestions.exists_today(U1, "morning_brief", day_15) is False


@pytest.mark.asyncio
async def test_suggestions__set_status_expected_mismatch__stale_version(repos):
    stored = await repos.suggestions.add(U1, _suggestion(U1))

    claimed = await repos.suggestions.set_status(
        U1, stored.id, SuggestionStatus.ACCEPTED, expected=SuggestionStatus.PENDING
    )
    with pytest.raises(errors.StaleVersionError):
        await repos.suggestions.set_status(
            U1, stored.id, SuggestionStatus.DISMISSED, expected=SuggestionStatus.PENDING
        )
    with pytest.raises(errors.NotFoundError):
        await repos.suggestions.set_status(
            U2, stored.id, SuggestionStatus.ACCEPTED, expected=SuggestionStatus.PENDING
        )

    assert claimed.status is SuggestionStatus.ACCEPTED
    assert (await repos.suggestions.get(U1, stored.id)).status is SuggestionStatus.ACCEPTED


@pytest.mark.asyncio
async def test_suggestions__consent_kind__round_trips(repos):
    typed = _suggestion(U1).model_copy(update={"consent_kind": ConsentReasonKind.RULE_MATCH})

    stored = await repos.suggestions.add(U1, typed)
    untyped = await repos.suggestions.add(U1, _suggestion(U1))

    assert (await repos.suggestions.get(U1, stored.id)).consent_kind is ConsentReasonKind.RULE_MATCH
    assert (await repos.suggestions.get(U1, untyped.id)).consent_kind is None


@pytest.mark.asyncio
async def test_suggestions__list__limit_and_read_cap__keep_the_newest(repos, fake_client):
    fake_client.max_rows = 3
    times = [T0 + timedelta(minutes=m) for m in range(5)]
    for at in times:
        await repos.suggestions.add(U1, _suggestion(U1, created_at=at))

    capped = [s.created_at for s in await repos.suggestions.list(U1)]
    limited = [s.created_at for s in await repos.suggestions.list(U1, limit=2)]

    assert capped == list(reversed(times))[: len(capped)] and len(capped) >= 3
    assert limited == [times[4], times[3]]
    with pytest.raises(errors.ValidationError):
        await repos.suggestions.list(U1, limit=-1)


@pytest.mark.asyncio
async def test_suggestions__set_status_with_consent__rewrites_the_decision(repos):
    claimed = _suggestion(U1).model_copy(
        update={"status": SuggestionStatus.ACCEPTED, "consent_behavior": "allow"}
    )
    stored = await repos.suggestions.add(U1, claimed)
    asking = ConsentDecision(
        behavior="ask", kind=ConsentReasonKind.MODE_DECISION, message="Asking first."
    )

    reopened = await repos.suggestions.set_status(
        U1, stored.id, SuggestionStatus.PENDING, expected=SuggestionStatus.ACCEPTED, consent=asking
    )

    assert reopened.status is SuggestionStatus.PENDING
    assert (reopened.consent_behavior, reopened.consent_kind, reopened.consent_reason) == (
        "ask", ConsentReasonKind.MODE_DECISION, "Asking first."
    )
    assert await repos.suggestions.get(U1, stored.id) == reopened


@pytest.mark.asyncio
async def test_suggestions__dedupe_keys__newest_first_one_status_one_user_one_column(
    repos, fake_client
):
    rows = [(0, "a", SuggestionStatus.DISMISSED), (1, "b", SuggestionStatus.PENDING),
            (2, "c", SuggestionStatus.DISMISSED)]
    for minute, key, status in rows:
        await repos.suggestions.add(
            U1,
            _suggestion(U1, created_at=T0 + timedelta(minutes=minute)).model_copy(
                update={"status": status, "action": {"kind": "move", "dedupe_key": key}}
            ),
        )
    await repos.suggestions.add(
        U2,
        _suggestion(U2).model_copy(
            update={"status": SuggestionStatus.DISMISSED, "action": {"dedupe_key": "z"}}
        ),
    )

    assert await repos.suggestions.dedupe_keys(U1, status=SuggestionStatus.DISMISSED) == ["c", "a"]
    assert await repos.suggestions.dedupe_keys(
        U1, status=SuggestionStatus.DISMISSED, limit=1
    ) == ["c"]
    reads = [names for table, names in fake_client.selects if table == "proactive_suggestions"]
    assert all(names == ("action",) for names in reads)  # no reads at all for the in-memory family


# --- Usage -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage__summary__counts_local_and_cloud(repos):
    await repos.usage.record(_usage(U1, LLMLocation.LOCAL, 100, 50, at=T0))
    await repos.usage.record(_usage(U1, LLMLocation.LOCAL, 10, 5, at=T0 + timedelta(minutes=30)))
    await repos.usage.record(_usage(U1, LLMLocation.CLOUD, 200, 80, pii=2, at=T0 + timedelta(hours=2)))
    await repos.usage.record(_usage(U2, LLMLocation.CLOUD, 999, 999, pii=9))
    await repos.usage.record(_usage(None, LLMLocation.LOCAL, 999, 999))

    assert await repos.usage.summary(U1) == {
        "local_calls": 2,
        "cloud_calls": 1,
        "input_tokens": 310,
        "output_tokens": 135,
        "pii_redactions": 2,
        "truncated": False,
    }
    assert await repos.usage.summary(U1, since=T0 + timedelta(hours=1)) == {
        "local_calls": 0,
        "cloud_calls": 1,
        "input_tokens": 200,
        "output_tokens": 80,
        "pii_redactions": 2,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_usage__summary__more_rows_than_one_read__exact_totals(repos, fake_client):
    """PostgREST answers at most max_rows (1000) rows per read: the summary pages past it."""
    assert fake_client.max_rows == 1000
    for i in range(1205):
        location = LLMLocation.LOCAL if i % 5 else LLMLocation.CLOUD
        await repos.usage.record(
            _usage(U1, location, 10, 2, pii=1 if location is LLMLocation.CLOUD else 0,
                   at=T0 + timedelta(seconds=i))
        )
    await repos.usage.record(_usage(U2, LLMLocation.CLOUD, 999, 999, pii=9))

    assert await repos.usage.summary(U1) == {
        "local_calls": 964,
        "cloud_calls": 241,
        "input_tokens": 12050,
        "output_tokens": 2410,
        "pii_redactions": 241,
        "truncated": False,
    }
    assert (await repos.usage.summary(U1, since=T0 + timedelta(seconds=1200)))["local_calls"] == 4


@pytest.mark.asyncio
async def test_usage__summary__past_the_page_bound__says_truncated(monkeypatch, fake_client):
    from app.infrastructure.persistence import supabase_repos

    monkeypatch.setattr(supabase_repos, "USAGE_PAGE_ROWS", 2)
    monkeypatch.setattr(supabase_repos, "USAGE_MAX_PAGES", 2)
    usage = RepositoryFactory.supabase(fake_client).usage
    for i in range(5):
        await usage.record(_usage(U1, LLMLocation.LOCAL, 1, 1, at=T0 + timedelta(seconds=i)))

    summary = await usage.summary(U1)

    assert (summary["local_calls"], summary["truncated"]) == (4, True)  # never silently short
    (columns,) = {names for table, names in fake_client.selects if table == "llm_usage_events"}
    assert set(columns) == {"location", "input_tokens", "output_tokens", "pii_redactions"}


# --- Memories ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_memories__add_get__roundtrip_with_provenance_and_session(repos, fake_client):
    session_id, message_id = str(uuid4()), str(uuid4())

    added = await repos.memories.add(
        U1,
        _memory(
            U1,
            "Usually skips the 9 AM slot",
            memory_type="behavioral_pattern",
            provenance=MemorySource.INFERRED,
            origin="behavior",
            pattern_key="skip_hour:9",
            confidence=0.7,
            observation_count=3,
            source_session_id=session_id,
            source_message_id=message_id,
            last_confirmed_at=T0 + timedelta(hours=1),
        ),
        embedding=_vec(1.0, 0.0),
    )

    assert added.id is not None
    got = await repos.memories.get(U1, added.id)
    assert got == added
    assert got.provenance is MemorySource.INFERRED and got.origin == "behavior"
    assert (got.source_session_id, got.source_message_id) == (session_id, message_id)
    assert got.status is MemoryStatus.ACTIVE and got.pattern_key == "skip_hour:9"
    assert got.last_confirmed_at == T0 + timedelta(hours=1)
    assert await repos.memories.get(U2, added.id) is None
    assert await repos.memories.get(U1, str(uuid4())) is None
    if isinstance(repos.memories, SupabaseMemoryRepository):
        # origin lives in the legacy source column; the table has no origin column.
        (row,) = fake_client.rows["user_memories"]
        assert row["source"] == "behavior" and "origin" not in row
        assert row["provenance"] == "inferred"


@pytest.mark.asyncio
async def test_memories__list__active_only_by_default(repos):
    active = await repos.memories.add(U1, _memory(U1, "Likes tea"))
    dismissed = await repos.memories.add(
        U1, _memory(U1, "Hates mornings", status=MemoryStatus.DISMISSED)
    )
    forgotten = await repos.memories.add(U1, _memory(U1, "", status=MemoryStatus.FORGOTTEN))
    await repos.memories.add(U1, _memory(U1, "Ran in 2025", status=MemoryStatus.FADED))
    await repos.memories.add(U1, _memory(U1, "Likes coffee", superseded_by=active.id))
    await repos.memories.add(U2, _memory(U2, "Theirs"))

    assert await repos.memories.list(U1) == [active]
    suppressed = await repos.memories.list(
        U1, statuses=frozenset({MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN})
    )
    assert sorted(m.id for m in suppressed) == sorted([dismissed.id, forgotten.id])
    assert [m.content for m in await repos.memories.list(U2)] == ["Theirs"]


@pytest.mark.asyncio
async def test_memories__list__types_filter(repos):
    fact = await repos.memories.add(U1, _memory(U1, "Studies CS", memory_type="fact"))
    goal = await repos.memories.add(U1, _memory(U1, "Ship the capstone", memory_type="goal"))
    await repos.memories.add(U1, _memory(U1, "Likes tea", memory_type="preference"))

    found = await repos.memories.list(U1, types=frozenset({"fact", "goal"}))

    assert sorted(m.id for m in found) == sorted([fact.id, goal.id])
    assert await repos.memories.list(U1, types=frozenset({"constraint"})) == []


@pytest.mark.asyncio
async def test_memories__list__never_returns_embeddings(repos, fake_client):
    added = await repos.memories.add(U1, _memory(U1, pattern_key="tea"), embedding=_vec(1.0, 0.0))

    read = [
        *await repos.memories.list(U1),
        await repos.memories.get(U1, added.id),
        await repos.memories.find_by_pattern_key(U1, "tea"),
        *[m.memory for m in await repos.memories.nearest(U1, _vec(1.0, 0.0), statuses=ACTIVE)],
    ]

    assert len(read) == 4
    assert all("embedding" not in m.model_dump() for m in read)
    selected = [columns for table, columns in fake_client.selects if table == "user_memories"]
    if isinstance(repos.memories, SupabaseMemoryRepository):
        assert len(selected) == 3, "list, get and find_by_pattern_key each select once"
    assert all("embedding" not in columns and "*" not in columns for columns in selected)


@pytest.mark.asyncio
async def test_memories__list__newest_first_with_a_limit(repos, fake_client):
    old = await repos.memories.add(U1, _memory(U1, "Old", created_at=T0))
    mid = await repos.memories.add(U1, _memory(U1, "Mid", created_at=T0 + timedelta(hours=1)))
    new = await repos.memories.add(U1, _memory(U1, "New", created_at=T0 + timedelta(hours=2)))

    assert await repos.memories.list(U1, newest_first=True, limit=2) == [new, mid]
    assert await repos.memories.list(U1) == [old, mid, new]  # default: oldest first, all
    with pytest.raises(errors.ValidationError):
        await repos.memories.list(U1, limit=-1)


@pytest.mark.asyncio
async def test_memories__list_ids__ids_only_newest_first_bounded_and_the_users(
    repos, fake_client
):
    forgotten = await repos.memories.add(
        U1, _memory(U1, "", status=MemoryStatus.FORGOTTEN, created_at=T0)
    )
    dismissed = await repos.memories.add(U1, _memory(
        U1, "Hates mornings", status=MemoryStatus.DISMISSED, created_at=T0 + timedelta(hours=1)
    ))
    await repos.memories.add(U1, _memory(U1, "Likes tea"))  # active: not asked for
    await repos.memories.add(U2, _memory(U2, "", status=MemoryStatus.FORGOTTEN))
    let_go = frozenset({MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN})
    before = len(fake_client.selects)

    ids = await repos.memories.list_ids(U1, statuses=let_go, limit=10)

    assert ids == [dismissed.id, forgotten.id]
    assert await repos.memories.list_ids(U1, statuses=let_go, limit=1) == [dismissed.id]
    if isinstance(repos.memories, SupabaseMemoryRepository):
        assert fake_client.selects[before] == ("user_memories", ("id",)), "no words read"


@pytest.mark.asyncio
async def test_memories__find_by_pattern_key__returns_dismissed_row(repos):
    dismissed = await repos.memories.add(
        U1, _memory(U1, "Skips 9 AM", status=MemoryStatus.DISMISSED, pattern_key="skip_hour:9")
    )

    assert await repos.memories.find_by_pattern_key(U1, "skip_hour:9") == dismissed
    assert await repos.memories.find_by_pattern_key(U1, "skip_hour:10") is None
    assert await repos.memories.find_by_pattern_key(U2, "skip_hour:9") is None


@pytest.mark.asyncio
async def test_memories__find_by_pattern_keys__one_read_any_status_only_the_users(
    repos, fake_client
):
    dismissed = await repos.memories.add(
        U1, _memory(U1, "Skips 9 AM", status=MemoryStatus.DISMISSED, pattern_key="skip_hour:9")
    )
    active = await repos.memories.add(U1, _memory(U1, "Peak 7 PM", pattern_key="peak_hour:19"))
    await repos.memories.add(U1, _memory(U1, "Likes tea"))  # unkeyed
    await repos.memories.add(U2, _memory(U2, "Theirs", pattern_key="skip_hour:10"))
    before = len(fake_client.selects)

    found = await repos.memories.find_by_pattern_keys(
        U1, ["skip_hour:9", "peak_hour:19", "skip_hour:10", "skip_hour:9"]
    )

    assert sorted(found, key=lambda m: m.pattern_key) == [active, dismissed]
    if isinstance(repos.memories, SupabaseMemoryRepository):
        assert len(fake_client.selects) - before == 1, "one read for every key"
    assert await repos.memories.find_by_pattern_keys(U1, []) == []
    assert await repos.memories.find_by_pattern_keys(U2, ["skip_hour:9"]) == []


@pytest.mark.asyncio
async def test_memories__find_by_pattern_keys__many_keys__bounded_reads_all_found(
    repos, fake_client, monkeypatch
):
    from app.infrastructure.persistence import supabase_repos

    monkeypatch.setattr(supabase_repos, "PATTERN_KEYS_PER_READ", 2)
    stored = [
        await repos.memories.add(U1, _memory(U1, f"Hour {h}", pattern_key=f"skip_hour:{h}"))
        for h in range(5)
    ]
    before = len(fake_client.selects)

    found = await repos.memories.find_by_pattern_keys(U1, [f"skip_hour:{h}" for h in range(5)])

    assert sorted(m.id for m in found) == sorted(m.id for m in stored)
    if isinstance(repos.memories, SupabaseMemoryRepository):
        assert len(fake_client.selects) - before == 3, "at most PATTERN_KEYS_PER_READ per read"


@pytest.mark.asyncio
async def test_memories__duplicate_pattern_key__raises_conflict(repos):
    first = await repos.memories.add(U1, _memory(U1, "Skips 9 AM", pattern_key="skip_hour:9"))
    other = await repos.memories.add(U1, _memory(U1, "Skips 10 AM", pattern_key="skip_hour:10"))

    with pytest.raises(errors.ConflictError) as added_again:
        await repos.memories.add(U1, _memory(U1, "Skips 9 AM again", pattern_key="skip_hour:9"))
    with pytest.raises(errors.ConflictError) as moved_onto:
        await repos.memories.update(U1, other.id, pattern_key="skip_hour:9")

    assert added_again.value.details == moved_onto.value.details == {"pattern_key": "skip_hour:9"}

    # Unkeyed memories never clash, and another user may hold the same key.
    await repos.memories.add(U1, _memory(U1, "Likes tea"))
    await repos.memories.add(U1, _memory(U1, "Likes jasmine tea"))
    await repos.memories.add(U2, _memory(U2, "Skips 9 AM", pattern_key="skip_hour:9"))
    assert await repos.memories.find_by_pattern_key(U1, "skip_hour:9") == first
    assert (await repos.memories.get(U1, other.id)).pattern_key == "skip_hour:10"
    assert len(await repos.memories.list(U1)) == 4


@pytest.mark.asyncio
async def test_memories__duplicate_id__raises_conflict_naming_the_id(repos):
    memory_id = str(uuid4())
    await repos.memories.add(U1, _memory(U1, "Likes tea", id=memory_id, pattern_key="tea"))

    for user_id in (U1, U2):  # the id is a primary key across every user
        with pytest.raises(errors.ConflictError) as info:
            await repos.memories.add(user_id, _memory(user_id, "Likes coffee", id=memory_id))
        assert info.value.details == {"memory_id": memory_id}

    assert [m.content for m in await repos.memories.list(U1)] == ["Likes tea"]
    assert await repos.memories.list(U2) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["source_session_id", "source_message_id", "superseded_by"])
async def test_memories__non_uuid_reference__raises_validation_and_writes_nothing(repos, field):
    every_status = frozenset(MemoryStatus)

    with pytest.raises(errors.ValidationError):
        await repos.memories.add(U1, _memory(U1, **{field: "session-1"}))
    assert await repos.memories.list(U1, statuses=every_status) == []

    # A UUID with no row behind it is fine: these are soft references.
    stored = await repos.memories.add(U1, _memory(U1, **{field: str(uuid4())}))
    with pytest.raises(errors.ValidationError):
        await repos.memories.update(U1, stored.id, **{field: "not-a-uuid"})
    assert await repos.memories.get(U1, stored.id) == stored


_BAD_EMBEDDINGS = {
    "too_short": [1.0, 0.0, 0.0],
    "too_long": [1.0] * 385,
    "all_zero": [0.0] * 384,
    "nan": [float("nan")] + [1.0] * 383,
    "infinite": [float("inf")] + [1.0] * 383,
    "not_numbers": ["a"] * 384,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _BAD_EMBEDDINGS.values(), ids=_BAD_EMBEDDINGS.keys())
async def test_memories__bad_embedding__raises_validation_and_writes_nothing(repos, bad):
    stored = await repos.memories.add(U1, _memory(U1, "Likes tea"), embedding=_vec(1.0, 0.0))

    with pytest.raises(errors.ValidationError):
        await repos.memories.add(U1, _memory(U1, "Likes coffee"), embedding=bad)
    with pytest.raises(errors.ValidationError):
        await repos.memories.update(U1, stored.id, embedding=bad)
    with pytest.raises(errors.ValidationError):
        await repos.memories.nearest(U1, bad, statuses=ACTIVE)

    assert await repos.memories.list(U1) == [stored]
    (match,) = await repos.memories.nearest(U1, _vec(1.0, 0.0), statuses=ACTIVE)
    assert match.similarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_memories__nearest__negative_limit__raises_validation(repos):
    await repos.memories.add(U1, _memory(U1, "Likes tea"), embedding=_vec(1.0, 0.0))

    with pytest.raises(errors.ValidationError):
        await repos.memories.nearest(U1, _vec(1.0, 0.0), statuses=ACTIVE, limit=-1)
    assert await repos.memories.nearest(U1, _vec(1.0, 0.0), statuses=ACTIVE, limit=0) == []


@pytest.mark.asyncio
async def test_memories__nearest__most_similar_first_status_and_type_filtered(repos):
    close = await repos.memories.add(U1, _memory(U1, "Close"), embedding=_vec(1.0, 0.1))
    closest = await repos.memories.add(U1, _memory(U1, "Closest"), embedding=_vec(1.0, 0.0))
    far = await repos.memories.add(U1, _memory(U1, "Far"), embedding=_vec(0.0, 1.0))
    await repos.memories.add(U1, _memory(U1, "No embedding"))
    dismissed = await repos.memories.add(
        U1, _memory(U1, "Dismissed", status=MemoryStatus.DISMISSED), embedding=_vec(1.0, 0.0)
    )
    goal = await repos.memories.add(
        U1, _memory(U1, "A goal", memory_type="goal"), embedding=_vec(1.0, 0.05)
    )
    await repos.memories.add(
        U1, _memory(U1, "Superseded", superseded_by=close.id), embedding=_vec(1.0, 0.0)
    )
    query = _vec(1.0, 0.0)

    matches = await repos.memories.nearest(U1, query, statuses=ACTIVE)
    assert [m.memory.id for m in matches] == [closest.id, goal.id, close.id, far.id]
    assert matches[0].similarity == pytest.approx(1.0)
    assert matches[-1].similarity == pytest.approx(0.0)

    preferences = await repos.memories.nearest(
        U1, query, statuses=ACTIVE, types=frozenset({"preference"}), limit=2
    )
    assert [m.memory.id for m in preferences] == [closest.id, close.id]

    similar = await repos.memories.nearest(U1, query, statuses=ACTIVE, min_similarity=0.5)
    assert far.id not in [m.memory.id for m in similar] and len(similar) == 3

    with_dismissed = await repos.memories.nearest(
        U1, query, statuses=frozenset({MemoryStatus.DISMISSED})
    )
    assert [m.memory.id for m in with_dismissed] == [dismissed.id]


@pytest.mark.asyncio
async def test_memories__nearest__user_without_memories__empty_never_another_users(repos):
    for status in MemoryStatus:
        content = "" if status is MemoryStatus.FORGOTTEN else "Their fact"
        await repos.memories.add(U2, _memory(U2, content, status=status), embedding=_vec(1.0, 0.0))

    found = await repos.memories.nearest(
        U1, _vec(1.0, 0.0), statuses=frozenset(MemoryStatus), limit=50, min_similarity=-1.0
    )

    assert found == []
    assert await repos.memories.list(U1, statuses=frozenset(MemoryStatus)) == []


@pytest.mark.asyncio
async def test_memories__update__other_user__raises_not_found(repos):
    mine = await repos.memories.add(U1, _memory(U1, "Likes tea"), embedding=_vec(0.0, 1.0))

    with pytest.raises(errors.NotFoundError):
        await repos.memories.update(U2, mine.id, status=MemoryStatus.DISMISSED)
    with pytest.raises(errors.NotFoundError):
        await repos.memories.update(U1, str(uuid4()), status=MemoryStatus.DISMISSED)
    with pytest.raises(errors.NotFoundError):
        await repos.memories.update(U1, "not-a-uuid", status=MemoryStatus.DISMISSED)
    assert await repos.memories.get(U1, mine.id) == mine

    forgotten = await repos.memories.update(
        U1, mine.id, status=MemoryStatus.FORGOTTEN, content="", embedding=_vec(1.0, 0.0)
    )

    assert forgotten.status is MemoryStatus.FORGOTTEN and forgotten.content == ""
    assert await repos.memories.get(U1, mine.id) == forgotten
    (match,) = await repos.memories.nearest(
        U1, _vec(1.0, 0.0), statuses=frozenset({MemoryStatus.FORGOTTEN})
    )
    assert match.similarity == pytest.approx(1.0)
    with pytest.raises(errors.ValidationError):
        await repos.memories.update(U1, mine.id, confidence=2.0)
    with pytest.raises(errors.ValidationError):
        await repos.memories.update(U1, mine.id, id=str(uuid4()))
    with pytest.raises(errors.ValidationError):
        await repos.memories.update(U1, mine.id, favourite_colour="red")


IN_USE = frozenset({MemoryStatus.ACTIVE, MemoryStatus.FADED})


@pytest.mark.asyncio
async def test_memories__update_only_if_status__status_allowed__updated(repos):
    faded = await repos.memories.add(U1, _memory(U1, "Likes tea", status=MemoryStatus.FADED))

    updated = await repos.memories.update(
        U1, faded.id, only_if_status=IN_USE, status=MemoryStatus.ACTIVE, strength=1.0
    )

    assert (updated.status, updated.strength) == (MemoryStatus.ACTIVE, 1.0)
    assert await repos.memories.get(U1, faded.id) == updated


@pytest.mark.asyncio
async def test_memories__update_only_if_status__status_moved__stale_and_unchanged(repos):
    forgotten = await repos.memories.add(U1, _memory(U1, "", status=MemoryStatus.FORGOTTEN))

    with pytest.raises(errors.StaleVersionError) as info:
        await repos.memories.update(
            U1, forgotten.id, only_if_status=IN_USE, status=MemoryStatus.ACTIVE, content="Back"
        )

    assert info.value.details["status"] == "forgotten"
    assert await repos.memories.get(U1, forgotten.id) == forgotten
    with pytest.raises(errors.NotFoundError):
        await repos.memories.update(U2, forgotten.id, only_if_status=IN_USE, strength=1.0)


@pytest.mark.asyncio
async def test_memories__guarded_update__forgotten_after_the_read__stale_and_kept(fake_client):
    """Supabase family: the status guard is part of the one UPDATE statement, so a
    forget that lands after the repository read the row still wins."""
    memories = RepositoryFactory.supabase(fake_client).memories
    mine = await memories.add(U1, _memory(U1, "Likes tea"))
    read = memories.get

    async def read_then_forget(user_id, memory_id):
        snapshot = await read(user_id, memory_id)
        (row,) = fake_client.rows["user_memories"]
        row.update(status="forgotten", content="")  # another request, between read and write
        return snapshot

    memories.get = read_then_forget

    with pytest.raises(errors.StaleVersionError):
        await memories.update(U1, mine.id, only_if_status=IN_USE, strength=1.0, confidence=0.9)

    (row,) = fake_client.rows["user_memories"]
    assert (row["status"], row["content"], row["confidence"]) == ("forgotten", "", 0.5)


# --- Conversations (chat_sessions / chat_messages: read, plus the summary) -----


@pytest.fixture
def seed_chat(memory_store, fake_client):
    """Write a session, or messages, into whichever store backs ``repos``.

    No port writes sessions or messages (``chat_history`` does, outside the domain),
    so, like ``expire_draft``, this reaches behind the ports and writes both stores.
    """

    def _session(user_id: str, *, updated_at: datetime = T0, title: str | None = None) -> str:
        session = ConversationSession(
            id=str(uuid4()), user_id=user_id, title=title,
            created_at=T0 - timedelta(days=1), updated_at=updated_at,
        )
        memory_store.chat_sessions.append(session)
        fake_client.rows.setdefault("chat_sessions", []).append(
            {**session.model_dump(mode="json"), "is_archived": False, "metadata": {}}
        )
        return session.id

    def _message(user_id: str, session_id: str, at: datetime, role: str = "user") -> ChatMessage:
        message = ChatMessage(
            id=str(uuid4()), session_id=session_id, user_id=user_id, role=role,
            content=f"said at {at:%H:%M}", created_at=at,
        )
        memory_store.chat_messages.append(message)
        fake_client.rows.setdefault("chat_messages", []).append(
            {**message.model_dump(mode="json"), "intent": None, "metadata": {}}
        )
        return message

    return SimpleNamespace(session=_session, message=_message)


@pytest.mark.asyncio
async def test_conversations__previous_session__newest_other_with_messages(repos, seed_chat):
    older = seed_chat.session(U1, updated_at=T0 - timedelta(days=2))
    seed_chat.message(U1, older, T0 - timedelta(days=2))
    newest = seed_chat.session(U1, updated_at=T0 - timedelta(hours=3))
    seed_chat.message(U1, newest, T0 - timedelta(hours=3))
    seed_chat.session(U1, updated_at=T0 - timedelta(hours=1))  # newer, but no message
    other = seed_chat.session(U2, updated_at=T0)
    seed_chat.message(U2, other, T0)
    current = seed_chat.session(U1, updated_at=T0)
    seed_chat.message(U1, current, T0)

    previous = await repos.conversations.previous_session(U1, current)

    assert previous is not None and previous.id == newest
    assert (await repos.conversations.previous_session(U1, newest)).id == current
    assert await repos.conversations.previous_session(U2, other) is None


@pytest.mark.asyncio
async def test_conversations__other_user__invisible(repos, seed_chat):
    theirs = seed_chat.session(U2, title="Their plans")
    seed_chat.message(U2, theirs, T0)

    assert await repos.conversations.get_session(U1, theirs) is None
    assert await repos.conversations.messages(U1, theirs) == []
    assert await repos.conversations.recent_sessions(U1, since=T0 - timedelta(days=7)) == []
    assert await repos.conversations.previous_session(U1, str(uuid4())) is None
    with pytest.raises(errors.NotFoundError):
        await repos.conversations.set_summary(U1, theirs, "Mine now.", T0)
    assert (await repos.conversations.get_session(U2, theirs)).summary is None


@pytest.mark.asyncio
async def test_conversations__set_summary__roundtrip(repos, seed_chat):
    session_id = seed_chat.session(U1, updated_at=T0, title="Plan my week.")
    ist_instant = datetime(2026, 11, 14, 14, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

    summary = "You planned the week.\n- Gym?"
    await repos.conversations.set_summary(U1, session_id, summary, ist_instant)

    session = await repos.conversations.get_session(U1, session_id)
    assert session.summary == summary
    assert session.summary_through == ist_instant  # the same instant, held in UTC
    assert session.summary_through.tzinfo is not None
    assert session.summary_updated_at is not None
    # The summary is not activity: the session's last activity is unchanged.
    assert (session.title, session.updated_at) == ("Plan my week.", T0)
    for bad in ("   ", "x" * 4001):
        with pytest.raises(errors.ValidationError):
            await repos.conversations.set_summary(U1, session_id, bad, T0)
    assert (await repos.conversations.get_session(U1, session_id)).summary.startswith("You planned")


@pytest.mark.asyncio
async def test_conversations__set_summary__covered_through_stored_never_before_the_window(
    repos, seed_chat
):
    session_id = seed_chat.session(U1, updated_at=T0)
    window, reach = T0, T0 + timedelta(minutes=8)

    await repos.conversations.set_summary(U1, session_id, "Idle.", window, covered_through=reach)
    assert (await repos.conversations.get_session(U1, session_id)).summary_covered_through == reach

    await repos.conversations.set_summary(U1, session_id, "Compacted.", reach)  # default
    assert (await repos.conversations.get_session(U1, session_id)).summary_covered_through == reach
    later = reach + timedelta(minutes=1)
    await repos.conversations.set_summary(
        U1, session_id, "Behind.", later, covered_through=window  # never before the window
    )
    stored = await repos.conversations.get_session(U1, session_id)
    assert (stored.summary_through, stored.summary_covered_through) == (later, later)
    assert stored.updated_at == T0  # not activity
    with pytest.raises(errors.ValidationError):
        await repos.conversations.set_summary(
            U1, session_id, "Naive.", later, covered_through=later.replace(tzinfo=None)
        )


@pytest.mark.asyncio
async def test_conversations__set_summary__compare_and_set_never_moves_back(repos, seed_chat):
    session_id = seed_chat.session(U1)
    t1, t2 = T0, T0 + timedelta(minutes=10)
    written = T0 + timedelta(hours=1)

    assert await repos.conversations.set_summary(U1, session_id, "First.", t1) is True
    assert await repos.conversations.set_summary(
        U1, session_id, "Reaches further.", t2, written_at=written
    ) is True
    # A slower writer that read an older state reaches less far: nothing is written.
    assert await repos.conversations.set_summary(U1, session_id, "Stale.", t1) is False
    stored = await repos.conversations.get_session(U1, session_id)
    assert (stored.summary, stored.summary_through, stored.summary_updated_at) == (
        "Reaches further.", t2, written,
    )
    # The same reach may be rewritten (an idle summary that keeps the window's start).
    assert await repos.conversations.set_summary(U1, session_id, "Same reach.", t2) is True
    assert (await repos.conversations.get_session(U1, session_id)).summary == "Same reach."


@pytest.mark.asyncio
async def test_conversations__messages__newest_limit_after_through_oldest_first(repos, seed_chat):
    session_id = seed_chat.session(U1)
    sent = [seed_chat.message(U1, session_id, T0 + timedelta(minutes=m)) for m in range(6)]

    newest_three = await repos.conversations.messages(U1, session_id, limit=3)
    after_second = await repos.conversations.messages(U1, session_id, after=sent[1].created_at)
    oldest_two_after = await repos.conversations.messages(
        U1, session_id, after=sent[1].created_at, limit=2, oldest_first=True
    )

    assert [m.id for m in newest_three] == [m.id for m in sent[3:]]
    assert [m.id for m in after_second] == [m.id for m in sent[2:]]
    assert [m.id for m in oldest_two_after] == [m.id for m in sent[2:4]]
    # A limit above the count is every message, either way round.
    assert [m.id for m in await repos.conversations.messages(U1, session_id, limit=50)] == [
        m.id for m in sent
    ]
    assert [
        m.id
        for m in await repos.conversations.messages(U1, session_id, limit=50, oldest_first=True)
    ] == [m.id for m in sent]
    assert await repos.conversations.messages(U1, "not-a-uuid") == []
    with pytest.raises(errors.ValidationError):
        await repos.conversations.messages(U1, session_id, limit=-1)


@pytest.mark.asyncio
async def test_conversations__recent_sessions__since_newest_first(repos, seed_chat):
    old = seed_chat.session(U1, updated_at=T0 - timedelta(days=9))
    mid = seed_chat.session(U1, updated_at=T0 - timedelta(days=2))
    new = seed_chat.session(U1, updated_at=T0 - timedelta(hours=1))

    recent = await repos.conversations.recent_sessions(U1, since=T0 - timedelta(days=7))

    assert [s.id for s in recent] == [new, mid]
    assert old not in {s.id for s in recent}


# --- Documents (ingested_documents / task_materials) and their files -----------------


def _document(user_id: str, source_id: str, **fields) -> IngestedDocument:
    return IngestedDocument(user_id=user_id, source_id=source_id, **fields)


def _link(user_id: str, task_id: str, source_id: str, **fields) -> TaskMaterialLink:
    return TaskMaterialLink(user_id=user_id, task_id=task_id, source_id=source_id, **fields)


@pytest.mark.asyncio
async def test_documents__save_then_get__stored_with_id_and_stamp_user_scoped(repos):
    saved = await repos.documents.save(U1, _document(
        U1, "ingestion_aaa", file_name="ML syllabus.pdf", media_type="pdf",
        document_topics=["Gradient descent", "Neural networks"], chunk_count=7,
    ))

    assert saved.id and saved.created_at is not None
    got = await repos.documents.get(U1, "ingestion_aaa")
    assert got == saved
    assert (got.file_name, got.media_type, got.chunk_count) == ("ML syllabus.pdf", "pdf", 7)
    assert got.document_topics == ["Gradient descent", "Neural networks"]
    assert await repos.documents.get(U2, "ingestion_aaa") is None


@pytest.mark.asyncio
async def test_documents__save_same_source_twice__one_record_replaced_keeping_its_id(repos):
    first = await repos.documents.save(U1, _document(U1, "ingestion_aaa", chunk_count=1))

    second = await repos.documents.save(U1, _document(
        U1, "ingestion_aaa", file_name="notes.pdf", chunk_count=4
    ))

    assert second.id == first.id
    listed = await repos.documents.list_for_user(U1)
    assert [(d.source_id, d.file_name, d.chunk_count) for d in listed] == [
        ("ingestion_aaa", "notes.pdf", 4)
    ]


@pytest.mark.asyncio
async def test_documents__save_another_users_record__validation_error_nothing_written(repos):
    with pytest.raises(errors.ValidationError):
        await repos.documents.save(U1, _document(U2, "ingestion_aaa"))

    assert await repos.documents.list_for_user(U1) == []
    assert await repos.documents.list_for_user(U2) == []


@pytest.mark.asyncio
async def test_documents__list_for_user__newest_first_only_theirs(repos):
    for hours, source_id in ((0, "old"), (2, "newest"), (1, "middle")):
        await repos.documents.save(U1, _document(
            U1, source_id, created_at=T0 + timedelta(hours=hours)
        ))
    await repos.documents.save(U2, _document(U2, "theirs", created_at=T0 + timedelta(hours=9)))

    assert [d.source_id for d in await repos.documents.list_for_user(U1)] == [
        "newest", "middle", "old"
    ]
    assert DOCUMENT_LIST_LIMIT >= 100


@pytest.mark.asyncio
async def test_documents__link_twice__one_link_per_task_and_document(repos):
    await repos.documents.save(U1, _document(U1, "doc-1"))

    await repos.documents.link(U1, [_link(U1, "t1", "doc-1", document_topics=["Graphs"])])
    await repos.documents.link(U1, [
        _link(U1, "t1", "doc-1", document_topics=["Graphs", "Trees"]),
        _link(U1, "t2", "doc-1"),
    ])

    links = await repos.documents.links_for_documents(U1, ["doc-1"])
    assert sorted((l.task_id, tuple(l.document_topics)) for l in links) == [
        ("t1", ("Graphs", "Trees")), ("t2", ()),
    ]
    assert [l.source_id for l in await repos.documents.links_for_task(U1, "t2")] == ["doc-1"]


@pytest.mark.asyncio
async def test_documents__links__user_scoped_and_another_users_link_refused(repos):
    await repos.documents.save(U1, _document(U1, "doc-1"))
    await repos.documents.save(U2, _document(U2, "doc-1"))
    await repos.documents.link(U2, [_link(U2, "t1", "doc-1")])

    with pytest.raises(errors.ValidationError):
        await repos.documents.link(U1, [_link(U1, "t9", "doc-1"), _link(U2, "t1", "doc-1")])

    assert await repos.documents.links_for_documents(U1, ["doc-1"]) == []  # nothing written
    assert await repos.documents.links_for_task(U1, "t1") == []
    assert [l.user_id for l in await repos.documents.links_for_task(U2, "t1")] == [U2]
    assert await repos.documents.links_for_documents(U1, []) == []
    await repos.documents.link(U1, [])  # no links, no write


@pytest.mark.asyncio
async def test_documents__delete__record_and_its_links_gone_other_users_untouched(repos):
    for user in (U1, U2):
        await repos.documents.save(user, _document(user, "doc-1"))
        await repos.documents.link(user, [_link(user, "t1", "doc-1")])
    await repos.documents.save(U1, _document(U1, "doc-2"))
    await repos.documents.link(U1, [_link(U1, "t1", "doc-2")])

    assert await repos.documents.delete(U1, "doc-1") is True
    assert await repos.documents.delete(U1, "doc-1") is False

    assert await repos.documents.get(U1, "doc-1") is None
    assert [l.source_id for l in await repos.documents.links_for_task(U1, "t1")] == ["doc-2"]
    assert await repos.documents.get(U2, "doc-1") is not None
    assert len(await repos.documents.links_for_task(U2, "t1")) == 1


_PDF = StoredFile(content=b"%PDF-1.4 syllabus", content_type="application/pdf")


@pytest.mark.asyncio
async def test_document_files__save_read_delete__round_trip_with_its_content_type(repos):
    await repos.document_files.save(U1, "ingestion_aaa", _PDF)

    assert await repos.document_files.read(U1, "ingestion_aaa") == _PDF
    assert await repos.document_files.read(U2, "ingestion_aaa") is None
    assert await repos.document_files.stored_ids(U1) == frozenset({"ingestion_aaa"})
    assert await repos.document_files.stored_ids(U2) == frozenset()

    assert await repos.document_files.delete(U1, "ingestion_aaa") is True
    assert await repos.document_files.delete(U1, "ingestion_aaa") is False
    assert await repos.document_files.read(U1, "ingestion_aaa") is None
    assert await repos.document_files.stored_ids(U1) == frozenset()


@pytest.mark.asyncio
async def test_document_files__save_again__replaces_the_file(repos):
    await repos.document_files.save(U1, "doc", _PDF)
    png = StoredFile(content=b"\x89PNG...", content_type="image/png")

    await repos.document_files.save(U1, "doc", png)

    assert await repos.document_files.read(U1, "doc") == png


@pytest.mark.asyncio
async def test_document_files__stored_ids__every_file_of_the_user_only(repos):
    for source_id in ("a", "b", "c"):
        await repos.document_files.save(U1, source_id, _PDF)
    await repos.document_files.save(U2, "z", _PDF)

    assert await repos.document_files.stored_ids(U1) == frozenset({"a", "b", "c"})


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_id", "source_id"), [
    ("demo/../alice", "doc"), ("demo", "../alice/doc"), ("demo", "a/b"), ("..", "doc"),
    ("demo", ""), ("", "doc"), ("demo", "a\\b"), ("demo", "doc\n"),
])
async def test_document_files__a_key_part_that_is_not_one_segment__validation_error(
    repos, user_id, source_id
):
    with pytest.raises(errors.ValidationError):
        await repos.document_files.save(user_id, source_id, _PDF)
    with pytest.raises(errors.ValidationError):
        await repos.document_files.read(user_id, source_id)


# --- Supabase Storage only: the bucket and its failures ------------------------------


@pytest.mark.asyncio
async def test_supabase_files__first_use__private_bucket_created_with_the_size_limit():
    client = FakeSupabase()
    files = RepositoryFactory.supabase(
        client, document_bucket="docs", max_file_bytes=1024
    ).document_files

    await files.save(U1, "doc", _PDF)

    assert client.storage.buckets["docs"]["public"] is False
    assert client.storage.buckets["docs"]["file_size_limit"] == 1024
    assert client.storage.objects["docs"][f"{U1}/doc"]["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_supabase_files__existing_public_or_small_bucket__made_private_and_raised():
    client = FakeSupabase()
    client.storage.create_bucket("docs", options={"public": True, "file_size_limit": 10})
    files = RepositoryFactory.supabase(
        client, document_bucket="docs", max_file_bytes=1024
    ).document_files

    await files.save(U1, "doc", _PDF)  # 17 bytes: over the old limit, under the new

    assert client.storage.buckets["docs"]["public"] is False
    assert client.storage.buckets["docs"]["file_size_limit"] == 1024


@pytest.mark.asyncio
async def test_supabase_files__stored_ids__one_listing_however_many_files():
    client = FakeSupabase()
    files = RepositoryFactory.supabase(client).document_files
    for n in range(5):
        await files.save(U1, f"doc-{n}", _PDF)
    client.storage.listings = 0

    ids = await files.stored_ids(U1)

    assert len(ids) == 5 and client.storage.listings == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [
    lambda f: f.save(U1, "doc", _PDF),
    lambda f: f.read(U1, "doc"),
    lambda f: f.delete(U1, "doc"),
    lambda f: f.stored_ids(U1),
])
async def test_supabase_files__store_unreachable__persistence_error_chained(call):
    client = FakeSupabase()
    client.storage.down = True
    files = RepositoryFactory.supabase(client).document_files

    with pytest.raises(errors.PersistenceError) as info:
        await call(files)

    assert isinstance(info.value.__cause__, ConnectionError)
    assert "storage.internal" not in info.value.message


# --- Supabase family only: transport failures ---------------------------------


class _UnreachableClient:
    """A supabase client whose every call dies in transport."""

    def table(self, name: str):
        raise ConnectionError("connection refused: secret-host:5432")

    @property
    def storage(self):
        raise ConnectionError("connection refused: secret-host:443")

    def rpc(self, name: str, params: dict):
        raise ConnectionError("connection refused: secret-host:5432")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.tasks.list_pending(U1),
        lambda r: r.tasks.list_for_user(U1),
        lambda r: r.tasks.bring_back(U1, "t1"),
        lambda r: r.task_events.list_for_user(U1),
        lambda r: r.drafts.accept_atomic(U1, str(uuid4()), [_row("x")]),
        lambda r: r.constraints.list_active(U1),
        lambda r: r.goals.latest_context(U1, "g1"),
        lambda r: r.goals.add_deadline_update(U1, _plan_update(U1, "g1", "context")),
        lambda r: r.habit_trackers.list_for_user(U1),
        lambda r: r.habit_trackers.record_review(
            U1, str(uuid4()), repetitions=1, ef=2.5, next_interval_days=1, quality=4, done_at=T0
        ),
        lambda r: r.habit_trackers.add_for_constraint(U1, str(uuid4()), started_at=T0),
        lambda r: r.habit_trackers.delete_for_constraint(U1, str(uuid4())),
        lambda r: r.profiles.get_or_create(U1),
        lambda r: r.suggestions.exists_today(U1, "evening_review", T0.date()),
        lambda r: r.usage.record(_usage(U1, LLMLocation.LOCAL, 1, 1)),
        lambda r: r.memories.list(U1),
        lambda r: r.memories.nearest(U1, _vec(1.0, 0.0), statuses=ACTIVE),
        lambda r: r.conversations.messages(U1, str(uuid4())),
        lambda r: r.conversations.set_summary(U1, str(uuid4()), "Summary.", T0),
        lambda r: r.documents.list_for_user(U1),
        lambda r: r.documents.save(U1, _document(U1, "doc-1")),
        lambda r: r.documents.link(U1, [_link(U1, "t1", "doc-1")]),
        lambda r: r.document_files.stored_ids(U1),
        lambda r: r.document_files.read(U1, "doc-1"),
    ],
)
async def test_supabase_repos__transport_error__raises_persistence_error_chained(call):
    repos = RepositoryFactory.supabase(_UnreachableClient())

    with pytest.raises(errors.PersistenceError) as info:
        await call(repos)

    assert isinstance(info.value.__cause__, ConnectionError)
    assert "secret-host" not in info.value.message


class _RacingClient:
    """A FakeSupabase where another writer moves the draft just before each status write."""

    def __init__(self, inner: FakeSupabase, interfering_statuses: list[str]):
        self._inner = inner
        self._interfering = list(interfering_statuses)

    def table(self, name: str):
        query = self._inner.table(name)
        original_update = query.update

        def update(payload):
            if name == "draft_schedules" and self._interfering:
                for row in self._inner.rows["draft_schedules"]:
                    row["status"] = self._interfering[0]
                self._interfering.pop(0)
            return original_update(payload)

        query.update = update
        return query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interfering", "outcome"),
    [
        (["modified"], DraftStatus.REJECTED),
        (["accepted"], errors.InvalidStateTransition),
        (["modified", "pending", "modified"], errors.InvalidStateTransition),
    ],
    ids=["lost_once_then_legal", "lost_to_terminal_state", "lost_every_attempt"],
)
async def test_supabase_drafts__set_status__lost_compare_and_set__retries_against_new_status(
    interfering, outcome
):
    fake = FakeSupabase()
    draft = await _new_draft(RepositoryFactory.supabase(fake))
    repos = RepositoryFactory.supabase(_RacingClient(fake, interfering))

    if outcome is errors.InvalidStateTransition:
        with pytest.raises(errors.InvalidStateTransition):
            await repos.drafts.set_status(U1, draft.id, DraftStatus.REJECTED)
    else:
        moved = await repos.drafts.set_status(U1, draft.id, DraftStatus.REJECTED)
        assert moved.status is outcome


class _OddRpcClient(FakeSupabase):
    """The accept_draft function answers, but with a payload that is not a row count."""

    def rpc(self, name: str, params: dict):
        class _Call:
            def execute(self):
                class _Result:
                    data = None

                return _Result()

        return _Call()


@pytest.mark.asyncio
async def test_supabase_drafts__accept_atomic__odd_rpc_payload__raises_persistence_error():
    repos = RepositoryFactory.supabase(_OddRpcClient())
    draft = await _new_draft(repos)

    with pytest.raises(errors.PersistenceError) as info:
        await repos.drafts.accept_atomic(U1, draft.id, [_row("x")])

    assert isinstance(info.value.__cause__, TypeError)
    # The database answered; what it answered is unreadable. Not an outage.
    assert info.value.message == "Stored data is not readable."


@pytest.mark.asyncio
async def test_supabase_repos__unreadable_stored_row__raises_persistence_error(fake_client):
    fake_client.rows["user_tasks"] = [{**_row("legacy", status="cancelled"), "user_id": U1}]
    repos = RepositoryFactory.supabase(fake_client)

    with pytest.raises(errors.PersistenceError) as info:
        await repos.tasks.get(U1, "legacy")

    assert info.value.message == "Stored data is not readable."
    assert isinstance(info.value.__cause__, PydanticValidationError)


@pytest.mark.asyncio
async def test_supabase_memories__legacy_row_with_null_columns__reads_with_defaults(fake_client):
    """Rows written before 20261115000002 may hold NULL in these columns
    (the migration fills them), or a fake row may not carry them at all."""
    legacy = ("confidence", "strength", "stability", "observation_count", "source", "created_at")
    base = {
        "user_id": U1,
        "memory_type": "fact",
        "status": "active",
        "provenance": "said",
        "strength_as_of": T0.isoformat(),
        "embedding": _vec(1.0, 0.0),
    }
    nulls = {**base, "id": str(uuid4()), "content": "Null columns", **dict.fromkeys(legacy)}
    absent = {**base, "id": str(uuid4()), "content": "Absent columns"}
    fake_client.rows["user_memories"] = [nulls, absent]
    repos = RepositoryFactory.supabase(fake_client)

    listed = await repos.memories.list(U1)
    found = [m.memory for m in await repos.memories.nearest(U1, _vec(1.0, 0.0), statuses=ACTIVE)]

    assert sorted(m.content for m in listed) == sorted(m.content for m in found) == [
        "Absent columns", "Null columns",
    ]
    for memory in listed:
        assert (memory.confidence, memory.strength, memory.stability) == (0.5, 1.0, 1.0)
        assert memory.observation_count == 1 and memory.origin == "conversation"
        assert memory.created_at == T0  # falls back to strength_as_of


class _UniqueViolationClient(FakeSupabase):
    """Every user_memories insert fails on a unique index that is not the pattern key's."""

    def table(self, name: str):
        query = super().table(name)

        def insert(payload):
            error = Exception('duplicate key value violates unique constraint "some_other_key"')
            error.code = "23505"
            raise error

        query.insert = insert
        return query


@pytest.mark.asyncio
async def test_supabase_memories__other_unique_violation__raises_persistence_error():
    repos = RepositoryFactory.supabase(_UniqueViolationClient())

    with pytest.raises(errors.PersistenceError) as info:
        await repos.memories.add(U1, _memory(U1, pattern_key="tea"))

    assert info.value.message == "Could not reach the database."
