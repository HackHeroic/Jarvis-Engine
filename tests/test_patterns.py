"""PEARL on the outcome history: detectors are strategies over ``task_events``.

An outcome is a completed, skipped or missed slot, bucketed by the local hour it was
scheduled at; a slot that has two outcomes (missed, then completed after all) counts
once, as what happened last. ``PatternService.observe`` remembers what the detectors
find, keyed by pattern so the same habit is recognised. It strengthens a pattern only
on new evidence, and never brings back one the user dismissed or forgot.

InMemory repositories, a fixed clock, and the user's clock in India.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.core.config as _cfg
from app.container import AppContainer
from app.domain.entities import Memory, MemorySource, MemoryStatus, TaskEvent, TaskEventType
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
from app.infrastructure.persistence.factory import RepositoryFactory
from app.orchestrator.hooks import ActionHooks
from app.services.memory.memory_service import MemoryService
from app.services.memory.patterns import (
    DeadlineCrunchDetector,
    PatternObserver,
    PatternService,
    PeakHourDetector,
    RepeatedSkipDetector,
    SkipTimeWindowDetector,
)
from tests.fakes import BagOfWordsEmbedder

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=IST)  # Monday morning, on the user's clock
EVERY_STATUS = frozenset(MemoryStatus)


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


async def _seed(repos, *events: TaskEvent) -> None:
    for event in events:
        await repos.task_events.append(event.user_id, event)


def _skips_at_nine(*days_ago: int, user_id: str = "alice") -> list[TaskEvent]:
    return [_outcome("skipped", _slot(d, 9), user_id=user_id) for d in days_ago]


# --- detectors -----------------------------------------------------------------------


def test_skip_window__three_of_three_at_nine__pattern_hour_9():
    (pattern,) = SkipTimeWindowDetector().detect(_skips_at_nine(3, 2, 1), IST)

    assert pattern.detector == "skip_time_window"
    assert pattern.pattern_key == "skip_time_window:9"
    assert pattern.hour == 9
    assert (pattern.occurrences, pattern.confidence) == (3, 1.0)
    assert pattern.description == "User avoids tasks during hour 9 (3 of 3 set aside)"
    assert pattern.evidence == [  # newest first, on the user's clock
        "skipped Sun 15 Nov 09:00 · Morning task",
        "skipped Sat 14 Nov 09:00 · Morning task",
        "skipped Fri 13 Nov 09:00 · Morning task",
    ]


def test_skip_window__two_of_three__nothing():
    events = [*_skips_at_nine(3, 2), _outcome("completed", _slot(1, 9))]

    assert SkipTimeWindowDetector().detect(events, IST) == []


def test_skip_window__missed_counts_as_set_aside():
    events = [*_skips_at_nine(3, 2), _outcome("missed", _slot(1, 9))]

    (pattern,) = SkipTimeWindowDetector().detect(events, IST)

    assert pattern.description == "User avoids tasks during hour 9 (3 of 3 set aside)"
    assert pattern.evidence[0] == "missed Sun 15 Nov 09:00 · Morning task"


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
    assert [line.rsplit(" · ", 1)[1] for line in pattern.evidence] == ["Quiz", "Lab", "Essay"]


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


@pytest.mark.asyncio
async def test_pattern_observer__task_skipped__observe_runs(service, repos, bus, detected):
    bus.subscribe(TaskSkipped, PatternObserver(service))
    await _seed(repos, *_skips_at_nine(3, 2, 1))

    await bus.publish(TaskSkipped(occurred_at=NOW, user_id="alice", task_id="t_1115_0900"))

    memory = await repos.memories.find_by_pattern_key("alice", "skip_time_window:9")
    assert memory is not None and memory.observation_count == 3
    assert len(detected.events) == 1


@pytest.mark.asyncio
async def test_container__task_outcomes__each_reaches_pattern_observer(monkeypatch):
    container = AppContainer.build(
        supabase_client=None,
        cfg=_cfg,
        hooks=ActionHooks(),
        embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )
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

    assert observed == ["alice", "bob", "carol"]


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
