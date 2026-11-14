"""PEARL: Jarvis notices what the user keeps doing, from what happened to their tasks.

The only input is the outcome history (``task_events``): each completed, skipped or
missed slot of the last ``FOCUS_WINDOW_DAYS``. A slot can have more than one outcome
(the sweep marks it missed, then the user completes it after all); it counts once, as
what happened last. Each ``PatternDetector`` is one strategy for reading that history,
with hours on the user's clock. Every pattern it finds carries a stable
``pattern_key``, such as ``skip_time_window:9``.

``PatternService.observe`` remembers patterns as inferred behavioural memories, looked
up by key. A new pattern is created. One seen on more evidence than its memory holds
is strengthened. Anything else is left alone, so looking again teaches nothing twice.
A pattern the user dismissed or forgot is never learned again. Every change is
announced (``PatternDetected``), and ``PatternObserver`` runs PEARL after each outcome.
"""

from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

import app.core.config as config
from app.domain import errors
from app.domain.entities import (
    SUPPRESSING_STATUSES,
    Memory,
    MemorySource,
    MemoryStatus,
    Pattern,
    TaskEvent,
    TaskEventType,
)
from app.domain.events import DomainEvent, EventBus, EventListener, PatternDetected
from app.domain.ports import MemoryRepository, ProfileRepository, TaskEventRepository
from app.services.analytical.missed_deadlines import _parse_deadline
from app.services.planning.local_time import user_zone
from app.services.task_service import utc_now

# What a slot can come to. Skipped and missed (the slot passed) are both "set aside".
_OUTCOMES = frozenset({TaskEventType.COMPLETED, TaskEventType.SKIPPED, TaskEventType.MISSED})
_SET_ASIDE = frozenset({TaskEventType.SKIPPED, TaskEventType.MISSED})
# A remembered pattern can be strengthened from these states; dismissed and forgotten stick,
# also when the user lets go of it while PEARL is looking (a compare-and-set on status).
_STRENGTHENABLE = frozenset({MemoryStatus.ACTIVE, MemoryStatus.FADED})

MIN_OCCURRENCES = 3  # never a pattern from fewer outcomes
SKIP_RATE = 0.7
PEAK_RATE = 0.8
CRUNCH_RATE = 0.7
CRUNCH_WINDOW = timedelta(hours=24)
MAX_EVIDENCE = 5
MAX_PATTERN_CONFIDENCE = 0.9  # an inferred pattern is never remembered as certain
INSIGHT_CONFIDENCE = 0.7


def _aware(moment: datetime) -> datetime:
    """Stored instants are UTC: a naive one is read as such."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _slot_time(event: TaskEvent) -> datetime:
    return _aware(event.scheduled_start or event.occurred_at)


def _done_time(event: TaskEvent) -> datetime:
    return _aware(event.occurred_at)


def _settled_outcomes(events: Iterable[TaskEvent]) -> list[TaskEvent]:
    """The last outcome of each slot (task, scheduled start), oldest first.

    Events that are not outcomes are dropped, and so is every outcome of a slot but
    its latest. Outcomes with no slot are grouped per task.
    """
    latest: dict[tuple[str, datetime | None], TaskEvent] = {}
    for event in sorted(events, key=_done_time):
        if event.event_type in _OUTCOMES:
            start = _aware(event.scheduled_start) if event.scheduled_start is not None else None
            latest[(event.task_id, start)] = event
    return sorted(latest.values(), key=_done_time)


def _by_local_hour(events: list[TaskEvent], zone: ZoneInfo) -> dict[int, list[TaskEvent]]:
    """Outcomes with a slot, by the local hour the slot started at; hours in order."""
    buckets: dict[int, list[TaskEvent]] = defaultdict(list)
    for event in events:
        if event.scheduled_start is not None:
            buckets[_aware(event.scheduled_start).astimezone(zone).hour].append(event)
    return dict(sorted(buckets.items()))


def _title(event: TaskEvent) -> str:
    return str(event.metadata.get("title") or event.task_id)


def _evidence(
    events: list[TaskEvent], zone: ZoneInfo, at: Callable[[TaskEvent], datetime] = _slot_time
) -> list[str]:
    """Up to MAX_EVIDENCE observations, newest first: what happened, when (local), to what."""
    newest = sorted(events, key=at, reverse=True)[:MAX_EVIDENCE]
    return [
        f"{e.event_type.value} {at(e).astimezone(zone):%a %d %b %H:%M} · {_title(e)}"
        for e in newest
    ]


class PatternDetector(ABC):
    """One way of reading the outcome history for a recurring behaviour (Strategy)."""

    name: ClassVar[str]

    @abstractmethod
    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        """The patterns in ``events``, hours read on ``zone``'s clock, each with a pattern_key."""


class SkipTimeWindowDetector(PatternDetector):
    """An hour the user keeps setting aside: 3 or more outcomes, at least 70% skipped or missed."""

    name: ClassVar[str] = "skip_time_window"

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        patterns = []
        for hour, outcomes in _by_local_hour(_settled_outcomes(events), zone).items():
            set_aside = [e for e in outcomes if e.event_type in _SET_ASIDE]
            n, total = len(set_aside), len(outcomes)
            if total < MIN_OCCURRENCES or n / total < SKIP_RATE:
                continue
            patterns.append(Pattern(
                detector=self.name,
                description=f"User avoids tasks during hour {hour} ({n} of {total} set aside)",
                occurrences=total,
                confidence=n / total,
                evidence=_evidence(set_aside, zone),
                pattern_key=f"{self.name}:{hour}",
                hour=hour,
            ))
        return patterns


class PeakHourDetector(PatternDetector):
    """The user's single best hour: 3 or more outcomes, at least 80% done.

    The best is the highest rate done, then the most outcomes, then the earlier hour.
    """

    name: ClassVar[str] = "peak_hour"

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        best: tuple[tuple[float, int], int, list[TaskEvent]] | None = None
        for hour, outcomes in _by_local_hour(_settled_outcomes(events), zone).items():
            if len(outcomes) < MIN_OCCURRENCES:
                continue
            done = [e for e in outcomes if e.event_type is TaskEventType.COMPLETED]
            rank = (len(done) / len(outcomes), len(outcomes))
            if best is None or rank > best[0]:
                best = (rank, hour, done)
        if best is None or best[0][0] < PEAK_RATE:
            return []
        (rate, total), hour, done = best
        return [Pattern(
            detector=self.name,
            description=(
                f"User does their best work around hour {hour} ({len(done)} of {total} done)"
            ),
            occurrences=total,
            confidence=rate,
            evidence=_evidence(done, zone),
            pattern_key=f"{self.name}:{hour}",
            hour=hour,
        )]


class DeadlineCrunchDetector(PatternDetector):
    """Deadlined work finished in its last day: 3 or more completions, at least 70% of them.

    A date-only deadline passes at 23:59:59 on the user's clock.
    """

    name: ClassVar[str] = "deadline_crunch"

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        deadlined = []
        for event in _settled_outcomes(events):
            if event.event_type is TaskEventType.COMPLETED:
                deadline = _parse_deadline(event.metadata.get("deadline_hint"), zone)
                if deadline is not None:
                    deadlined.append((event, deadline))
        last_day = [
            event
            for event, deadline in deadlined
            if deadline - CRUNCH_WINDOW <= _done_time(event) <= deadline
        ]
        k, n = len(last_day), len(deadlined)
        if n < MIN_OCCURRENCES or k / n < CRUNCH_RATE:
            return []
        return [Pattern(
            detector=self.name,
            description=f"User tends to finish deadlined work in the last day ({k} of {n})",
            occurrences=n,
            confidence=k / n,
            evidence=_evidence(last_day, zone, at=_done_time),
            pattern_key=self.name,
        )]


class RepeatedSkipDetector(PatternDetector):
    """One task set aside again and again: skipped 3 or more times.

    Confidence is the share of the task's outcomes that were skips.
    """

    name: ClassVar[str] = "repeated_skip"

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        by_task: dict[str, list[TaskEvent]] = defaultdict(list)
        for event in _settled_outcomes(events):
            by_task[event.task_id].append(event)
        patterns = []
        for task_id, outcomes in by_task.items():
            skipped = [e for e in outcomes if e.event_type is TaskEventType.SKIPPED]
            n = len(skipped)
            if n < MIN_OCCURRENCES:
                continue
            patterns.append(Pattern(
                detector=self.name,
                description=(
                    f"'{_title(skipped[-1])}' has been set aside {n} times;"
                    " smaller steps may help"
                ),
                occurrences=n,
                confidence=n / len(outcomes),
                evidence=_evidence(skipped, zone),
                pattern_key=f"{self.name}:{task_id}",
            ))
        return patterns


class PatternChange(BaseModel):
    """What ``PatternService.observe`` changed for one pattern."""

    pattern: Pattern = Field(description="The pattern as detected now.")
    memory_id: str = Field(description="Id of the memory that holds the pattern.")
    action: Literal["created", "strengthened"] = Field(
        description="created: newly remembered; strengthened: seen on more evidence than before."
    )


class PatternView(BaseModel):
    """A pattern detected now, with the memory that holds it, if any."""

    pattern: Pattern = Field(description="The pattern as detected now.")
    memory_id: str | None = Field(
        description="Id of the memory that holds the pattern; None until it is remembered."
    )
    confirmed: bool = Field(description="Whether the user has confirmed the remembered pattern.")


def _clock_hour(hour: int) -> str:
    """A 24-hour ``hour`` as the user reads it: 0 is 12 AM, 9 is 9 AM, 19 is 7 PM."""
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def _insight(pattern: Pattern) -> str:
    """What Jarvis tells the user about ``pattern``, from its live numbers."""
    if pattern.detector == SkipTimeWindowDetector.name:
        n = round(pattern.confidence * pattern.occurrences)
        return (
            f"I keep demanding work out of the {_clock_hour(pattern.hour)} hour"
            f" — you've set aside {n} of {pattern.occurrences} there."
        )
    if pattern.detector == PeakHourDetector.name:
        return f"You do your best work around {_clock_hour(pattern.hour)}."
    if pattern.detector == DeadlineCrunchDetector.name:
        return "You tend to finish deadlined work on the last day."
    return pattern.description


class PatternService:
    """Finds the user's recurring behaviour in their outcome history and remembers it."""

    def __init__(
        self,
        task_events: TaskEventRepository,
        memories: MemoryRepository,
        profiles: ProfileRepository,
        bus: EventBus,
        *,
        detectors: list[PatternDetector] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._task_events = task_events
        self._memories = memories
        self._profiles = profiles
        self._bus = bus
        self._detectors = (
            detectors
            if detectors is not None
            else [
                SkipTimeWindowDetector(),
                PeakHourDetector(),
                DeadlineCrunchDetector(),
                RepeatedSkipDetector(),
            ]
        )
        self._clock = clock

    async def observe(self, user_id: str) -> list[PatternChange]:
        """Remember what the detectors find now; return what changed, each one announced.

        Looking again with no new evidence changes nothing: a memory is strengthened
        only when the pattern has more occurrences than the memory holds.
        """
        now = self._clock()
        changes = []
        for pattern in await self._detect(user_id, now):
            change = await self._remember(user_id, pattern, now)
            if change is not None:
                changes.append(change)
                await self._bus.publish(
                    PatternDetected(occurred_at=now, user_id=user_id, pattern=pattern)
                )
        return changes

    async def current(self, user_id: str) -> list[PatternView]:
        """The patterns detected now, each with its memory; dismissed and forgotten left out."""
        views = []
        for pattern in await self._detect(user_id, self._clock()):
            memory = await self._memories.find_by_pattern_key(user_id, pattern.pattern_key)
            if memory is not None and memory.status in SUPPRESSING_STATUSES:
                continue
            views.append(PatternView(
                pattern=pattern,
                memory_id=memory.id if memory is not None else None,
                confirmed=memory is not None and memory.last_confirmed_at is not None,
            ))
        return views

    async def insights(self, user_id: str) -> list[dict]:
        """What to tell the user: remembered patterns seen 3+ times at confidence 0.7 or more.

        The words and numbers are the live pattern's, never the memory's capped confidence.
        Each is ``{"insight", "confidence", "occurrences", "memory_id"}``.
        """
        return [
            {
                "insight": _insight(view.pattern),
                "confidence": view.pattern.confidence,
                "occurrences": view.pattern.occurrences,
                "memory_id": view.memory_id,
            }
            for view in await self.current(user_id)
            if view.memory_id is not None
            and view.pattern.confidence >= INSIGHT_CONFIDENCE
            and view.pattern.occurrences >= MIN_OCCURRENCES
        ]

    async def _detect(self, user_id: str, now: datetime) -> list[Pattern]:
        """Every detector over the user's outcomes of the last FOCUS_WINDOW_DAYS, their clock."""
        zone = user_zone(await self._profiles.get_or_create(user_id))
        events = await self._task_events.list_for_user(
            user_id, since=now - timedelta(days=config.FOCUS_WINDOW_DAYS), types=set(_OUTCOMES)
        )
        return [p for detector in self._detectors for p in detector.detect(events, zone)]

    async def _remember(
        self, user_id: str, pattern: Pattern, now: datetime
    ) -> PatternChange | None:
        """Create or strengthen the pattern's memory; None when nothing changed."""
        existing = await self._memories.find_by_pattern_key(user_id, pattern.pattern_key)
        confidence = min(MAX_PATTERN_CONFIDENCE, pattern.confidence)
        if existing is None:
            memory = Memory(
                user_id=user_id,
                memory_type="behavioral_pattern",
                content=pattern.description,
                provenance=MemorySource.INFERRED,
                origin="behavior",
                pattern_key=pattern.pattern_key,
                confidence=confidence,
                observation_count=pattern.occurrences,
                strength_as_of=now,
                created_at=now,
                applied_as=(
                    "soft_preference" if pattern.detector == SkipTimeWindowDetector.name else None
                ),
            )
            try:
                stored = await self._memories.add(user_id, memory)
            except errors.ConflictError:  # a concurrent observe remembered it first
                return None
            return PatternChange(pattern=pattern, memory_id=stored.id, action="created")
        if existing.status in SUPPRESSING_STATUSES:  # the user let it go: never again
            return None
        if existing.status in _STRENGTHENABLE and existing.observation_count < pattern.occurrences:
            try:
                await self._memories.update(
                    user_id,
                    existing.id,
                    only_if_status=_STRENGTHENABLE,
                    content=pattern.description,
                    confidence=confidence,
                    observation_count=pattern.occurrences,
                    status=MemoryStatus.ACTIVE,
                    strength=1.0,
                    strength_as_of=now,
                )
            except errors.StaleVersionError:  # dismissed or forgotten since it was read
                return None
            return PatternChange(pattern=pattern, memory_id=existing.id, action="strengthened")
        return None


class PatternObserver(EventListener):
    """Runs PEARL for the user after each of their task outcomes."""

    def __init__(self, patterns: PatternService) -> None:
        self._patterns = patterns

    async def handle(self, event: DomainEvent) -> None:
        if event.user_id is not None:
            await self._patterns.observe(event.user_id)
