"""PEARL on the outcome history: detectors are strategies over ``task_events``.

An outcome is a completed, skipped or missed slot, bucketed by the local hour it was
scheduled at; a slot that has two outcomes (missed, then completed after all) counts
once, as what happened last. ``PatternService.observe`` remembers what the detectors
find, keyed by pattern so the same habit is recognised. It strengthens a pattern when
an outcome behind it is newer than the memory, and never brings back one the user
dismissed or forgot. What it learns is held for the next turn to tell
(``drain_pending``), whoever ran it. ``PatternObserver`` runs it in the background
after each outcome, at most once at a time per user.

InMemory repositories, a fixed clock, and the user's clock in India.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

import app.core.config as _cfg
from app.container import AppContainer
from app.domain.entities import (
    Memory,
    MemorySource,
    MemoryStatus,
    Pattern,
    Task,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from app.domain.events import (
    DeadlineMissed,
    DomainEvent,
    EventBus,
    EventListener,
    PatternDetected,
    TaskCompleted,
    TaskMissed,
    TaskSkipped,
)
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryMemoryRepository, InMemoryStore
from app.orchestrator.hooks import ActionHooks
from app.services.memory.memory_service import MemoryService
from app.services.memory.patterns import (
    DeadlineCrunchDetector,
    PatternDetector,
    PatternObserver,
    PatternService,
    PeakHourDetector,
    RepeatedSkipDetector,
    SkipTimeWindowDetector,
)
from app.services.task_service import TaskService
from tests.fakes import BagOfWordsEmbedder

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=IST)  # Monday morning, on the user's clock
EVERY_STATUS = frozenset(MemoryStatus)
OUTCOMES = (TaskCompleted, TaskSkipped, TaskMissed)


def _slot(days_ago: int, hour: int, minute: int = 0) -> datetime:
    """``hour``:``minute`` IST, ``days_ago`` days before NOW."""
    day = (NOW - timedelta(days=days_ago)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def _outcome(
    kind: str,
    slot: datetime,
    *,
    task_id: str | None = None,
    user_id: str = "alice",
    title: str = "Morning task",
    deadline_hint: str | None = None,
    occurred_at: datetime | None = None,
) -> TaskEvent:
    """An outcome event as TaskService records it, for a 25-minute slot at ``slot``."""
    return TaskEvent(
        user_id=user_id,
        task_id=task_id or f"t_{slot.astimezone(IST):%m%d_%H%M}",
        event_type=TaskEventType(kind),
        occurred_at=occurred_at or slot + timedelta(minutes=25),
        scheduled_start=slot,
        scheduled_end=slot + timedelta(minutes=25),
        metadata={"title": title, "goal_id": "g1", "deadline_hint": deadline_hint},
    )


def _pending(slot: datetime, task_id: str, *, user_id: str = "alice") -> Task:
    """A pending task as draft acceptance stores it, slot instants in UTC."""
    start = slot.astimezone(timezone.utc)
    return Task(
        user_id=user_id,
        task_id=task_id,
        goal_id="g1",
        title="Morning task",
        duration_minutes=25,
        difficulty_weight=0.5,
        status=TaskStatus.PENDING,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=25),
    )


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class _Recorder(EventListener):
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.events.append(event)


class _Fixed(PatternDetector):
    """A detector that finds exactly the patterns it is given."""

    name = "fixed"

    def __init__(self, patterns: list[Pattern]) -> None:
        self.patterns = patterns

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        return list(self.patterns)


def _fixed(key: str, *, seen: datetime, occurrences: int = 3) -> Pattern:
    return Pattern(
        detector="fixed", description=f"Pattern {key} ({occurrences})", occurrences=occurrences,
        confidence=0.8, pattern_key=key, last_observed_at=seen,
    )


class _GatedObserve:
    """Stands in for ``PatternService.observe``: counts its calls, waits at a gate."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self, user_id: str) -> list:
        self.calls.append(user_id)
        self.entered.set()
        await self.gate.wait()
        return []


@pytest.fixture
def clock() -> _Clock:
    return _Clock(NOW)


@pytest.fixture
def repos():
    return RepositoryFactory.in_memory()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def detected(bus) -> _Recorder:
    recorder = _Recorder()
    bus.subscribe(PatternDetected, recorder)
    return recorder


@pytest.fixture
def service(repos, bus, clock) -> PatternService:
    return PatternService(repos.task_events, repos.memories, repos.profiles, bus, clock=clock)


@dataclass
class _Wired:
    """PEARL as the container wires it: TaskService publishes, the observer listens."""

    store: InMemoryStore
    repos: Repositories
    bus: EventBus
    patterns: PatternService
    observer: PatternObserver
    tasks: TaskService


@pytest_asyncio.fixture
async def wired(clock):
    store = InMemoryStore()
    repos = RepositoryFactory.in_memory(store)
    bus = EventBus()
    patterns = PatternService(repos.task_events, repos.memories, repos.profiles, bus, clock=clock)
    observer = PatternObserver(patterns)
    for outcome in OUTCOMES:
        bus.subscribe(outcome, observer)
    yield _Wired(
        store, repos, bus, patterns, observer,
        TaskService(repos.tasks, repos.task_events, bus, clock=clock),
    )
    await observer.shutdown(grace_s=0)


async def _seed(repos, *events: TaskEvent) -> None:
    for event in events:
        await repos.task_events.append(event.user_id, event)


def _skips_at_nine(*days_ago: int, user_id: str = "alice") -> list[TaskEvent]:
    return [_outcome("skipped", _slot(d, 9), user_id=user_id) for d in days_ago]


async def _until(condition, *, tries: int = 100) -> None:
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


# --- detectors -----------------------------------------------------------------------


def test_skip_window__three_of_three_at_nine__pattern_hour_9():
    (pattern,) = SkipTimeWindowDetector().detect(_skips_at_nine(3, 2, 1), IST)

    assert pattern.detector == "skip_time_window"
    assert pattern.pattern_key == "skip_time_window:9"
    assert pattern.hour == 9
    assert (pattern.occurrences, pattern.confidence) == (3, 1.0)
    assert pattern.description == "User avoids tasks during hour 9 (3 of 3 set aside)"
    assert pattern.evidence == [  # newest first, on the user's clock, in the user's words
        "set aside Sun 15 Nov 09:00 · Morning task",
        "set aside Sat 14 Nov 09:00 · Morning task",
        "set aside Fri 13 Nov 09:00 · Morning task",
    ]
    assert pattern.last_observed_at == _slot(1, 9, 25)  # when the newest one was recorded


def test_skip_window__two_of_three__nothing():
    events = [*_skips_at_nine(3, 2), _outcome("completed", _slot(1, 9))]

    assert SkipTimeWindowDetector().detect(events, IST) == []


def test_skip_window__missed_counts_as_set_aside():
    events = [*_skips_at_nine(3, 2), _outcome("missed", _slot(1, 9))]

    (pattern,) = SkipTimeWindowDetector().detect(events, IST)

    assert pattern.description == "User avoids tasks during hour 9 (3 of 3 set aside)"
    assert pattern.evidence[0] == "set aside Sun 15 Nov 09:00 · Morning task"
    assert not [line for line in pattern.evidence if "missed" in line or "skipped" in line]


def test_skip_window__missed_then_completed_same_slot__counts_once_as_done():
    late = _outcome("completed", _slot(1, 9), occurred_at=_slot(1, 18))
    events = [*_skips_at_nine(3, 2), _outcome("missed", _slot(1, 9)), late]

    assert SkipTimeWindowDetector().detect(events, IST) == []  # 2 of 3, not 3 of 4


def test_skip_window__utc_timestamps__bucketed_by_local_hour():
    # 03:30 UTC is 09:00 in India: stored instants are UTC, the hour is the user's.
    events = [
        _outcome("skipped", datetime(2026, 11, day, 3, 30, tzinfo=timezone.utc))
        for day in (12, 13, 14)
    ]

    (pattern,) = SkipTimeWindowDetector().detect(events, IST)

    assert (pattern.hour, pattern.pattern_key) == (9, "skip_time_window:9")


def test_peak_hour__evening_completions__exactly_one_pattern():
    events = [
        *[_outcome("completed", _slot(d, 19)) for d in (4, 3, 2, 1)],           # 4 of 4
        *[_outcome("completed", _slot(d, 20)) for d in (5, 4, 3, 2)],           # 4 of 5
        _outcome("skipped", _slot(1, 20)),
    ]

    (pattern,) = PeakHourDetector().detect(events, IST)

    assert (pattern.detector, pattern.pattern_key, pattern.hour) == ("peak_hour", "peak_hour:19", 19)
    assert pattern.description == "User does their best work around hour 19 (4 of 4 done)"
    assert (pattern.occurrences, pattern.confidence) == (4, 1.0)
    assert pattern.evidence[0] == "done Sun 15 Nov 19:00 · Morning task"


def test_deadline_crunch__last_day_completions__pattern():
    events = [
        # A date-only deadline passes at 23:59:59 on the user's clock.
        _outcome("completed", _slot(6, 21), title="Essay", deadline_hint="2026-11-10"),
        _outcome("completed", _slot(4, 23), title="Lab", deadline_hint="2026-11-12"),
        _outcome("completed", _slot(2, 10), title="Quiz", deadline_hint="2026-11-15T09:00:00+05:30"),
        _outcome("completed", _slot(9, 10), title="Slides", deadline_hint="2026-11-12"),  # 3 days early
        _outcome("completed", _slot(1, 10), title="Notes", deadline_hint="someday"),      # not a date
    ]

    (pattern,) = DeadlineCrunchDetector().detect(events, IST)

    assert (pattern.detector, pattern.pattern_key, pattern.hour) == (
        "deadline_crunch", "deadline_crunch", None
    )
    assert pattern.description == "User tends to finish deadlined work in the last day (3 of 4)"
    assert (pattern.occurrences, pattern.confidence) == (4, 0.75)
    assert pattern.evidence == [  # when each was finished
        "done Sat 14 Nov 10:25 · Quiz",
        "done Thu 12 Nov 23:25 · Lab",
        "done Tue 10 Nov 21:25 · Essay",
    ]


def test_repeated_skip__same_task_three_times__pattern_names_title():
    events = [
        _outcome("skipped", _slot(d, hour), task_id="essay_t1", title="Draft the essay intro")
        for d, hour in ((5, 10), (3, 14), (1, 18))
    ]

    (pattern,) = RepeatedSkipDetector().detect(events, IST)

    assert (pattern.detector, pattern.pattern_key) == ("repeated_skip", "repeated_skip:essay_t1")
    assert pattern.description == (
        "'Draft the essay intro' has been set aside 3 times; smaller steps may help"
    )
    assert (pattern.occurrences, pattern.confidence) == (3, 1.0)


# --- observe -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe__first_time__created_and_pattern_detected_published(
    service, repos, detected
):
    await _seed(repos, *_skips_at_nine(3, 2, 1))

    (change,) = await service.observe("alice")

    assert change.action == "created"
    memory = await repos.memories.get("alice", change.memory_id)
    assert memory.memory_type == "behavioral_pattern"
    assert (memory.provenance, memory.origin) == (MemorySource.INFERRED, "behavior")
    assert memory.pattern_key == "skip_time_window:9"
    assert memory.content == "User avoids tasks during hour 9 (3 of 3 set aside)"
    assert (memory.confidence, memory.observation_count) == (0.9, 3)  # capped below certainty
    assert memory.applied_as == "soft_preference"
    assert [(e.user_id, e.pattern) for e in detected.events] == [("alice", change.pattern)]


@pytest.mark.asyncio
async def test_observe__no_new_evidence__no_change_no_reinforcement(
    service, repos, detected, clock
):
    await _seed(repos, *_skips_at_nine(3, 2, 1))
    (created,) = await service.observe("alice")
    before = await repos.memories.get("alice", created.memory_id)
    clock.advance(days=1)

    assert await service.observe("alice") == []
    assert await service.observe("alice") == []  # the same history, looked at again

    assert await repos.memories.get("alice", created.memory_id) == before
    assert len(detected.events) == 1


@pytest.mark.asyncio
async def test_observe__more_evidence__strengthened(service, repos, detected, clock):
    await _seed(repos, *_skips_at_nine(3, 2, 1))
    (created,) = await service.observe("alice")
    await repos.memories.update(
        "alice", created.memory_id, status=MemoryStatus.FADED, strength=0.05
    )
    clock.advance(days=1)
    await _seed(repos, *_skips_at_nine(0))

    (change,) = await service.observe("alice")

    assert (change.action, change.memory_id) == ("strengthened", created.memory_id)
    memory = await repos.memories.get("alice", created.memory_id)
    assert memory.content == "User avoids tasks during hour 9 (4 of 4 set aside)"
    assert (memory.observation_count, memory.confidence) == (4, 0.9)
    assert (memory.status, memory.strength, memory.strength_as_of) == (
        MemoryStatus.ACTIVE, 1.0, clock.now
    )
    assert [e.pattern.occurrences for e in detected.events] == [3, 4]


@pytest.mark.asyncio
async def test_observe__steady_habit_over_30_days__stays_active(service, repos, clock):
    """The window slides: a steady habit never beats its old count (and the soft block
    thins the evidence), so a new outcome behind it, not a new high, is what renews it."""
    memories = MemoryService(repos.memories, BagOfWordsEmbedder(), clock=clock)
    # A weeks-old habit: the 09:00 slot set aside every fourth day for four weeks.
    await _seed(repos, *_skips_at_nine(26, 22, 18, 14, 10, 6, 2))
    (created,) = await service.observe("alice")

    for day in range(1, 31):
        clock.now = NOW + timedelta(days=day)  # 08:00: the daily decay sweep
        await memories.fade_stale("alice", clock.now)
        if day % 4 == 2:  # and it carries on: the window still holds 7 of 7
            await _seed(repos, _outcome("skipped", _slot(-day, 9)))
            clock.advance(hours=2)
            await service.observe("alice")

    memory = await repos.memories.get("alice", created.memory_id)
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.observation_count == 7
    assert memory.strength_at(clock.now) == 1.0  # renewed this morning


@pytest.mark.asyncio
async def test_observe__habit_stopped__never_strengthened(service, repos, detected, clock):
    await _seed(repos, *_skips_at_nine(9, 8, 7, 6, 5, 4, 3))
    (created,) = await service.observe("alice")
    before = await repos.memories.get("alice", created.memory_id)
    clock.advance(days=3)
    # The user does the 09:00 work now. The hour is still 7 of 9 set aside, and more
    # outcomes than before, but nothing behind the pattern is new.
    await _seed(repos, *[_outcome("completed", _slot(d, 9)) for d in (-1, -2)])

    assert await service.observe("alice") == []

    assert await repos.memories.get("alice", created.memory_id) == before
    assert len(detected.events) == 1


@pytest.mark.parametrize("let_go", ["dismiss", "forget"])
@pytest.mark.asyncio
async def test_observe__dismissed_key__never_recreated(
    service, repos, detected, clock, let_go
):
    await _seed(repos, *_skips_at_nine(3, 2, 1))
    (created,) = await service.observe("alice")
    await getattr(MemoryService(repos.memories, BagOfWordsEmbedder()), let_go)(
        "alice", created.memory_id
    )
    clock.advance(days=2)
    await _seed(repos, *_skips_at_nine(0, -1))  # the habit carries on

    assert await service.observe("alice") == []

    (row,) = await repos.memories.list("alice", statuses=EVERY_STATUS)
    assert row.id == created.memory_id
    assert row.status in {MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN}
    assert len(detected.events) == 1


@pytest.mark.asyncio
async def test_observe__dismissed_while_observing__stays_dismissed(
    service, repos, detected, clock, monkeypatch
):
    """The user's dismissal wins a race with PEARL: a strengthen that read the pattern
    before the dismissal changes nothing."""
    await _seed(repos, *_skips_at_nine(3, 2, 1))
    (created,) = await service.observe("alice")
    read_before_the_dismissal = await repos.memories.get("alice", created.memory_id)
    await MemoryService(repos.memories, BagOfWordsEmbedder()).dismiss("alice", created.memory_id)

    async def stale_read(user_id: str, pattern_key: str) -> Memory:
        return read_before_the_dismissal

    monkeypatch.setattr(repos.memories, "find_by_pattern_key", stale_read)
    clock.advance(days=1)
    await _seed(repos, *_skips_at_nine(0))

    assert await service.observe("alice") == []

    memory = await repos.memories.get("alice", created.memory_id)
    assert (memory.status, memory.observation_count) == (MemoryStatus.DISMISSED, 3)
    assert len(detected.events) == 1


@pytest.mark.asyncio
async def test_observe__key_remembered_by_a_concurrent_observe__left_to_it_rest_still_learned(
    service, repos, detected, monkeypatch
):
    """A Skip and a chat turn can observe at once: the one that loses the race for a
    pattern key leaves that pattern to the winner and still learns the others."""
    await _seed(
        repos,
        *_skips_at_nine(3, 2, 1),
        *[_outcome("completed", _slot(d, 19)) for d in (3, 2, 1)],
    )
    await repos.memories.add("alice", Memory(  # the winner's write
        user_id="alice", memory_type="behavioral_pattern", provenance=MemorySource.INFERRED,
        content="User avoids tasks during hour 9 (3 of 3 set aside)",
        pattern_key="skip_time_window:9", strength_as_of=NOW, created_at=NOW,
    ))

    async def read_before_the_winners_write(user_id: str, pattern_key: str) -> None:
        return None

    monkeypatch.setattr(repos.memories, "find_by_pattern_key", read_before_the_winners_write)

    changes = await service.observe("alice")

    assert [(c.pattern.pattern_key, c.action) for c in changes] == [("peak_hour:19", "created")]
    rows = await repos.memories.list("alice", statuses=EVERY_STATUS)
    assert sorted(m.pattern_key for m in rows) == ["peak_hour:19", "skip_time_window:9"]
    assert [e.pattern.pattern_key for e in detected.events] == ["peak_hour:19"]


@pytest.mark.asyncio
async def test_observe__other_users_events__ignored(service, repos, detected):
    await _seed(repos, *_skips_at_nine(3, 2, 1, user_id="bob"))

    assert await service.observe("alice") == []

    assert await repos.memories.list("alice", statuses=EVERY_STATUS) == []
    assert detected.events == []
    assert service.drain_pending("alice") == []


# --- learning moments held for the next turn -----------------------------------------


@pytest.mark.asyncio
async def test_drain_pending__skip_teaches_a_pattern__next_turn_still_gets_its_moment(wired):
    """The Skip's own PEARL run learns the pattern, so the next chat turn's look finds
    nothing new; the moment waits for that turn all the same."""
    await _seed(wired.repos, *_skips_at_nine(3, 2))
    wired.store.tasks.append(_pending(_slot(1, 9), "t_yesterday"))

    await wired.tasks.skip("alice", "t_yesterday")
    await wired.observer.settle()

    assert await wired.patterns.observe("alice") == []  # the turn's own look
    (moment,) = wired.patterns.drain_pending("alice")
    assert (moment.action, moment.pattern.pattern_key) == ("created", "skip_time_window:9")
    assert moment.pattern.occurrences == 3
    assert wired.patterns.drain_pending("alice") == []  # told once
    assert wired.patterns.drain_pending("bob") == []


@pytest.mark.asyncio
async def test_drain_pending__more_than_ten__keeps_the_newest_ten(repos, bus, clock):
    detector = _Fixed([_fixed(f"k{i}", seen=NOW - timedelta(hours=1)) for i in range(12)])
    service = PatternService(
        repos.task_events, repos.memories, repos.profiles, bus, detectors=[detector], clock=clock
    )

    assert len(await service.observe("alice")) == 12  # observe's own answer is unchanged

    assert [c.pattern.pattern_key for c in service.drain_pending("alice")] == [
        f"k{i}" for i in range(2, 12)
    ]


@pytest.mark.asyncio
async def test_drain_pending__created_then_strengthened_before_a_turn__one_new_moment(
    repos, bus, clock
):
    detector = _Fixed([_fixed("k", seen=NOW - timedelta(hours=1), occurrences=3)])
    service = PatternService(
        repos.task_events, repos.memories, repos.profiles, bus, detectors=[detector], clock=clock
    )
    await service.observe("alice")
    clock.advance(hours=2)
    detector.patterns = [_fixed("k", seen=NOW + timedelta(hours=1), occurrences=4)]

    (strengthened,) = await service.observe("alice")

    assert strengthened.action == "strengthened"
    (moment,) = service.drain_pending("alice")
    assert (moment.action, moment.pattern.occurrences) == ("created", 4)  # new to the user


class _SlowAnswers(InMemoryMemoryRepository):
    """The write lands at once; the answer takes as long as the test says."""

    def __init__(self, store: InMemoryStore) -> None:
        super().__init__(store)
        self.landed = asyncio.Event()
        self.answer = asyncio.Event()

    async def add(self, user_id: str, m: Memory, *, embedding: list[float] | None = None) -> Memory:
        stored = await super().add(user_id, m, embedding=embedding)
        self.landed.set()
        await self.answer.wait()
        return stored


@pytest.mark.asyncio
async def test_drain_pending__time_cap_cancels_after_the_write__moment_kept(bus, clock):
    store = InMemoryStore()
    repos = RepositoryFactory.in_memory(store)
    memories = _SlowAnswers(store)
    service = PatternService(repos.task_events, memories, repos.profiles, bus, clock=clock)
    await _seed(repos, *_skips_at_nine(3, 2, 1))

    turn = asyncio.create_task(service.observe("alice"))
    await memories.landed.wait()
    turn.cancel()  # the caller's time cap
    with pytest.raises(asyncio.CancelledError):
        await turn
    memories.answer.set()
    held: list = []
    await _until(lambda: held.extend(service.drain_pending("alice")) or held)

    (moment,) = held
    assert (moment.action, moment.pattern.pattern_key) == ("created", "skip_time_window:9")
    assert (await memories.get("alice", moment.memory_id)).pattern_key == "skip_time_window:9"


# --- the observer: in the background, once at a time per user -------------------------


@pytest.mark.asyncio
async def test_pattern_observer__task_skipped__observe_runs(service, repos, bus, detected):
    observer = PatternObserver(service)
    bus.subscribe(TaskSkipped, observer)
    await _seed(repos, *_skips_at_nine(3, 2, 1))

    await bus.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id="t_1115_0900"))
    await observer.settle()

    memory = await repos.memories.find_by_pattern_key("alice", "skip_time_window:9")
    assert memory is not None and memory.observation_count == 3
    assert len(detected.events) == 1


@pytest.mark.asyncio
async def test_pattern_observer__complete__returns_without_waiting_for_observe(
    wired, monkeypatch
):
    observe = _GatedObserve()
    monkeypatch.setattr(wired.patterns, "observe", observe)
    wired.store.tasks.append(_pending(_slot(1, 9), "t1"))

    event = await asyncio.wait_for(wired.tasks.complete("alice", "t1"), timeout=1)

    assert event is not None and event.event_type is TaskEventType.COMPLETED
    await asyncio.wait_for(observe.entered.wait(), timeout=1)  # it did start, behind the reply
    observe.gate.set()
    await wired.observer.settle()
    assert observe.calls == ["alice"]


@pytest.mark.asyncio
async def test_pattern_observer__burst_of_outcomes__at_most_two_observes(wired, monkeypatch):
    observe = _GatedObserve()
    monkeypatch.setattr(wired.patterns, "observe", observe)

    await wired.bus.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id="t0"))
    await asyncio.wait_for(observe.entered.wait(), timeout=1)
    for i in range(1, 6):  # while the first run is still looking
        await wired.bus.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id=f"t{i}"))
    observe.gate.set()
    await wired.observer.settle()

    assert observe.calls == ["alice", "alice"]  # one more run covers all five


@pytest.mark.asyncio
async def test_pattern_observer__sweep_sets_aside_five_slots__one_or_two_observes(
    wired, monkeypatch
):
    calls: list[str] = []

    async def observe(user_id: str) -> list:
        calls.append(user_id)
        await asyncio.sleep(0)
        return []

    monkeypatch.setattr(wired.patterns, "observe", observe)
    wired.store.tasks.extend(_pending(_slot(1, h), f"t{h}") for h in (9, 10, 11, 12, 13))

    missed = await wired.tasks.sweep_passed_slots("alice", NOW)
    await wired.observer.settle()

    assert len(missed) == 5
    assert 1 <= len(calls) <= 2 and set(calls) == {"alice"}


@pytest.mark.asyncio
async def test_pattern_observer__observe_fails__logged_by_type_and_the_next_outcome_runs(
    wired, monkeypatch, caplog
):
    calls: list[str] = []

    async def failing(user_id: str) -> list:
        calls.append(user_id)
        raise RuntimeError("words the user wrote")

    monkeypatch.setattr(wired.patterns, "observe", failing)

    with caplog.at_level(logging.WARNING, logger="app.services.memory.patterns"):
        for task_id in ("t1", "t2"):
            await wired.bus.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id=task_id))
            await wired.observer.settle()

    assert calls == ["alice", "alice"]
    logged = [r.getMessage() for r in caplog.records if r.name == "app.services.memory.patterns"]
    assert len(logged) == 2
    assert all("RuntimeError" in line and "words the user wrote" not in line for line in logged)


@pytest.mark.asyncio
async def test_pattern_observer__shutdown__waits_its_grace_then_cancels_and_takes_no_more(
    wired, monkeypatch
):
    observe = _GatedObserve()  # never opened: a run that would not end on its own
    monkeypatch.setattr(wired.patterns, "observe", observe)
    await wired.bus.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id="t1"))
    await asyncio.wait_for(observe.entered.wait(), timeout=1)

    await asyncio.wait_for(wired.observer.shutdown(grace_s=0.05), timeout=1)

    await asyncio.wait_for(wired.observer.settle(), timeout=1)  # nothing left in flight
    await wired.bus.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id="t2"))
    await asyncio.sleep(0)
    assert observe.calls == ["alice"]


# --- the composition root ---------------------------------------------------------------


def _container() -> AppContainer:
    return AppContainer.build(
        supabase_client=None,
        cfg=_cfg,
        hooks=ActionHooks(),
        embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )


@pytest.mark.asyncio
async def test_container__task_outcomes__each_reaches_pattern_observer(monkeypatch):
    container = _container()
    observed: list[str] = []

    async def record(user_id: str) -> list:
        observed.append(user_id)
        return []

    monkeypatch.setattr(container.patterns, "observe", record)

    for event in (
        TaskCompleted(occurred_at=NOW, user_id="alice", task_id="t1"),
        TaskSkipped(occurred_at=NOW, user_id="bob", task_id="t2"),
        TaskMissed(occurred_at=NOW, user_id="carol", task_id="t3"),
        DeadlineMissed(occurred_at=NOW, user_id="dave", task_id="t4"),  # a date, not an hour
    ):
        await container.events.publish(event)
    await container.pattern_observer.settle()

    assert sorted(observed) == ["alice", "bob", "carol"]


@pytest.mark.asyncio
async def test_container__shutdown__waits_for_pearl_runs_in_flight(monkeypatch):
    container = _container()
    observe = _GatedObserve()
    monkeypatch.setattr(container.patterns, "observe", observe)
    await container.events.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id="t1"))
    await asyncio.wait_for(observe.entered.wait(), timeout=1)

    stopping = asyncio.create_task(container.shutdown())
    await asyncio.sleep(0)
    assert not stopping.done()  # the run in flight gets to finish
    observe.gate.set()
    await asyncio.wait_for(stopping, timeout=1)


# --- current and insights ------------------------------------------------------------


@pytest.mark.asyncio
async def test_current__live_patterns_joined_with_memories__suppressed_excluded(service, repos):
    memory_service = MemoryService(repos.memories, BagOfWordsEmbedder())
    await _seed(
        repos,
        *_skips_at_nine(3, 2, 1),
        *[_outcome("completed", _slot(d, 19)) for d in (3, 2, 1)],
    )
    stored = {c.pattern.pattern_key: c.memory_id for c in await service.observe("alice")}
    await memory_service.dismiss("alice", stored["skip_time_window:9"])
    await memory_service.confirm("alice", stored["peak_hour:19"])
    await _seed(repos, *[  # new evidence nobody has looked at yet
        _outcome("completed", _slot(d, h), title=t, deadline_hint=dl)
        for d, h, t, dl in ((6, 20, "Essay", "2026-11-10"), (4, 21, "Lab", "2026-11-12"),
                            (2, 22, "Quiz", "2026-11-14"))
    ])

    views = await service.current("alice")

    assert [(v.pattern.pattern_key, v.memory_id, v.confirmed) for v in views] == [
        ("peak_hour:19", stored["peak_hour:19"], True),
        ("deadline_crunch", None, False),
    ]


@pytest.mark.asyncio
async def test_insights__stored_patterns__copy_uses_live_numbers(service, repos):
    await _seed(
        repos,
        *_skips_at_nine(5, 4, 3, 2, 1),
        *[_outcome("completed", _slot(d, 19)) for d in (3, 2, 1)],
    )
    stored = {c.pattern.pattern_key: c.memory_id for c in await service.observe("alice")}
    await _seed(repos, *[  # a live pattern with no memory yet: not an insight
        _outcome("skipped", _slot(d, h), task_id="essay_t1", title="Draft the essay intro")
        for d, h in ((5, 14), (3, 15), (1, 16))
    ])

    insights = await service.insights("alice")

    assert insights == [
        {
            # 5 of 5, from the live pattern, not round(0.9 x 5) from the stored memory.
            "insight": "I keep demanding work out of the 9 AM hour — you've set aside 5 of 5 there.",
            "confidence": 1.0,
            "occurrences": 5,
            "memory_id": stored["skip_time_window:9"],
        },
        {
            "insight": "You do your best work around 7 PM.",
            "confidence": 1.0,
            "occurrences": 3,
            "memory_id": stored["peak_hour:19"],
        },
    ]


class _Streak(PatternDetector):
    """A detector the service has never heard of."""

    name = "streak"
    applied_as = "gentle_nudge"

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        return [Pattern(
            detector=self.name, description="User keeps a four-day streak", occurrences=4,
            confidence=0.9, pattern_key="streak", last_observed_at=NOW - timedelta(hours=1),
        )]

    def insight(self, pattern: Pattern) -> str:
        return "Four days running."


@pytest.mark.asyncio
async def test_detector__new_strategy__brings_its_own_insight_and_planning_use(repos, bus, clock):
    service = PatternService(
        repos.task_events, repos.memories, repos.profiles, bus, detectors=[_Streak()], clock=clock
    )

    (change,) = await service.observe("alice")

    assert (await repos.memories.get("alice", change.memory_id)).applied_as == "gentle_nudge"
    assert await service.insights("alice") == [{
        "insight": "Four days running.", "confidence": 0.9, "occurrences": 4,
        "memory_id": change.memory_id,
    }]
    assert SkipTimeWindowDetector.applied_as == "soft_preference"
    assert PeakHourDetector.applied_as is None
