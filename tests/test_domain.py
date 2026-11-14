"""The framework-free domain layer: entities, the draft state machine, ports and the event bus."""

import logging
import math
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.entities import DraftSchedule, DraftStatus, Task, TaskStatus
from app.domain.errors import InvalidStateTransition
from app.domain.events import (
    DomainEvent,
    EventBus,
    EventListener,
    NullEventBus,
    TaskCompleted,
    TaskSkipped,
)
from app.domain.ports import LLMProvider, MemoryRepository, TaskRepository, TextEmbedder

_NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _draft(status: DraftStatus) -> DraftSchedule:
    return DraftSchedule(id="d1", user_id="u1", tasks=[{"task_id": "t1"}], status=status)


def _task(**overrides) -> Task:
    fields = {
        "user_id": "u1",
        "task_id": "t1",
        "title": "Read chapter 3",
        "duration_minutes": 25,
        "difficulty_weight": 0.5,
        "status": TaskStatus.PENDING,
    }
    fields.update(overrides)
    return Task(**fields)


class _RecordingListener(EventListener):
    def __init__(self) -> None:
        self.received: list[DomainEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.received.append(event)


class _FailingListener(EventListener):
    async def handle(self, event: DomainEvent) -> None:
        raise RuntimeError("listener blew up")


# --- Draft state machine ---------------------------------------------------


def test_draft_transition__pending_to_accepted__ok():
    draft = _draft(DraftStatus.PENDING)

    accepted = draft.transition(DraftStatus.ACCEPTED)

    assert accepted.status == DraftStatus.ACCEPTED
    assert accepted.id == "d1"
    assert draft.status == DraftStatus.PENDING  # the original is not mutated


def test_draft_transition__accepted_to_accepted__raises_invalid_state_transition():
    draft = _draft(DraftStatus.ACCEPTED)

    with pytest.raises(InvalidStateTransition):
        draft.transition(DraftStatus.ACCEPTED)


def test_draft_transition__modified_to_modified__ok():
    draft = _draft(DraftStatus.MODIFIED)

    assert draft.transition(DraftStatus.MODIFIED).status == DraftStatus.MODIFIED


def test_open_draft_statuses__are_exactly_the_states_with_a_way_out():
    """One definition of "open", and it agrees with the state machine; the legacy
    DraftStore filters on the same states."""
    from app.domain.entities import DRAFT_TRANSITIONS, OPEN_DRAFT_STATUSES
    from app.infrastructure.persistence import _mapping
    from app.services import draft_store

    assert set(OPEN_DRAFT_STATUSES) == {s for s, targets in DRAFT_TRANSITIONS.items() if targets}
    assert _mapping.OPEN_DRAFT_STATUSES is OPEN_DRAFT_STATUSES
    assert draft_store._OPEN_STATUSES == [s.value for s in OPEN_DRAFT_STATUSES]


def test_draft_transition__raw_string__stored_as_the_status_enum():
    accepted = _draft(DraftStatus.PENDING).transition("accepted")

    assert accepted.status is DraftStatus.ACCEPTED
    assert accepted.status.value == "accepted"


def test_draft_transition__unknown_string__raises_and_never_becomes_a_status():
    with pytest.raises(ValueError):
        _draft(DraftStatus.PENDING).transition("archived")


def test_draft_transition__copy__shares_no_tasks_with_the_original():
    draft = _draft(DraftStatus.PENDING)

    moved = draft.transition(DraftStatus.MODIFIED)
    moved.tasks[0]["task_id"] = "changed"

    assert draft.tasks == [{"task_id": "t1"}]


# --- Task invariants --------------------------------------------------------


def test_task__zero_duration__rejected():
    with pytest.raises(ValidationError):
        _task(duration_minutes=0)


def test_task__difficulty_out_of_range__rejected():
    with pytest.raises(ValidationError):
        _task(difficulty_weight=1.5)
    with pytest.raises(ValidationError):
        _task(difficulty_weight=-0.1)


# --- Ports ------------------------------------------------------------------


def test_ports__instantiated_directly__raise_type_error():
    with pytest.raises(TypeError):
        TaskRepository()
    with pytest.raises(TypeError):
        LLMProvider()
    with pytest.raises(TypeError):
        EventListener()
    with pytest.raises(TypeError):
        MemoryRepository()
    with pytest.raises(TypeError):
        TextEmbedder()


# --- Event bus --------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bus__failing_listener__others_still_run(caplog):
    bus = EventBus()
    before, after = _RecordingListener(), _RecordingListener()
    bus.subscribe(TaskCompleted, before)
    bus.subscribe(TaskCompleted, _FailingListener())
    bus.subscribe(TaskCompleted, after)
    event = TaskCompleted(occurred_at=_NOW, user_id="u1", task_id="t1")

    with caplog.at_level(logging.ERROR, logger="app.domain.events"):
        await bus.publish(event)

    assert before.received == [event]
    assert after.received == [event]
    assert any("_FailingListener" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_event_bus__other_event_type__not_delivered():
    bus = EventBus()
    completed, skipped = _RecordingListener(), _RecordingListener()
    bus.subscribe(TaskCompleted, completed)
    bus.subscribe(TaskSkipped, skipped)
    event = TaskSkipped(occurred_at=_NOW, user_id="u1", task_id="t1", reason="tired")

    await bus.publish(event)

    assert completed.received == []
    assert skipped.received == [event]


@pytest.mark.asyncio
async def test_event_bus__base_type_subscription__receives_every_event_once():
    bus = EventBus()
    everything, completed = _RecordingListener(), _RecordingListener()
    bus.subscribe(DomainEvent, everything)
    bus.subscribe(TaskCompleted, completed)
    done = TaskCompleted(occurred_at=_NOW, user_id="u1", task_id="t1")
    skipped = TaskSkipped(occurred_at=_NOW, user_id="u1", task_id="t2", reason="tired")

    await bus.publish(done)
    await bus.publish(skipped)

    assert everything.received == [done, skipped]
    assert completed.received == [done]


@pytest.mark.asyncio
async def test_null_event_bus__publish__no_error():
    bus = NullEventBus()
    listener = _RecordingListener()
    bus.subscribe(TaskCompleted, listener)

    await bus.publish(TaskCompleted(occurred_at=_NOW, user_id="u1", task_id="t1"))

    assert listener.received == []


# --- value ranges -----------------------------------------------------------------


def _pattern(**overrides):
    from app.domain.entities import Pattern

    fields = {"detector": "skip_hour", "description": "Skips 9 AM", "occurrences": 3, "confidence": 0.7}
    fields.update(overrides)
    return Pattern(**fields)


@pytest.mark.parametrize("bad", [{"confidence": -0.1}, {"confidence": 1.01}, {"occurrences": 0}])
def test_pattern__out_of_range__rejected(bad):
    with pytest.raises(ValidationError):
        _pattern(**bad)


def test_pattern__range_edges__accepted():
    assert _pattern(confidence=0.0, occurrences=1).confidence == 0.0
    assert _pattern(confidence=1.0).confidence == 1.0


@pytest.mark.parametrize(
    "field", ["input_tokens", "output_tokens", "cache_read_tokens", "latency_ms"]
)
def test_llm_usage_event__negative_count__rejected(field):
    from app.domain.entities import LLMLocation, LLMUsageEvent

    fields = {
        "task": "voice_of_jarvis", "role": "fast", "provider": "local", "model": "m",
        "location": LLMLocation.LOCAL, "input_tokens": 0, "output_tokens": 0,
        "latency_ms": 0, "created_at": _NOW,
    }

    assert LLMUsageEvent(**fields).input_tokens == 0  # zero is a real count
    with pytest.raises(ValidationError):
        LLMUsageEvent(**{**fields, field: -1})


# --- Memory -----------------------------------------------------------------------


def _memory(**overrides):
    from app.domain.entities import Memory, MemorySource

    fields = {
        "user_id": "u1",
        "memory_type": "preference",
        "content": "Prefers deep work before noon",
        "provenance": MemorySource.SAID,
        "strength_as_of": _NOW,
        "created_at": _NOW,
    }
    fields.update(overrides)
    return Memory(**fields)


def test_memory__strength_at__decays_by_e_each_stability_week():
    from datetime import timedelta

    memory = _memory(strength=1.0, stability=1.0)
    sturdy = _memory(strength=1.0, stability=2.0)
    week = timedelta(hours=168)

    assert memory.strength_at(_NOW) == pytest.approx(1.0)
    assert memory.strength_at(_NOW + week) == pytest.approx(math.exp(-1))
    assert memory.strength_at(_NOW + 2 * week) == pytest.approx(math.exp(-2))
    assert sturdy.strength_at(_NOW + 2 * week) == pytest.approx(math.exp(-1))


def test_memory__confidence_out_of_range__rejected():
    with pytest.raises(ValidationError):
        _memory(confidence=1.01)
    with pytest.raises(ValidationError):
        _memory(confidence=-0.01)
    assert _memory(confidence=0.0).confidence == 0.0
    assert _memory(confidence=1.0).confidence == 1.0


def test_memory__blank_content__allowed_only_when_forgotten():
    from app.domain.entities import MemoryStatus

    assert _memory(content="", status=MemoryStatus.FORGOTTEN).content == ""
    for status in (MemoryStatus.ACTIVE, MemoryStatus.DISMISSED, MemoryStatus.FADED):
        with pytest.raises(ValidationError):
            _memory(content="", status=status)
    with pytest.raises(ValidationError):
        _memory(content="x" * 2001)
