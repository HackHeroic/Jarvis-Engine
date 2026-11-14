"""The framework-free domain layer: entities, the draft state machine, ports and the event bus."""

import logging
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
from app.domain.ports import LLMProvider, TaskRepository

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


def test_ports__are_abstract():
    with pytest.raises(TypeError):
        TaskRepository()
    with pytest.raises(TypeError):
        LLMProvider()
    with pytest.raises(TypeError):
        EventListener()


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
async def test_event_bus__only_subscribed_type_delivered():
    bus = EventBus()
    completed, skipped = _RecordingListener(), _RecordingListener()
    bus.subscribe(TaskCompleted, completed)
    bus.subscribe(TaskSkipped, skipped)
    event = TaskSkipped(occurred_at=_NOW, user_id="u1", task_id="t1", reason="tired")

    await bus.publish(event)

    assert completed.received == []
    assert skipped.received == [event]


@pytest.mark.asyncio
async def test_null_event_bus__publish__no_error():
    bus = NullEventBus()
    listener = _RecordingListener()
    bus.subscribe(TaskCompleted, listener)

    await bus.publish(TaskCompleted(occurred_at=_NOW, user_id="u1", task_id="t1"))

    assert listener.received == []
