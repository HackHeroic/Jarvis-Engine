"""MemoryService: what Jarvis remembers, and what it lets go of for good.

InMemory repositories, the bag-of-words embedder and a fixed clock. A fact said
again is reinforced, not stored twice; a contradiction supersedes the old fact; a
fact the user forgot or dismissed is recognised when it comes up again and is not
stored. Decay is persisted by a sweep, and a memory unused past the retention floor
fades: how soon depends on what it is and who said it, and using it keeps it.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import app.core.config as config
from app.domain import errors
from app.domain.entities import Memory, MemorySource, MemoryStatus, SUPPRESSING_STATUSES
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryMemoryRepository, InMemoryStore
from app.services.memory.memory_service import (
    MemoryCandidate,
    MemoryDecaySweep,
    MemoryService,
)
from tests.fakes import BagOfWordsEmbedder, FakeSupabase

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
MARATHON = "Training for the Mumbai marathon in March"
MARATHON_AGAIN = "I'm training for the Mumbai marathon in March"  # 0.88 similar
MORNINGS = "Prefers studying in the mornings"
NIGHTS = "Now studies best late at night"
SKIPS_AT_NINE = "Skips work around 9 AM"
ALL_STATUSES = frozenset(MemoryStatus)


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> _Clock:
    return _Clock(NOW)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def memories(store):
    return RepositoryFactory.in_memory(store).memories


@pytest.fixture
def service(memories, clock) -> MemoryService:
    return MemoryService(memories, BagOfWordsEmbedder(), clock=clock)


def _candidate(content: str, memory_type: str = "fact", **fields) -> MemoryCandidate:
    return MemoryCandidate(memory_type=memory_type, content=content, **fields)


async def _every_row(memories, user_id: str = "alice") -> list[Memory]:
    return await memories.list(user_id, statuses=ALL_STATUSES)


# --- remember ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember__new_fact__created_with_said_provenance_and_source_ids(service, memories):
    session, message = str(uuid4()), str(uuid4())

    outcome = await service.remember(
        "alice",
        _candidate(MARATHON, "goal", confidence=0.8),
        source_session_id=session,
        source_message_id=message,
    )

    assert outcome.action == "created"
    (stored,) = await _every_row(memories)
    assert stored == outcome.memory
    assert stored.content == MARATHON
    assert stored.memory_type == "goal"
    assert stored.provenance is MemorySource.SAID
    assert stored.origin == "conversation"
    assert stored.status is MemoryStatus.ACTIVE
    assert (stored.source_session_id, stored.source_message_id) == (session, message)
    assert stored.confidence == pytest.approx(0.8)
    assert (stored.created_at, stored.strength_as_of, stored.strength) == (NOW, NOW, 1.0)
    assert stored.observation_count == 1
    assert stored.last_confirmed_at is None


@pytest.mark.asyncio
async def test_remember__same_fact_again__reinforced_not_duplicated_and_confirmed(
    service, memories, clock
):
    first = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    clock.advance(days=3)

    outcome = await service.remember("alice", _candidate(MARATHON_AGAIN, "goal"))

    assert outcome.action == "reinforced"
    (stored,) = await _every_row(memories)
    assert stored.id == outcome.memory.id == first.id
    assert stored.content == MARATHON  # the words it was first learned in
    assert stored.stability == pytest.approx(first.stability + 1.0)  # 12 weeks, then 13
    assert stored.observation_count == 2
    assert stored.confidence == pytest.approx(0.6)
    assert (stored.strength, stored.strength_as_of) == (1.0, clock.now)
    assert stored.last_confirmed_at == clock.now  # saying it again confirms it


@pytest.mark.asyncio
async def test_remember__contradiction__old_superseded_new_active(service, memories):
    old = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory

    outcome = await service.remember(
        "alice", _candidate(NIGHTS, "preference", confidence=0.8, contradicts=old.id)
    )

    assert outcome.action == "superseded"
    assert outcome.memory.content == NIGHTS
    assert (await memories.get("alice", old.id)).superseded_by == outcome.memory.id
    assert [m.id for m in await service.list_active("alice")] == [outcome.memory.id]


@pytest.mark.asyncio
async def test_remember__supersedes__outcome_names_the_old_memory(service, memories):
    old = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory
    assert (await service.remember("alice", _candidate(MARATHON, "goal"))).superseded_id is None

    outcome = await service.remember(
        "alice", _candidate(NIGHTS, "preference", confidence=0.8, contradicts=old.id)
    )

    assert outcome.action == "superseded" and outcome.superseded_id == old.id
    again = await service.remember("alice", _candidate(NIGHTS, "preference"))
    assert (again.action, again.superseded_id) == ("reinforced", None)


@pytest.mark.asyncio
async def test_remember__contradicted_memory_let_go_meanwhile__created_and_names_none(
    store, clock
):
    """The old memory stops being active between the read and the link: the new one
    stands as created, and no memory is named as replaced (nothing of it may go)."""

    class _ForgottenBeforeTheLink(InMemoryMemoryRepository):
        async def update(self, user_id, memory_id, **fields):
            if "superseded_by" in fields:
                await super().update(user_id, memory_id, status=MemoryStatus.FORGOTTEN, content="")
            return await super().update(user_id, memory_id, **fields)

    service = MemoryService(_ForgottenBeforeTheLink(store), BagOfWordsEmbedder(), clock=clock)
    old = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory

    outcome = await service.remember("alice", _candidate(NIGHTS, "preference", contradicts=old.id))

    assert (outcome.action, outcome.superseded_id) == ("created", None)


@pytest.mark.asyncio
async def test_remember__after_forget__suppressed_and_nothing_written(service, memories):
    """Review Focus 3: a forgotten fact said again, in another conversation and in
    other words, is not stored again, and the tombstone is not touched."""
    memory = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    await service.forget("alice", memory.id)
    before = await _every_row(memories)

    outcome = await service.remember(
        "alice", _candidate(MARATHON_AGAIN, "goal"), source_session_id=str(uuid4())
    )

    assert (outcome.action, outcome.memory) == ("suppressed", None)
    assert await _every_row(memories) == before
    assert await service.list_active("alice") == []


@pytest.mark.asyncio
async def test_remember__after_dismiss__suppressed(service, memories):
    memory = (
        await service.remember(
            "alice",
            _candidate(SKIPS_AT_NINE, "behavioral_pattern"),
            provenance=MemorySource.INFERRED,
            origin="behavior",
        )
    ).memory
    await service.dismiss("alice", memory.id)

    outcome = await service.remember(
        "alice", _candidate(SKIPS_AT_NINE, "behavioral_pattern"), provenance=MemorySource.INFERRED
    )

    assert (outcome.action, outcome.memory) == ("suppressed", None)
    (stored,) = await _every_row(memories)
    assert stored.status is MemoryStatus.DISMISSED
    assert stored.observation_count == 1


@pytest.mark.asyncio
async def test_remember__faded_fact_mentioned__reactivated(service, memories, clock):
    memory = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    clock.advance(weeks=30)  # a said goal (12 weeks) is under the floor after ~28 unused
    assert await service.fade_stale("alice", clock.now) == 1

    outcome = await service.remember("alice", _candidate(MARATHON_AGAIN, "goal"))

    assert outcome.action == "reinforced"
    (stored,) = await _every_row(memories)
    assert stored.id == memory.id
    assert stored.status is MemoryStatus.ACTIVE
    assert (stored.strength, stored.strength_as_of) == (1.0, clock.now)
    assert [m.id for m in await service.list_active("alice")] == [memory.id]


@pytest.mark.asyncio
async def test_remember__text_with_no_words__nothing_to_remember_and_never_embedded(memories):
    class _Refuses(BagOfWordsEmbedder):
        def embed(self, texts):
            raise AssertionError("blank text must never reach the embedder")

    service = MemoryService(memories, _Refuses(), clock=_Clock(NOW))

    outcome = await service.remember("alice", _candidate("  ?! "))

    assert (outcome.action, outcome.memory) == ("suppressed", None)
    assert await _every_row(memories) == []


# --- the user's controls ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget__content_blanked_and_hidden_from_list_active(service, memories):
    embedding = BagOfWordsEmbedder().embed([SKIPS_AT_NINE])[0]
    memory = await memories.add(
        "alice",
        Memory(
            user_id="alice",
            memory_type="behavioral_pattern",
            content=SKIPS_AT_NINE,
            provenance=MemorySource.INFERRED,
            origin="behavior",
            pattern_key="skip_time_window:9",
            strength_as_of=NOW,
            created_at=NOW,
        ),
        embedding=embedding,
    )

    assert await service.forget("alice", memory.id) is None

    stored = await memories.get("alice", memory.id)
    assert (stored.status, stored.content) == (MemoryStatus.FORGOTTEN, "")
    assert stored.pattern_key == "skip_time_window:9"  # still recognised by its key ...
    (match,) = await memories.nearest(
        "alice", embedding, statuses=SUPPRESSING_STATUSES, limit=1
    )
    assert match.memory.id == memory.id  # ... and by its meaning
    assert await service.list_active("alice") == []


@pytest.mark.asyncio
async def test_forget__another_users_memory__not_found_and_untouched(service, memories):
    memory = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory

    with pytest.raises(errors.NotFoundError):
        await service.forget("bob", memory.id)

    assert (await memories.get("alice", memory.id)).content == MARATHON


@pytest.mark.asyncio
async def test_confirm__raises_confidence_and_sets_last_confirmed_at(service, memories, clock):
    memory = (
        await service.remember(
            "alice",
            _candidate(SKIPS_AT_NINE, "behavioral_pattern", confidence=0.5),
            provenance=MemorySource.INFERRED,
            origin="behavior",
        )
    ).memory
    assert memory.last_confirmed_at is None
    clock.advance(days=2)

    confirmed = await service.confirm("alice", memory.id)

    assert confirmed == await memories.get("alice", memory.id)
    assert confirmed.confidence == pytest.approx(0.9)
    assert confirmed.last_confirmed_at == clock.now
    assert (confirmed.strength, confirmed.strength_as_of) == (1.0, clock.now)
    assert confirmed.stability == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_edit__new_words__re_embedded_so_the_new_words_are_recognised(service, memories):
    memory = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory

    edited = await service.edit("alice", memory.id, content=NIGHTS, memory_type="fact")

    assert (edited.content, edited.memory_type) == (NIGHTS, "fact")
    outcome = await service.remember("alice", _candidate(NIGHTS, "fact"))
    assert (outcome.action, outcome.memory.id) == ("reinforced", memory.id)


# --- what is active ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_active__expired_temporal_event__excluded(service, clock):
    exam = (
        await service.remember(
            "alice",
            _candidate("Has a chemistry exam on Wednesday", "temporal_event", expires_at=NOW + timedelta(days=1)),
        )
    ).memory
    goal = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    assert [m.id for m in await service.list_active("alice")] == [exam.id, goal.id]

    clock.advance(days=2)

    assert [m.id for m in await service.list_active("alice")] == [goal.id]


# --- decay --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fade_stale__unused_past_the_floor__faded_and_constraint_kept(service, memories):
    fact = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    rule = (await service.remember("alice", _candidate("Never schedule before 11 AM", "constraint"))).memory
    # Both under the floor: the said goal (12 weeks) and the said constraint (16 weeks).
    much_later = NOW + timedelta(weeks=40)

    assert await service.fade_stale("alice", much_later) == 1

    assert (await memories.get("alice", fact.id)).status is MemoryStatus.FADED
    kept = await memories.get("alice", rule.id)
    assert kept.status is MemoryStatus.ACTIVE
    assert kept.strength == pytest.approx(math.exp(-40 / 16))
    assert kept.strength < config.MEMORY_RETENTION_FLOOR  # under it, and still kept
    assert kept.strength_as_of == much_later


@pytest.mark.asyncio
async def test_fade_stale__persists_strength_without_double_decay(service, memories):
    memory = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    later = NOW + timedelta(days=10)
    expected = memory.strength_at(later)

    assert await service.fade_stale("alice", NOW + timedelta(days=4)) == 0

    stored = await memories.get("alice", memory.id)
    assert stored.strength_as_of == NOW + timedelta(days=4)
    assert stored.strength == pytest.approx(math.exp(-4 * 24 / (168 * memory.stability)))
    assert stored.strength_at(later) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_memory_decay_sweep__one_users_failure__others_faded(store, clock, caplog):
    class _DownForBob(InMemoryMemoryRepository):
        async def list(self, user_id, **kwargs):
            if user_id == "bob":
                raise errors.PersistenceError("Could not reach the database.")
            return await super().list(user_id, **kwargs)

    memories = _DownForBob(store)
    service = MemoryService(memories, BagOfWordsEmbedder(), clock=clock)
    memory = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    sweep = MemoryDecaySweep(service)

    with caplog.at_level(logging.WARNING, logger="app.services.memory.memory_service"):
        changed = await sweep.run(["bob", "alice"], NOW + timedelta(weeks=30))

    assert changed == 1
    assert (await memories.get("alice", memory.id)).status is MemoryStatus.FADED
    assert (MemoryDecaySweep.name, MemoryDecaySweep.interval) == ("memory_decay", timedelta(hours=24))
    (warning,) = [r for r in caplog.records if r.name == "app.services.memory.memory_service"]
    assert "PersistenceError" in warning.getMessage()


# --- forget and dismiss are terminal ------------------------------------------------------


class _ForgottenRightAfterRead(InMemoryMemoryRepository):
    """While armed, another request forgets every memory a read hands back, just after
    the read: the window between a service's read and its write."""

    armed = False

    def _forget(self, found: list[Memory]) -> None:
        if not self.armed:
            return
        for memory in found:
            i = self._index(memory.user_id, memory.id)
            self._store.memories[i] = self._store.memories[i].model_copy(
                update={"status": MemoryStatus.FORGOTTEN, "content": ""}
            )

    async def get(self, user_id, memory_id):
        found = await super().get(user_id, memory_id)
        self._forget([found] if found else [])
        return found

    async def list(self, user_id, **kwargs):
        found = await super().list(user_id, **kwargs)
        self._forget(found)
        return found

    async def nearest(self, user_id, embedding, **kwargs):
        found = await super().nearest(user_id, embedding, **kwargs)
        self._forget([match.memory for match in found])
        return found


@pytest.mark.asyncio
async def test_forget__lands_between_a_read_and_its_write__memory_stays_forgotten(store, clock):
    memories = _ForgottenRightAfterRead(store)
    service = MemoryService(memories, BagOfWordsEmbedder(), clock=clock)
    mentioned = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    confirmed = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory
    decayed = (await service.remember("alice", _candidate(SKIPS_AT_NINE, "behavioral_pattern"))).memory
    memories.armed = True

    again = await service.remember("alice", _candidate(MARATHON_AGAIN, "goal"))  # reinforce
    with pytest.raises(errors.ConflictError):
        await service.confirm("alice", confirmed.id)
    faded = await service.fade_stale("alice", NOW + timedelta(days=30))

    memories.armed = False
    assert (again.action, again.memory, faded) == ("suppressed", None, 0)
    for memory in (mentioned, confirmed, decayed):
        stored = await memories.get("alice", memory.id)
        assert (stored.status, stored.content) == (MemoryStatus.FORGOTTEN, "")


@pytest.mark.asyncio
async def test_confirm_or_edit__dismissed_or_forgotten__conflict_and_unchanged(service, memories):
    dismissed = (await service.remember("alice", _candidate(SKIPS_AT_NINE, "behavioral_pattern"))).memory
    forgotten = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    await service.dismiss("alice", dismissed.id)
    await service.forget("alice", forgotten.id)
    before = await _every_row(memories)

    for memory in (dismissed, forgotten):
        with pytest.raises(errors.ConflictError):
            await service.confirm("alice", memory.id)
        with pytest.raises(errors.ConflictError):
            await service.edit("alice", memory.id, content="Something else")
    with pytest.raises(errors.ConflictError):
        await service.dismiss("alice", forgotten.id)

    assert await _every_row(memories) == before
    await service.forget("alice", dismissed.id)  # the one way out of dismissed: erase it
    assert (await memories.get("alice", dismissed.id)).status is MemoryStatus.FORGOTTEN


@pytest.mark.asyncio
async def test_remember__forgotten_goal_offered_as_another_type__still_suppressed(service, memories):
    goal = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    await service.forget("alice", goal.id)

    outcome = await service.remember("alice", _candidate(MARATHON_AGAIN, "temporal_event"))

    assert (outcome.action, outcome.memory) == ("suppressed", None)
    assert [m.id for m in await _every_row(memories)] == [goal.id]


@pytest.mark.asyncio
async def test_remember__supersede_retried_after_the_link_failed__one_new_memory(store, clock):
    class _LinkFailsOnce(InMemoryMemoryRepository):
        failed = False

        async def update(self, user_id, memory_id, **fields):
            if "superseded_by" in fields and not self.failed:
                self.failed = True
                raise errors.PersistenceError("Could not reach the database.")
            return await super().update(user_id, memory_id, **fields)

    memories = _LinkFailsOnce(store)
    service = MemoryService(memories, BagOfWordsEmbedder(), clock=clock)
    old = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory
    replacement = _candidate(NIGHTS, "preference", contradicts=old.id)
    with pytest.raises(errors.PersistenceError):
        await service.remember("alice", replacement)

    outcome = await service.remember("alice", replacement)

    assert outcome.action == "superseded"
    stored = [m.id for m in store.memories]  # superseded rows included
    assert sorted(stored) == sorted([old.id, outcome.memory.id])
    assert (await memories.get("alice", old.id)).superseded_by == outcome.memory.id
    assert [m.id for m in await service.list_active("alice")] == [outcome.memory.id]


# --- the turn's one read of the memories: bounded, and no words it does not need ------


@pytest.mark.asyncio
async def test_snapshot__bounded_active_memories_and_only_the_ids_of_let_go_ones(clock):
    """Recall and the known memories need the active memories, newest first and bounded;
    the forget check needs only which memories were let go of (the store compares their
    embeddings itself), never their words."""
    import app.services.memory.memory_service as memory_service

    fake = FakeSupabase()
    memories = RepositoryFactory.supabase(fake).memories
    service = MemoryService(memories, BagOfWordsEmbedder(), clock=clock)
    for n in range(3):
        await service.remember("alice", _candidate(f"Fact number {n} about the user"))
        clock.advance(minutes=1)
    dismissed = (await service.remember("alice", _candidate("Hates early starts"))).memory
    await service.dismiss("alice", dismissed.id)
    before = len(fake.selects)
    original = memory_service.MEMORY_SNAPSHOT_LIMIT
    memory_service.MEMORY_SNAPSHOT_LIMIT = 2
    try:
        snapshot = await service.snapshot("alice")
    finally:
        memory_service.MEMORY_SNAPSHOT_LIMIT = original

    assert [m.content for m in snapshot.in_use(clock())] == [
        "Fact number 1 about the user", "Fact number 2 about the user",
    ]  # the newest 2, oldest first as list_active gives them
    assert snapshot.let_go_ids == frozenset({dismissed.id})
    assert all(m.status is MemoryStatus.ACTIVE for m in snapshot.memories)  # no tombstones
    reads = fake.selects[before:]
    assert len(reads) == 2 and ("user_memories", ("id",)) in reads
    assert await service.drop_suppressed(
        "alice", ["hates early starts", "likes tea"], let_go_ids=snapshot.let_go_ids
    ) == ["likes tea"]  # suppression still works from the ids alone


@pytest.mark.asyncio
async def test_drop_let_go__forgotten_rule__dropped_by_the_test_remember_uses(service):
    """A rule stored outside the memories is held to the same suppression as a memory:
    said again in the same words it is dropped; anything else is kept."""
    forgotten = (await service.remember("alice", _candidate("Never study before 11 AM"))).memory
    await service.forget("alice", forgotten.id)

    kept = await service.drop_let_go(
        "alice", ["Never study before 11 AM", "Gym from 6 to 7 pm"]
    )

    assert kept == ["Gym from 6 to 7 pm"]
    assert (await service.remember("alice", _candidate("Never study before 11 AM"))).action == (
        "suppressed"
    )  # the same fact, the same verdict
    assert await service.drop_let_go("bob", ["Never study before 11 AM"]) == [
        "Never study before 11 AM"
    ]  # another user's forgetting is not bob's


# --- how long a memory lasts: what it is, who said it, and whether it is used ----------


def test_initial_stability__said_preference__12_weeks():
    from app.services.memory.memory_service import initial_stability

    assert initial_stability("preference", MemorySource.SAID) == 12.0


def test_initial_stability__inferred_fact__2_weeks():
    from app.services.memory.memory_service import initial_stability

    assert initial_stability("fact", MemorySource.INFERRED) == 2.0


@pytest.mark.parametrize(
    ("memory_type", "said", "inferred"),
    [
        ("constraint", 16.0, 4.0),
        ("goal", 12.0, 4.0),
        ("preference", 12.0, 3.0),
        ("fact", 8.0, 2.0),
        ("behavioral_pattern", 1.0, 1.0),  # not in the table: a week, as before
        ("feedback", 1.0, 1.0),
        ("temporal_event", 1.0, 1.0),
    ],
)
def test_initial_stability__by_type_and_provenance__weeks_to_lose_a_factor_of_e(
    memory_type, said, inferred
):
    from app.services.memory.memory_service import initial_stability

    assert initial_stability(memory_type, MemorySource.SAID) == said
    assert initial_stability(memory_type, MemorySource.INFERRED) == inferred


@pytest.mark.asyncio
async def test_remember__said_preference__after_13_days_strength_at_least_0_8(service, clock):
    """The measured case: "I think most clearly in the evenings" read 0.20 after 13 days."""
    memory = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory
    assert memory.stability == 12.0

    clock.advance(days=13)

    assert memory.strength_at(clock.now) >= 0.8
    assert [m.id for m in await service.list_active("alice")] == [memory.id]


@pytest.mark.asyncio
async def test_remember__inferred_fact_never_seen_again__below_the_floor_within_six_weeks(
    service, memories, clock
):
    memory = (await service.remember(
        "alice", _candidate("Works in the library on Tuesdays"), provenance=MemorySource.INFERRED,
    )).memory
    clock.advance(weeks=1)
    assert [m.id for m in await service.list_active("alice")] == [memory.id]

    clock.advance(weeks=5)

    assert await service.list_active("alice") == []
    assert await service.fade_stale("alice", clock.now) == 1
    assert (await memories.get("alice", memory.id)).status is MemoryStatus.FADED


@pytest.mark.asyncio
async def test_remember__said_again__stability_up_by_one_and_capped(service, memories):
    rule = _candidate("Never schedule before 11 AM", "constraint")
    first = (await service.remember("alice", rule)).memory
    assert first.stability == 16.0

    for _ in range(6):
        await service.remember("alice", rule)

    assert (await memories.get("alice", first.id)).stability == 20.0  # 16 + 1 x 4, then capped


async def _note_used_now(service, memories, memory: Memory, clock) -> int:
    """``note_used`` at the clock's now, for the memory as stored now (what a turn's recall
    would hand it)."""
    return await service.note_used("alice", [await memories.get("alice", memory.id)], clock.now)


@pytest.mark.asyncio
async def test_note_used__relevant_memory__strength_reset_and_stability_up_by_half(
    service, memories, clock
):
    memory = (await service.remember("alice", _candidate(MARATHON, "fact"))).memory
    clock.advance(days=3)

    assert await _note_used_now(service, memories, memory, clock) == 1

    stored = await memories.get("alice", memory.id)
    assert (stored.strength, stored.strength_as_of) == (1.0, clock.now)
    assert stored.stability == pytest.approx(8.5)
    # Being used is not being said: no repeat counted, nothing confirmed.
    assert (stored.observation_count, stored.last_confirmed_at) == (1, None)


@pytest.mark.asyncio
async def test_note_used__same_memory_twice_within_20_hours__strengthened_once(
    service, memories, clock
):
    """Review Focus 4: recalled in ten replies in one day, it is strengthened once, the
    decay sweep running in between or not."""
    memory = (await service.remember("alice", _candidate(MARATHON, "fact"))).memory
    clock.advance(days=3)

    strengthened = 0
    for hour in range(10):
        if hour == 5:
            await service.fade_stale("alice", clock.now)  # the daily sweep, mid-day
        recall = await service.recall("alice", "how is my marathon training", now=clock.now)
        assert [m.id for m in recall.relevant] == [memory.id]
        strengthened += await service.note_used("alice", recall.relevant, clock.now)
        clock.advance(hours=1)

    assert strengthened == 1
    assert (await memories.get("alice", memory.id)).stability == pytest.approx(8.5)
    clock.advance(hours=11)  # 21 hours after the first use: once more
    assert await _note_used_now(service, memories, memory, clock) == 1
    assert (await memories.get("alice", memory.id)).stability == pytest.approx(9.0)


@pytest.mark.asyncio
async def test_note_used__decay_persisted_since_its_last_use__strengthened(
    service, memories, clock
):
    """The sweep moved ``strength_as_of`` an hour ago, but nothing set the memory to full
    strength within 20 hours: being used now strengthens it."""
    memory = (await service.remember("alice", _candidate(MARATHON, "fact"))).memory
    clock.advance(days=3)
    await service.fade_stale("alice", clock.now)
    clock.advance(hours=1)

    assert await _note_used_now(service, memories, memory, clock) == 1
    assert (await memories.get("alice", memory.id)).strength == 1.0


@pytest.mark.asyncio
async def test_note_used__just_learned__not_strengthened(service, memories, clock):
    memory = (await service.remember("alice", _candidate(MARATHON, "fact"))).memory
    clock.advance(hours=2)

    assert await _note_used_now(service, memories, memory, clock) == 0
    assert (await memories.get("alice", memory.id)).stability == 8.0


@pytest.mark.asyncio
async def test_note_used__dismissed_or_forgotten_memory__untouched(service, memories, clock):
    """Review Focus 5: read while still active (as recall read it), let go of before the
    write: never strengthened, and a forgotten one stays without its words."""
    dismissed = (await service.remember("alice", _candidate(MORNINGS, "preference"))).memory
    forgotten = (await service.remember("alice", _candidate(MARATHON, "fact"))).memory
    clock.advance(days=3)
    read = [await memories.get("alice", dismissed.id), await memories.get("alice", forgotten.id)]
    await service.dismiss("alice", dismissed.id)
    await service.forget("alice", forgotten.id)
    before = await _every_row(memories)

    assert await service.note_used("alice", read, clock.now) == 0
    assert await service.note_used("alice", await _every_row(memories), clock.now) == 0

    assert await _every_row(memories) == before


@pytest.mark.asyncio
async def test_note_used__store_fails__logged_and_nothing_raised(store, clock, caplog):
    class _WritesFail(InMemoryMemoryRepository):
        async def update(self, user_id, memory_id, **fields):
            if "stability" in fields and "observation_count" not in fields:
                raise errors.PersistenceError("Could not reach the database.")
            return await super().update(user_id, memory_id, **fields)

    service = MemoryService(_WritesFail(store), BagOfWordsEmbedder(), clock=clock)
    memory = (await service.remember("alice", _candidate(MARATHON, "fact"))).memory
    clock.advance(days=3)
    recall = await service.recall("alice", "how is my marathon training", now=clock.now)

    with caplog.at_level(logging.WARNING, logger="app.services.memory.memory_service"):
        assert await service.note_used("alice", recall.relevant, clock.now) == 0
        service.note_used_in_background("alice", recall.relevant, clock.now)
        await service.settle()

    assert [m.id for m in recall.relevant] == [memory.id]  # the recall stands
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "app.services.memory.memory_service"]
    assert len(warnings) == 2 and all("PersistenceError" in w for w in warnings)
    assert all("marathon" not in w.lower() for w in warnings)  # no memory words in the log
