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


@pytest.mark.asyncio
async def test_recall__memory_longer_than_the_budget__skipped_and_the_rest_fit(memories):
    huge = "Prefers " + "very " * 380 + "long study blocks"  # one line, far over 200 tokens
    await _add(memories, huge, "preference", created_at=NOW)  # newest: first in line
    await _add(memories, "Prefers studying in the mornings", "preference")
    await _add(memories, "Never schedules work on Sundays", "constraint")
    service = MemoryService(memories, _CountingEmbedder(), clock=lambda: NOW)

    recall = await service.recall("alice", "hi", trivial=True, now=NOW)

    assert estimate_tokens(render_memory_line(recall.profile[0], NOW)) < 200
    assert huge not in _contents(recall.profile)
    assert _contents(recall.profile) == [
        "Never schedules work on Sundays", "Prefers studying in the mornings",
    ]


def test_estimate_tokens__three_and_a_half_characters_a_token():
    from app.services.memory.context import CHARS_PER_TOKEN_ESTIMATE

    assert CHARS_PER_TOKEN_ESTIMATE == 3.5
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefg") == 2
    assert estimate_tokens("abcdefgh") == 3


# --- each part of a question -----------------------------------------------------------
# A question about two things averages their meaning in one embedding, and the memory
# for each part can fall under the bar. Recall looks for the whole message and for each
# of its clauses, and keeps a memory found a little under the bar when it shares a
# telling word with the message.


def _axis(index: int, similarity: float = 1.0) -> list[float]:
    """A unit vector at cosine ``similarity`` to axis ``index``, the rest of its length
    on the last axis (a direction no memory points along)."""
    vector = [0.0] * _DIMS
    vector[index] = similarity
    vector[-1] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


def _mixed(**on_axis: float) -> list[float]:
    """A unit vector with the given cosine to each named axis (``a0=0.6``), the rest of
    its length on the last axis."""
    vector = [0.0] * _DIMS
    for name, similarity in on_axis.items():
        vector[int(name[1:])] = similarity
    vector[-1] = math.sqrt(max(0.0, 1.0 - sum(v * v for v in vector)))
    return vector


class _MappedEmbedder(TextEmbedder):
    """Embeds each text it knows as its chosen vector, and records every call."""

    dimensions = _DIMS

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vectors[text] for text in texts]


def test_query_parts__two_questions_joined_by_and__whole_then_each_part():
    from app.services.memory.memory_service import _query_parts

    assert _query_parts("When is my gym, and what should I know about Thursdays?", 3) == [
        "When is my gym, and what should I know about Thursdays?",
        "When is my gym",
        "what should I know about Thursdays",
    ]


def test_query_parts__single_clause__whole_only():
    from app.services.memory.memory_service import _query_parts

    assert _query_parts("What do you know about Dr. Rao?", 3) == ["What do you know about Dr. Rao?"]
    # A fragment under three words is no clause of its own: one clause is the whole.
    assert _query_parts("Thanks, what do you know about Dr. Rao?", 3) == [
        "Thanks, what do you know about Dr. Rao?"
    ]


def test_query_parts__three_questions_with_no_and__whole_then_each_part():
    from app.services.memory.memory_service import _query_parts

    query = "When does the mess close? Who teaches Computer Networks; what is due on Friday?"
    assert _query_parts(query, 3) == [
        query, "When does the mess close", "Who teaches Computer Networks", "what is due on Friday",
    ]


def test_query_parts__more_clauses_than_the_cap__first_ones_once_each():
    from app.services.memory.memory_service import _query_parts

    query = (
        "when is my gym? When is my gym? who teaches networks, what is due friday,"
        " and where is lunch"
    )
    assert _query_parts(query, 3) == [
        query, "when is my gym", "who teaches networks", "what is due friday",
    ]
    assert _query_parts("gym timing? thursday quizzes?", 3) == ["gym timing? thursday quizzes?"]


@pytest.mark.asyncio
async def test_recall__three_part_question__memory_for_each_part_is_relevant(memories):
    query = "When does the mess close? Who teaches Computer Networks, and what is due on Friday?"
    parts = ["When does the mess close", "Who teaches Computer Networks", "what is due on Friday"]
    mess = await _add(memories, "Hostel mess closes at 9 PM", embedding=_axis(0))
    rao = await _add(memories, "Computer Networks is taught by Dr. Rao", embedding=_axis(1))
    friday = await _add(memories, "The OS assignment is due every Friday", embedding=_axis(2))
    await _add(memories, "The mess serves dosa on Sundays", embedding=_axis(0, 0.95),
               status=MemoryStatus.DISMISSED)  # close to a part, but let go of
    embedder = _MappedEmbedder({
        query: _mixed(a0=0.15, a1=0.15, a2=0.15),  # the whole: under both bars for each
        parts[0]: _mixed(a0=0.6),
        parts[1]: _mixed(a1=0.6),
        parts[2]: _mixed(a2=0.6),
    })
    service = MemoryService(memories, embedder, clock=lambda: NOW)

    recall = await service.recall("alice", query, now=NOW)

    assert sorted(m.id for m in recall.relevant) == sorted([mess.id, rao.id, friday.id])
    assert embedder.calls == [[query, *parts]]  # one embedding batch, no model call


@pytest.mark.asyncio
async def test_recall__two_part_question__memory_for_each_part_is_relevant(memories):
    query = "When is the mess open, and what should I know about Thursdays?"
    rao = await _add(
        memories, "Computer Networks professor is Dr. Rao, who gives surprise quizzes on Thursdays",
        embedding=_axis(0),
    )
    mess = await _add(memories, "Hostel mess closes at 9 PM", embedding=_axis(1))
    embedder = _MappedEmbedder({
        query: _mixed(a0=0.1, a1=0.1),
        "When is the mess open": _mixed(a1=0.5),
        "what should I know about Thursdays": _mixed(a0=0.45),
    })
    service = MemoryService(memories, embedder, clock=lambda: NOW)

    recall = await service.recall("alice", query, now=NOW)

    contents = [m.content for m in recall.relevant]
    assert any("Thursdays" in c for c in contents) and any("mess" in c for c in contents)
    assert [m.id for m in recall.relevant] == [mess.id, rao.id]  # 0.5 ranks over 0.45


@pytest.mark.asyncio
async def test_recall__rare_word_shared_with_a_memory_below_the_bar__still_relevant(memories):
    query = "anything about Thursdays?"
    quizzes = await _add(memories, "Dr. Rao gives surprise quizzes on Thursdays",
                         embedding=_at_similarity(0.25))   # under 0.3, a word in common
    await _add(memories, "Hostel mess closes at 9 PM",
               embedding=_at_similarity(0.25))             # under 0.3, no word in common
    await _add(memories, "Thursday evenings are for the chess club",
               embedding=_at_similarity(0.15))             # a word in common, under 0.2
    await _add(memories, "Knows about a lot of anything",
               embedding=_at_similarity(0.25))             # only stop words in common
    embedder = _MappedEmbedder({query: _at_similarity(1.0)})
    service = MemoryService(memories, embedder, clock=lambda: NOW)

    recall = await service.recall("alice", query, now=NOW)

    assert [m.id for m in recall.relevant] == [quizzes.id]


@pytest.mark.asyncio
async def test_recall__short_parts_with_no_and__found_by_their_words(memories):
    """"gym timing? thursday quizzes?": no clause is three words long, so the whole is
    the one embedding, and the memory it lands a little under the bar for is kept by the
    words it shares ("thursday", "quizzes")."""
    query = "gym timing? thursday quizzes?"
    quizzes = await _add(memories, "Dr. Rao gives surprise quizzes on Thursdays",
                         embedding=_at_similarity(0.22))
    await _add(memories, "Hostel mess closes at 9 PM", embedding=_at_similarity(0.22))
    embedder = _MappedEmbedder({query: _at_similarity(1.0)})
    service = MemoryService(memories, embedder, clock=lambda: NOW)

    recall = await service.recall("alice", query, now=NOW)

    assert [m.id for m in recall.relevant] == [quizzes.id]
    assert embedder.calls == [[query]]


@pytest.mark.asyncio
async def test_recall__same_memory_matched_by_two_parts__listed_once_at_its_best(memories):
    query = "When is my gym session, and how long is my gym session?"
    gym = await _add(memories, "Gym sessions last an hour", embedding=_axis(0))
    other = await _add(memories, "Sessions with the tutor are on Mondays", embedding=_axis(1))
    embedder = _MappedEmbedder({
        query: _mixed(a0=0.35, a1=0.5),
        "When is my gym session": _mixed(a0=0.4),
        "how long is my gym session": _mixed(a0=0.7),
    })
    service = MemoryService(memories, embedder, clock=lambda: NOW)

    recall = await service.recall("alice", query, now=NOW)

    # Found by all three, listed once, ranked by its best (0.7 over the tutor's 0.5).
    assert [m.id for m in recall.relevant] == [gym.id, other.id]


class _ReversedNearest:
    """The memory repository, answering ``nearest`` in the reverse order: a store whose
    order differs must not change the note."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def nearest(self, *args, **kwargs):
        return list(reversed(await self._inner.nearest(*args, **kwargs)))


@pytest.mark.asyncio
async def test_recall__same_inputs__same_note_bytes(memories):
    from zoneinfo import ZoneInfo

    from app.services.memory.context import render_context_note

    query = "When is the mess open, and what should I know about Thursdays?"
    await _add(memories, "Prefers studying in the mornings", "preference")
    for i, axis in enumerate((0, 0, 1, 1)):  # equal similarities: ties broken by id
        await _add(memories, f"Memory {i} about the mess and Thursdays", embedding=_axis(axis))
    vectors = {
        query: _mixed(a0=0.25, a1=0.25),
        "When is the mess open": _mixed(a1=0.5),
        "what should I know about Thursdays": _mixed(a0=0.5),
    }
    zone = ZoneInfo("Asia/Kolkata")

    notes = []
    for store in (memories, _ReversedNearest(memories), memories):
        service = MemoryService(store, _MappedEmbedder(vectors), clock=lambda: NOW)
        recall = await service.recall("alice", query, now=NOW)
        notes.append(render_context_note(recall, now=NOW, zone=zone).encode())

    assert len(set(notes)) == 1
    assert notes[0].count(b"about the mess and Thursdays") == 4
