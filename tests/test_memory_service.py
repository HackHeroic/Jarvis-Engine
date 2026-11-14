"""MemoryService: what Jarvis remembers, and what it lets go of for good.

InMemory repositories, the bag-of-words embedder and a fixed clock. A fact said
again is reinforced, not stored twice; a contradiction supersedes the old fact; a
fact the user forgot or dismissed is recognised when it comes up again and is not
stored. Decay is persisted by a sweep, and a month-unused memory fades.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.domain import errors
from app.domain.entities import Memory, MemorySource, MemoryStatus, SUPPRESSING_STATUSES
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryMemoryRepository, InMemoryStore
from app.services.memory.memory_service import (
    MemoryCandidate,
    MemoryDecaySweep,
    MemoryService,
)
from tests.fakes import BagOfWordsEmbedder

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
    assert stored.stability == pytest.approx(2.0)
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
    clock.advance(days=30)
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
async def test_fade_stale__month_unused__faded_and_constraint_kept(service, memories):
    fact = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    rule = (await service.remember("alice", _candidate("Never schedule before 11 AM", "constraint"))).memory
    month_later = NOW + timedelta(days=30)

    assert await service.fade_stale("alice", month_later) == 1

    assert (await memories.get("alice", fact.id)).status is MemoryStatus.FADED
    kept = await memories.get("alice", rule.id)
    assert kept.status is MemoryStatus.ACTIVE
    assert kept.strength == pytest.approx(math.exp(-30 * 24 / 168))
    assert kept.strength_as_of == month_later


@pytest.mark.asyncio
async def test_fade_stale__persists_strength_without_double_decay(service, memories):
    memory = (await service.remember("alice", _candidate(MARATHON, "goal"))).memory
    later = NOW + timedelta(days=10)
    expected = memory.strength_at(later)

    assert await service.fade_stale("alice", NOW + timedelta(days=4)) == 0

    stored = await memories.get("alice", memory.id)
    assert stored.strength_as_of == NOW + timedelta(days=4)
    assert stored.strength == pytest.approx(math.exp(-4 * 24 / 168))
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
        changed = await sweep.run(["bob", "alice"], NOW + timedelta(days=30))

    assert changed == 1
    assert (await memories.get("alice", memory.id)).status is MemoryStatus.FADED
    assert (MemoryDecaySweep.name, MemoryDecaySweep.interval) == ("memory_decay", timedelta(hours=24))
    (warning,) = [r for r in caplog.records if r.name == "app.services.memory.memory_service"]
    assert "PersistenceError" in warning.getMessage()
