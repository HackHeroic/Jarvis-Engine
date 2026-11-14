"""The You page's one read: what Jarvis knows about the user, and their settings.

``UserModelService.snapshot`` runs PEARL's idempotent observe first, so every pattern
shown has a memory the user can confirm or dismiss. Patterns carry their evidence in
anti-guilt words; memories keep provenance and are measured for freshness at the
snapshot's instant. Nothing of another user's ever appears.

InMemory repositories, a fixed clock, and the user's clock in India.
"""

import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.entities import (
    AutonomyMode,
    BehavioralConstraint,
    LLMLocation,
    LLMUsageEvent,
    Memory,
    MemorySource,
    MemoryStatus,
    TaskEvent,
    TaskEventType,
)
from app.domain.events import EventBus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from app.services.memory.patterns import PatternService
from app.services.usage_ledger import UsageLedger
from app.services.user_model_service import UserModelService
from tests.behaviour.conftest import blame_phrases_in
from tests.fakes import BagOfWordsEmbedder

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=IST)  # Monday morning, on the user's clock
EVERY_STATUS = frozenset(MemoryStatus)
MARATHON = "Training for the Mumbai marathon in March"
SHORT_AFTER_LUNCH = "Prefers short tasks right after lunch"
ZERO_RECEIPT = {
    "since": NOW - timedelta(days=30),
    "truncated": False,
    "on_device_calls": 0,
    "cloud_calls": 0,
    "pii_redactions": 0,
    "input_tokens": 0,
    "output_tokens": 0,
}


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _slot(days_ago: int, hour: int) -> datetime:
    day = (NOW - timedelta(days=days_ago)).date()
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)


def _outcome(kind: str, slot: datetime, *, user_id: str = "alice") -> TaskEvent:
    return TaskEvent(
        user_id=user_id,
        task_id=f"t_{slot:%m%d_%H%M}",
        event_type=TaskEventType(kind),
        occurred_at=slot + timedelta(minutes=25),
        scheduled_start=slot,
        scheduled_end=slot + timedelta(minutes=25),
        metadata={"title": "Morning task", "goal_id": "g1"},
    )


def _usage(
    user_id: str, location: LLMLocation, redactions: int = 0, *, at: datetime = NOW
) -> LLMUsageEvent:
    return LLMUsageEvent(
        user_id=user_id,
        task="voice_of_jarvis",
        role="fast",
        provider="local" if location is LLMLocation.LOCAL else "claude",
        model="m",
        location=location,
        input_tokens=100,
        output_tokens=20,
        latency_ms=50,
        pii_redactions=redactions,
        created_at=at,
    )


@pytest.fixture
def clock() -> _Clock:
    return _Clock(NOW)


@pytest.fixture
def repos():
    return RepositoryFactory.in_memory()


@pytest.fixture
def memory(repos, clock) -> MemoryService:
    return MemoryService(repos.memories, BagOfWordsEmbedder(), clock=clock)


@pytest.fixture
def service(repos, memory, clock) -> UserModelService:
    patterns = PatternService(
        repos.task_events, repos.memories, repos.profiles, EventBus(), clock=clock
    )
    return UserModelService(
        repos.profiles,
        memory,
        patterns,
        repos.constraints,
        HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator()),
        UsageLedger(repos.usage),
        clock=clock,
    )


async def _seed(repos, *events: TaskEvent) -> None:
    for event in events:
        await repos.task_events.append(event.user_id, event)


async def _seed_bob(repos, memory) -> None:
    """Bob has a history, a pattern, a memory, a rule and model calls."""
    await _seed(repos, *[_outcome("skipped", _slot(d, 9), user_id="bob") for d in (3, 2, 1)])
    await memory.remember("bob", MemoryCandidate(memory_type="goal", content=MARATHON))
    await repos.constraints.add("bob", BehavioralConstraint(
        id="00000000-0000-4000-8000-00000000b0b0", user_id="bob",
        raw_text="Gym at 6 every morning", constraint_type="exercise",
    ))
    await repos.usage.record(_usage("bob", LLMLocation.CLOUD, redactions=2))


@pytest.mark.asyncio
async def test_snapshot__new_user__empty_sections_prior_focus_zero_receipt(
    service, repos, memory
):
    await _seed_bob(repos, memory)  # someone else's data exists; none of it is Alice's

    view = await service.snapshot("alice")

    assert (view.patterns, view.rules, view.memories) == ([], [], [])
    assert (view.focus.source, view.focus.observations) == ("prior", 0)
    assert view.focus.scores() == [HeuristicEnergyEstimator().score(h) for h in range(24)]
    assert view.receipt.model_dump() == ZERO_RECEIPT
    assert view.profile.user_id == "alice"
    assert (view.profile.timezone, view.profile.wake_time) == ("Asia/Kolkata", time(7, 0))
    assert view.as_of == NOW
    assert await repos.memories.list("alice", statuses=EVERY_STATUS) == []


@pytest.mark.asyncio
async def test_snapshot__history__patterns_with_evidence_and_memory_ids(service, repos):
    await _seed(
        repos,
        *[_outcome("skipped", _slot(d, 9)) for d in (4, 3, 2)],
        _outcome("missed", _slot(1, 9)),
    )

    view = await service.snapshot("alice")
    again = await service.snapshot("alice")

    (pattern,) = [p for p in view.patterns if p.pattern_key == "skip_time_window:9"]
    remembered = await repos.memories.find_by_pattern_key("alice", "skip_time_window:9")
    assert remembered is not None  # observe ran first: the pattern has a memory to act on
    assert pattern.memory_id == remembered.id
    assert (pattern.detector, pattern.occurrences, pattern.confidence) == (
        "skip_time_window", 4, 1.0
    )
    assert pattern.description == "You tend to set work aside around 9 AM"
    assert pattern.evidence == [  # newest first, local time, never "missed" or "skipped"
        "set aside Sun 15 Nov 09:00 · Morning task",
        "set aside Sat 14 Nov 09:00 · Morning task",
        "set aside Fri 13 Nov 09:00 · Morning task",
        "set aside Thu 12 Nov 09:00 · Morning task",
    ]
    assert pattern.confirmed is False
    for line in [pattern.description, *pattern.evidence]:
        assert "missed" not in line and "skipped" not in line
        assert blame_phrases_in(line) == []
    assert again.patterns == view.patterns  # looking again teaches nothing twice
    (row,) = await repos.memories.list("alice", statuses=EVERY_STATUS)
    assert row.observation_count == 4


@pytest.mark.asyncio
async def test_snapshot__dismissed_pattern__absent(service, repos, memory, clock):
    await _seed(repos, *[_outcome("skipped", _slot(d, 9)) for d in (3, 2, 1)])
    (shown,) = (await service.snapshot("alice")).patterns
    await memory.dismiss("alice", shown.memory_id)
    await _seed(repos, _outcome("skipped", _slot(0, 9)))  # new evidence for the same habit
    clock.advance(hours=3)

    view = await service.snapshot("alice")

    assert [p.pattern_key for p in view.patterns] == []
    assert view.memories == []
    (row,) = await repos.memories.list("alice", statuses=EVERY_STATUS)
    assert (row.status, row.observation_count) == (MemoryStatus.DISMISSED, 3)


@pytest.mark.asyncio
async def test_snapshot__memories__provenance_and_freshness(service, repos, memory, clock):
    said = (await memory.remember(
        "alice", MemoryCandidate(memory_type="goal", content=MARATHON, confidence=0.8)
    )).memory
    noticed = (await memory.remember(
        "alice",
        MemoryCandidate(memory_type="preference", content=SHORT_AFTER_LUNCH, confidence=0.6),
        provenance=MemorySource.INFERRED,
        origin="behavior",
    )).memory
    habit = (await memory.remember(  # a pattern PEARL does not see now: listed, as noticed
        "alice",
        MemoryCandidate(memory_type="behavioral_pattern", content="Skips work around 9 AM"),
        provenance=MemorySource.INFERRED,
        origin="behavior",
    )).memory
    clock.advance(days=7)  # a week unused: strength falls by a factor of e

    view = await service.snapshot("alice")

    assert [m.id for m in view.memories] == [said.id, noticed.id, habit.id]  # oldest first
    assert [m.provenance for m in view.memories] == [
        MemorySource.SAID, MemorySource.INFERRED, MemorySource.INFERRED
    ]
    assert view.as_of == NOW + timedelta(days=7)
    assert [m.strength_at(view.as_of) for m in view.memories] == pytest.approx(
        [math.exp(-1)] * 3
    )


@pytest.mark.asyncio
async def test_snapshot__other_users_data__never_included(service, repos, memory):
    await _seed_bob(repos, memory)
    await repos.constraints.add("alice", BehavioralConstraint(
        id="00000000-0000-4000-8000-0000000a11ce", user_id="alice",
        raw_text="No work after 10 PM", constraint_type="sleep", source="said",
    ))

    alice = await service.snapshot("alice")
    bob = await service.snapshot("bob")

    assert [(r.text, r.provenance) for r in alice.rules] == [("No work after 10 PM", "said")]
    assert alice.rules[0].created_at is not None  # stamped by the store: "noted N days ago"
    assert (alice.patterns, alice.memories) == ([], [])
    assert alice.focus.observations == 0
    assert alice.receipt.model_dump() == ZERO_RECEIPT
    # Bob's own snapshot has all of it: the filter is by user, not an empty store.
    assert [p.pattern_key for p in bob.patterns] == ["skip_time_window:9"]
    assert [m.content for m in bob.memories] == [MARATHON]
    assert [r.text for r in bob.rules] == ["Gym at 6 every morning"]
    assert (bob.receipt.cloud_calls, bob.receipt.pii_redactions) == (1, 2)
    assert bob.focus.observations == 3


@pytest.mark.asyncio
async def test_update_settings__autonomy_and_wake_time__persisted(service, repos):
    updated = await service.update_settings(
        "alice", autonomy_mode=AutonomyMode.SUGGEST_ONLY, wake_time=time(6, 30)
    )

    stored = await repos.profiles.get_or_create("alice")
    assert stored == updated
    assert (stored.autonomy_mode, stored.wake_time) == (AutonomyMode.SUGGEST_ONLY, time(6, 30))
    assert stored.timezone == "Asia/Kolkata"  # untouched: it was not given
    moved = await service.update_settings("alice", timezone="Europe/London")
    assert (moved.timezone, moved.wake_time) == ("Europe/London", time(6, 30))
    assert (await repos.profiles.get_or_create("bob")).autonomy_mode is AutonomyMode.ASK_FIRST


@pytest.mark.asyncio
async def test_snapshot__pattern_memory_without_recent_evidence__listed_as_noticed(
    service, repos, clock
):
    """A learned skip window still shapes plans after its evidence ages out of the window:
    it stays visible (under memories, as noticed) so it can be confirmed or dismissed."""
    old = NOW - timedelta(days=40)
    kept = await repos.memories.add("alice", Memory(
        user_id="alice", memory_type="behavioral_pattern",
        content="User avoids tasks during hour 9 (3 of 3 set aside)",
        provenance=MemorySource.INFERRED, origin="behavior", pattern_key="skip_time_window:9",
        applied_as="soft_preference", observation_count=3, stability=20.0,
        strength_as_of=old, created_at=old,
    ))
    await _seed(repos, *[_outcome("skipped", _slot(d, 9)) for d in (42, 41, 40)])  # too old
    await _seed(repos, *[_outcome("skipped", _slot(d, 14)) for d in (3, 2, 1)])  # seen now

    view = await service.snapshot("alice")

    assert [p.pattern_key for p in view.patterns] == ["skip_time_window:14"]
    assert [(m.id, m.provenance) for m in view.memories] == [(kept.id, MemorySource.INFERRED)]
    assert 0 < kept.strength_at(view.as_of) < 1  # its freshness is shown, not hidden


@pytest.mark.asyncio
async def test_snapshot__receipt__last_30_days_and_says_so(service, repos):
    await repos.usage.record(_usage("alice", LLMLocation.LOCAL))
    await repos.usage.record(_usage("alice", LLMLocation.CLOUD, redactions=1,
                                    at=NOW - timedelta(days=29)))
    await repos.usage.record(_usage("alice", LLMLocation.CLOUD, redactions=5,
                                    at=NOW - timedelta(days=31)))  # outside the window

    receipt = (await service.snapshot("alice")).receipt

    assert receipt.since == NOW - timedelta(days=30)
    assert (receipt.on_device_calls, receipt.cloud_calls, receipt.pii_redactions) == (1, 1, 1)
    assert receipt.truncated is False
