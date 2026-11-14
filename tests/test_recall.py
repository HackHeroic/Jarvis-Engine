"""Budgeted recall: a small always-on profile, then what bears on this message.

``MemoryService.recall`` returns the user's standing constraints, preferences and goals
(the profile) and the memories most relevant to the message, each part inside its own
token budget, so the prompt never grows with the number of memories. Relevant memories
rank by similarity x freshness x confidence. Nothing the user let go of, nothing faded,
expired or below the retention floor, and nobody else's memories ever appear. A trivial
or wordless message costs no embedding call; an embedder that is down leaves the
profile only.

InMemory repositories and a fixed clock.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

import pytest

import app.core.config as config
from app.domain.entities import Memory, MemorySource, MemoryStatus
from app.domain.errors import EmbeddingUnavailableError
from app.domain.ports import TextEmbedder
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
from app.services.memory.context import Recall, estimate_tokens, render_memory_line
from app.services.memory.memory_service import MemoryService
from tests.fakes import BagOfWordsEmbedder

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
QUERY = "how is my marathon training going"
_DIMS = 384


class _CountingEmbedder(BagOfWordsEmbedder):
    """The bag-of-words embedder, counting every text it is asked to embed."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return super().embed(texts)


class _VectorEmbedder(TextEmbedder):
    """Embeds QUERY as the first axis; a memory's vector sits at a chosen cosine from it."""

    dimensions = _DIMS

    def embed(self, texts: list[str]) -> list[list[float]]:
        assert texts == [QUERY], texts
        return [_at_similarity(1.0)]


class _DownEmbedder(TextEmbedder):
    dimensions = _DIMS

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("The text embedder is unavailable.")


def _at_similarity(similarity: float) -> list[float]:
    """A unit vector whose cosine with the first axis is ``similarity``."""
    vector = [0.0] * _DIMS
    vector[0] = similarity
    vector[1] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


@pytest.fixture
def memories():
    return RepositoryFactory.in_memory(InMemoryStore()).memories


async def _add(
    memories,
    content: str,
    memory_type: str = "fact",
    *,
    user_id: str = "alice",
    embedding: list[float] | None = None,
    confidence: float = 0.8,
    provenance: MemorySource = MemorySource.SAID,
    **fields,
) -> Memory:
    """Store a memory directly, with the bag-of-words embedding of its text by default."""
    fields.setdefault("created_at", NOW - timedelta(days=1))
    fields.setdefault("strength_as_of", NOW)
    memory = Memory(
        user_id=user_id,
        memory_type=memory_type,
        content=content,
        confidence=confidence,
        provenance=provenance,
        **fields,
    )
    vector = embedding if embedding is not None else BagOfWordsEmbedder().embed([content])[0]
    return await memories.add(user_id, memory, embedding=vector)


def _lines_tokens(found: list[Memory]) -> int:
    """Tokens of the memory lines as the note renders them, one per line."""
    return estimate_tokens("\n".join(render_memory_line(m, NOW) for m in found))


def _contents(found: list[Memory]) -> list[str]:
    return [m.content for m in found]


# --- a brand-new user ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall__new_user__empty_and_no_other_users(memories):
    await _add(memories, "Training for the Mumbai marathon in March", "goal", user_id="alice")
    await _add(memories, "My marathon training runs are at 6 AM", "fact", user_id="alice")
    embedder = _CountingEmbedder()
    service = MemoryService(memories, embedder, clock=lambda: NOW)

    bob = await service.recall("bob", QUERY, now=NOW)

    assert bob == Recall(profile=[], relevant=[], tokens=0)
    assert bob.all() == []
    assert embedder.texts == [QUERY]  # bob's message was looked up, among bob's rows only


# --- the profile ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall__profile_capped_at_budget(memories):
    for day in range(4):
        created = NOW - timedelta(days=30 - day)
        await _add(memories, f"Goal number {day}: finish the long project chapter in time",
                   "goal", created_at=created)
        await _add(memories, f"Preference {day}: deep work happens best in a quiet library",
                   "preference", created_at=created)
        await _add(memories, f"Constraint {day}: never schedule anything during the gym hour",
                   "constraint", created_at=created)
    await _add(memories, "Loves cold coffee", "preference", confidence=0.5)  # too unsure
    await _add(memories, "Has a dog named Bruno", "fact")  # not a standing preference
    service = MemoryService(memories, _CountingEmbedder(), clock=lambda: NOW)

    recall = await service.recall("alice", QUERY, trivial=True, now=NOW)

    assert recall.profile, "the profile holds what fits"
    assert len(recall.profile) < 12, "and no more"
    assert _lines_tokens(recall.profile) <= config.MEMORY_PROFILE_TOKENS
    assert recall.tokens <= config.MEMORY_PROFILE_TOKENS
    # Constraints first, then preferences, then goals; newest first within each.
    kinds = [m.memory_type for m in recall.profile]
    assert kinds == sorted(kinds, key=["constraint", "preference", "goal"].index)
    constraints = [m for m in recall.profile if m.memory_type == "constraint"]
    assert constraints[0].content.startswith("Constraint 3:")
    assert [m.created_at for m in constraints] == sorted(
        (m.created_at for m in constraints), reverse=True
    )
    assert "Loves cold coffee" not in _contents(recall.profile)
    assert "Has a dog named Bruno" not in _contents(recall.profile)


# --- what bears on this message --------------------------------------------------------


@pytest.mark.asyncio
async def test_recall__relevant_ranked_by_similarity_freshness_confidence(memories):
    two_weeks_ago = NOW - timedelta(weeks=2)  # strength e^-2 = 0.135, above the floor
    unsure = await _add(memories, "Ran 10 km on Sunday", embedding=_at_similarity(0.9),
                        confidence=0.5)                                  # 0.9 * 1.0 * 0.5 = 0.45
    sure = await _add(memories, "Running with a club on Saturdays",
                      embedding=_at_similarity(0.6), confidence=0.9)    # 0.6 * 1.0 * 0.9 = 0.54
    stale = await _add(memories, "Marathon pace is 5:30 per km", embedding=_at_similarity(0.95),
                       confidence=0.9, strength_as_of=two_weeks_ago)    # 0.95 * 0.568 * 0.9 = 0.49
    await _add(memories, "Bought new shoes", embedding=_at_similarity(0.2))  # under the cut-off
    profile = await _add(memories, "Training for the Mumbai marathon in March", "goal",
                         embedding=_at_similarity(0.99))
    service = MemoryService(memories, _VectorEmbedder(), clock=lambda: NOW)

    recall = await service.recall("alice", QUERY, now=NOW)

    assert [m.id for m in recall.profile] == [profile.id]
    # Pure similarity would say stale, unsure, sure.
    assert [m.id for m in recall.relevant] == [sure.id, stale.id, unsure.id]
    assert recall.all() == [*recall.profile, *recall.relevant]
    assert _lines_tokens(recall.all()) <= recall.tokens  # the count never under-reports


@pytest.mark.asyncio
async def test_recall__trivial__profile_only_and_no_embedding_call(memories):
    await _add(memories, "Prefers studying in the mornings", "preference")
    await _add(memories, "My marathon training runs are at 6 AM", "fact")
    embedder = _CountingEmbedder()
    service = MemoryService(memories, embedder, clock=lambda: NOW)

    greeting = await service.recall("alice", "hi", trivial=True, now=NOW)
    wordless = await service.recall("alice", " 👋 !! ", now=NOW)

    for recall in (greeting, wordless):
        assert _contents(recall.profile) == ["Prefers studying in the mornings"]
        assert recall.relevant == []
    assert embedder.texts == []


@pytest.mark.asyncio
async def test_recall__embedder_down__profile_only_and_logged(memories, caplog):
    await _add(memories, "Prefers studying in the mornings", "preference")
    await _add(memories, "My marathon training runs are at 6 AM", "fact")
    service = MemoryService(memories, _DownEmbedder(), clock=lambda: NOW)

    with caplog.at_level(logging.WARNING):
        recall = await service.recall("alice", QUERY, now=NOW)

    assert _contents(recall.profile) == ["Prefers studying in the mornings"]
    assert recall.relevant == []
    assert "embedder" in caplog.text


# --- what never appears ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall__faded_or_expired__absent(memories):
    kept = await _add(memories, "Marathon training goal for March", "goal")
    await _add(memories, "Marathon training on the beach", "goal", status=MemoryStatus.FADED)
    await _add(memories, "Marathon training camp next week", "goal",
               expires_at=NOW - timedelta(hours=1))
    await _add(memories, "Marathon training with Ravi", "goal",
               strength_as_of=NOW - timedelta(weeks=3))  # e^-3 = 0.05, under the floor
    await _add(memories, "Marathon training is pointless", "goal", status=MemoryStatus.DISMISSED)
    await _add(memories, "", "goal", status=MemoryStatus.FORGOTTEN,
               embedding=BagOfWordsEmbedder().embed(["marathon training notes"])[0])
    old = await _add(memories, "Marathon training in the evenings", "goal")
    new = await _add(memories, "Marathon training in the mornings now", "goal")
    await memories.update("alice", old.id, superseded_by=new.id)
    # The same, offered as relevant (not profile) memories.
    await _add(memories, "Marathon training notes faded", status=MemoryStatus.FADED)
    await _add(memories, "Marathon training notes expired", expires_at=NOW - timedelta(minutes=1))
    await _add(memories, "Marathon training notes weak", strength_as_of=NOW - timedelta(weeks=3))
    service = MemoryService(memories, _CountingEmbedder(), clock=lambda: NOW)

    recall = await service.recall("alice", "marathon training notes", now=NOW)

    assert sorted(_contents(recall.all())) == sorted([kept.content, new.content])


@pytest.mark.asyncio
async def test_recall__hundred_memories__never_over_500_tokens(memories):
    filler = "with enough words in it to cost real tokens when it is shown to the model"
    for i in range(100):
        memory_type = ("constraint", "preference", "goal", "fact", "behavioral_pattern")[i % 5]
        await _add(
            memories,
            f"Marathon training memory {i} {filler}",
            memory_type,
            provenance=MemorySource.SAID if i % 2 else MemorySource.INFERRED,
            observation_count=1 + i % 4,
            created_at=NOW - timedelta(days=i % 20),
        )
    service = MemoryService(memories, _CountingEmbedder(), clock=lambda: NOW)

    recall = await service.recall("alice", "marathon training memory", now=NOW)

    assert recall.profile and recall.relevant
    assert _lines_tokens(recall.profile) <= config.MEMORY_PROFILE_TOKENS
    assert _lines_tokens(recall.relevant) <= config.MEMORY_RECALL_TOKENS
    assert _lines_tokens(recall.all()) <= 500
    assert recall.tokens <= 500
    assert len({m.id for m in recall.all()}) == len(recall.all())  # nothing shown twice
