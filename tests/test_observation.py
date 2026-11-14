# tests/test_observation.py
import asyncio
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import app.core.config as config
import app.core.observation as observation
from app.container import AppContainer
from app.core.observation import (
    detect_pearl_patterns,
    persist_turn_memories,
    run_observation_loop,
)
from app.core.runtime import set_container
from app.domain.entities import TaskEvent, TaskEventType, UserProfile
from app.domain.errors import EmbeddingUnavailableError
from app.orchestrator.hooks import ActionHooks
from app.orchestrator.state import ConversationPhase, NegotiationPhase
from app.schemas.context import BrainDumpExtraction
from app.schemas.memory import ExtractedMemory
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

IST = ZoneInfo("Asia/Kolkata")


def _make_state(**overrides):
    base = {
        "user_model": MagicMock(),
        "user_message": "plan my DSA study",
        "response_message": "Here's your schedule...",
        "brain_dump": None,
        "intent": "PLAN_DAY",
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "conversation_phase": ConversationPhase.PLANNING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": ["planning_module"],
        "needs_followup": False,
        "needs_consent": None,
        "error": None,
        "progress_callback": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_observation_loop_returns_state():
    state = _make_state()
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)
    result = await run_observation_loop(state)
    assert result is not None
    assert result.get("needs_followup") is False


# --- the turn's memories, remembered inline ---------------------------------------


class _SpyEmbedder(BagOfWordsEmbedder):
    """Bag-of-words vectors, recording every text it is asked to embed."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return super().embed(texts)


class _DownEmbedder(BagOfWordsEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingUnavailableError("model weights missing at /srv/models/minilm")


def _register(embedder) -> AppContainer:
    """The composition root over in-memory repositories, registered like the lifespan does."""
    container = AppContainer.build(
        supabase_client=None, cfg=config, hooks=ActionHooks(), embedder=embedder,
        install_chain=False,
    )
    set_container(container)
    return container


def _turn(*memories: dict, **overrides):
    """A non-trivial turn of alice's whose extraction returned ``memories``, and its queue."""
    queue: asyncio.Queue = asyncio.Queue()
    state = _make_state(**{
        "user_id": "alice",
        "session_id": str(uuid.uuid4()),
        "user_message_id": str(uuid.uuid4()),
        "memory_handles": {},
        "brain_dump": BrainDumpExtraction(memories=[ExtractedMemory(**m) for m in memories]),
        "progress_queue": queue,
        **overrides,
    })
    state["user_model"].user_id = "alice"
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)
    state["user_model"].get_profile = AsyncMock(return_value=None)  # no profile row
    return state, queue


def _drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    return events


MORNINGS = {"type": "preference", "content": "Prefers studying in the mornings", "confidence": 0.8}


@pytest.mark.asyncio
async def test_observation__stated_memory__frame_queued_and_no_task_left_running():
    _register(_SpyEmbedder())
    state, queue = _turn(MORNINGS)
    before = asyncio.all_tasks()

    result = await run_observation_loop(state)

    (frame,) = [e for e in _drain(queue) if e["_event_type"] == "memory_extracted"]
    assert {k: frame[k] for k in ("type", "content", "provenance", "action")} == {
        "type": "preference", "content": MORNINGS["content"], "provenance": "said",
        "action": "created",
    }
    assert result["memories_extracted_count"] == 1
    assert [t for t in asyncio.all_tasks() - before if not t.done()] == []


@pytest.mark.asyncio
async def test_observation__learning_phase__end_frame_distinct_from_start():
    """The start and end of the learning step reach the browser as ``phase`` frames with
    ``_event_type`` stripped: identical bodies made the end indistinguishable from the start.
    The end says ``status: done``, as a tool step's last frame does."""
    _register(_SpyEmbedder())
    state, queue = _turn(MORNINGS)

    await run_observation_loop(state)

    events = _drain(queue)
    (start,) = [e for e in events if e["_event_type"] == "phase_start"]
    (end,) = [e for e in events if e["_event_type"] == "phase_end"]
    wire = [{k: v for k, v in e.items() if k != "_event_type"} for e in (start, end)]
    assert wire == [{"phase": "learning"}, {"phase": "learning", "status": "done"}]


@pytest.mark.asyncio
async def test_observation__embedder_unavailable__turn_goes_on_with_no_memory_frame():
    embedder = _DownEmbedder()
    _register(embedder)
    marathon = {"type": "goal", "content": "Training for the Mumbai marathon in March"}
    state, queue = _turn(MORNINGS, marathon)

    result = await run_observation_loop(state)

    events = _drain(queue)
    assert embedder.calls == 1  # nothing else will embed either: the rest is not tried
    assert [e for e in events if e["_event_type"] == "memory_extracted"] == []
    assert result["memories_extracted_count"] == 0
    assert "minilm" not in json.dumps(events)  # no exception text reaches a frame


@pytest.mark.asyncio
async def test_persist_turn_memories__blank_or_wordless__never_embedded():
    embedder = _SpyEmbedder()
    _register(embedder)
    state, _queue = _turn(
        {"type": "fact", "content": "   "}, {"type": "fact", "content": "... !!"}
    )

    outcomes = await persist_turn_memories(state)

    assert embedder.texts == []
    assert [o.action for o in outcomes] == ["suppressed"]  # the wordless one; the blank skipped


@pytest.mark.asyncio
async def test_persist_turn_memories__source_ids_not_uuids__stored_without_them():
    _register(_SpyEmbedder())
    state, _queue = _turn(MORNINGS, session_id="local-session", user_message_id=None)

    (outcome,) = await persist_turn_memories(state)

    assert outcome.action == "created"
    assert (outcome.memory.source_session_id, outcome.memory.source_message_id) == (None, None)


@pytest.mark.asyncio
async def test_persist_turn_memories__date_only_expiry__ends_that_day_on_the_users_clock():
    _register(_SpyEmbedder())
    exam = {"type": "temporal_event", "content": "Calculus exam on 20 March", "expires_at": "2027-03-20"}
    state, _queue = _turn(exam)

    (outcome,) = await persist_turn_memories(state)

    assert outcome.memory.expires_at == datetime(2027, 3, 20, 23, 59, 59, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.mark.asyncio
async def test_persist_turn_memories__expiry__read_from_the_profile_without_creating_one():
    container = _register(_SpyEmbedder())
    container.repos.profiles.get_or_create = AsyncMock(
        side_effect=AssertionError("reading a timezone must not create a profile")
    )
    exam = {"type": "temporal_event", "content": "Calculus exam on 20 March", "expires_at": "2027-03-20"}
    state, _queue = _turn(exam)
    state["user_model"].get_profile = AsyncMock(
        return_value=UserProfile(user_id="alice", timezone="America/New_York")
    )

    (outcome,) = await persist_turn_memories(state)

    assert outcome.memory.expires_at == datetime(
        2027, 3, 20, 23, 59, 59, tzinfo=ZoneInfo("America/New_York")
    )


@pytest.mark.asyncio
async def test_persist_turn_memories__profile_read_fails__stored_without_an_expiry():
    _register(_SpyEmbedder())
    exam = {"type": "temporal_event", "content": "Calculus exam on 20 March", "expires_at": "2027-03-20"}
    state, _queue = _turn(exam)
    state["user_model"].get_profile = AsyncMock(side_effect=RuntimeError("profiles unreachable"))

    (outcome,) = await persist_turn_memories(state)

    assert outcome.action == "created"
    assert outcome.memory.expires_at is None


@pytest.mark.asyncio
async def test_detect_pearl_patterns__no_composition_root__nothing():
    state, _queue = _turn()
    state["user_model"]._db = FakeDBClient(FakeSupabase())

    assert await detect_pearl_patterns(state) == []


@pytest.mark.asyncio
async def test_persist_turn_memories__no_composition_root__learns_nothing():
    state, _queue = _turn(MORNINGS)

    assert await persist_turn_memories(state) == []


# --- PEARL's learning moments reach the turn, however slow the store ----------------


def _a_week_of_outcomes(user_id: str = "alice") -> list[TaskEvent]:
    """Seven days of 09:00 IST set-asides and 19:00 IST completions, ending yesterday."""
    events = []
    today = datetime.now(IST).date()
    for days_ago in range(1, 8):
        day = today - timedelta(days=days_ago)
        for hour, kind in ((9, TaskEventType.MISSED), (19, TaskEventType.COMPLETED)):
            start = datetime(day.year, day.month, day.day, hour, tzinfo=IST)
            events.append(TaskEvent(
                user_id=user_id, task_id=f"t_{days_ago}_{hour}", event_type=kind,
                occurred_at=start + timedelta(minutes=40), scheduled_start=start,
                scheduled_end=start + timedelta(minutes=25), metadata={"title": "Morning task"},
            ))
    return events


def _slow(monkeypatch, obj: object, name: str, seconds: float) -> None:
    """Every call of ``obj.name`` takes ``seconds`` longer: a store with real latency."""
    fast = getattr(obj, name)

    async def slow(*args, **kwargs):
        await asyncio.sleep(seconds)
        return await fast(*args, **kwargs)

    monkeypatch.setattr(obj, name, slow)


@pytest.mark.asyncio
async def test_observation__slow_store_outlasts_the_cap__held_moments_told_this_turn(monkeypatch):
    container = _register(_SpyEmbedder())
    await container.repos.task_events.append_many("alice", _a_week_of_outcomes())
    held = await container.patterns.observe("alice")  # PEARL after an outcome, in the background
    monkeypatch.setattr(observation, "PEARL_INLINE_TIMEOUT_S", 0.05)
    repos = container.repos
    for obj, name in (
        (repos.profiles, "get_or_create"),
        (repos.task_events, "list_for_user"),
        (repos.memories, "find_by_pattern_key"),
        (repos.memories, "find_by_pattern_keys"),
    ):
        _slow(monkeypatch, obj, name, 0.04)  # the turn's own look takes longer than the cap
    state, queue = _turn()

    result = await run_observation_loop(state)

    frames = [e for e in _drain(queue) if e["_event_type"] == "pattern_detected"]
    assert sorted(f["pattern_key"] for f in frames) == sorted(
        c.pattern.pattern_key for c in held
    ) == ["peak_hour:19", "skip_time_window:9"]
    assert result["patterns_detected_count"] == 2
    assert container.patterns.drain_pending("alice") == []  # told once
