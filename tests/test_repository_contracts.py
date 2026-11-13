"""Repository contract suite: every test runs against BOTH repository families.

The ``repos`` fixture is parametrised over the in-memory family and the Supabase
family (driven by ``FakeSupabase``), and the tests only ever talk to the ports.
The same assertions passing for both is the Liskov-substitution evidence: a
caller holding a ``TaskRepository`` cannot tell which family it was given.
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.domain import errors
from app.domain.entities import (
    AutonomyMode,
    BehavioralConstraint,
    DraftStatus,
    LLMLocation,
    LLMUsageEvent,
    Suggestion,
    SuggestionStatus,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from app.domain.ports import (
    ConstraintRepository,
    DraftRepository,
    ProfileRepository,
    SuggestionRepository,
    TaskEventRepository,
    TaskRepository,
    UsageRepository,
)
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
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
        "completion_criteria": "Explain it back",  # a column the Task entity ignores
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
async def test_tasks__update_status__unknown_task__raises_not_found(repos):
    await _seed_tasks(repos, U2, [_row("theirs")])

    with pytest.raises(errors.NotFoundError):
        await repos.tasks.update_status(U1, "missing", TaskStatus.COMPLETED)
    with pytest.raises(errors.NotFoundError):
        await repos.tasks.update_status(U1, "theirs", TaskStatus.COMPLETED)

    assert (await repos.tasks.get(U2, "theirs")).status is TaskStatus.PENDING


@pytest.mark.asyncio
async def test_tasks__reschedule__sets_times_and_keeps_row(repos):
    await _seed_tasks(repos, U1, [_row("a", start=T0)])
    before = await repos.tasks.get(U1, "a")
    new_start, new_end = T0 + timedelta(days=1), T0 + timedelta(days=1, minutes=30)

    moved = await repos.tasks.reschedule(U1, "a", new_start, new_end)

    assert (moved.scheduled_start, moved.scheduled_end) == (new_start, new_end)
    assert moved.id == before.id and moved.title == before.title
    assert await repos.tasks.get(U1, "a") == moved
    assert [t.task_id for t in await repos.tasks.list_pending(U1)] == ["a"]
    with pytest.raises(errors.NotFoundError):
        await repos.tasks.reschedule(U2, "a", new_start, new_end)


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
async def test_drafts__accept_atomic__concurrent_task_edit__edit_is_not_lost(repos):
    """An accept marks the draft ACCEPTED; it must not also write back stale tasks."""
    draft = await _new_draft(repos)
    edited = [{"task_id": "g1_t9", "title": "Edited while accepting", "duration_minutes": 10}]

    await asyncio.gather(
        repos.drafts.accept_atomic(U1, draft.id, [_row("x")]),
        repos.drafts.replace_tasks(U1, draft.id, edited),
    )

    stored = await repos.drafts.get(U1, draft.id)
    assert stored.status is DraftStatus.ACCEPTED
    assert stored.tasks == edited


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
    }
    assert await repos.usage.summary(U1, since=T0 + timedelta(hours=1)) == {
        "local_calls": 0,
        "cloud_calls": 1,
        "input_tokens": 200,
        "output_tokens": 80,
        "pii_redactions": 2,
    }


# --- Supabase family only: transport failures ---------------------------------


class _UnreachableClient:
    """A supabase client whose every call dies in transport."""

    def table(self, name: str):
        raise ConnectionError("connection refused: secret-host:5432")

    def rpc(self, name: str, params: dict):
        raise ConnectionError("connection refused: secret-host:5432")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.tasks.list_pending(U1),
        lambda r: r.task_events.list_for_user(U1),
        lambda r: r.drafts.accept_atomic(U1, str(uuid4()), [_row("x")]),
        lambda r: r.constraints.list_active(U1),
        lambda r: r.profiles.get_or_create(U1),
        lambda r: r.suggestions.exists_today(U1, "evening_review", T0.date()),
        lambda r: r.usage.record(_usage(U1, LLMLocation.LOCAL, 1, 1)),
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


@pytest.mark.asyncio
async def test_supabase_repos__unreadable_stored_row__raises_persistence_error(fake_client):
    fake_client.rows["user_tasks"] = [{**_row("legacy", status="cancelled"), "user_id": U1}]
    repos = RepositoryFactory.supabase(fake_client)

    with pytest.raises(errors.PersistenceError) as info:
        await repos.tasks.get(U1, "legacy")

    assert info.value.message == "Stored data is not readable."
    assert isinstance(info.value.__cause__, PydanticValidationError)
