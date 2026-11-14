"""PEARL: Jarvis notices what the user keeps doing, from what happened to their tasks.

The only input is the outcome history (``task_events``): each completed, skipped or
missed slot of the last ``FOCUS_WINDOW_DAYS``, each slot counted once as its latest
outcome (``latest_outcome_per_slot``). Each ``PatternDetector`` is one strategy for
reading that history, with hours on the user's clock. It owns everything particular
to its pattern: the pattern's stable ``pattern_key`` (such as
``skip_time_window:9``), how planning applies it, and the words: descriptions and
insights speak to the user ("You tend to set work aside around 9 AM"), with a human
time and no blame. Adding a detector is a new class here and one line in the composition
root; no other code changes.

The composition root chooses the detectors (``app.container.default_pattern_detectors``).
``PatternService.observe`` remembers patterns as inferred behavioural memories, looked
up by key. A new pattern is created. One with an observation newer than its memory is
strengthened. Anything else is left alone, so looking at the same history again
changes nothing. A pattern the user dismissed or forgot is never learned again.
Every change is announced (``PatternDetected``) and held for the user's next turn
(``drain_pending``), whoever ran PEARL. ``PatternObserver`` runs PEARL in the
background after each outcome, never in the user's way.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

import app.core.config as config
from app.domain import errors
from app.domain.clock import utc_now
from app.domain.entities import (
    SUPPRESSING_STATUSES,
    Memory,
    MemorySource,
    MemoryStatus,
    Pattern,
    TaskEvent,
    TaskEventType,
    memory_moves_to,
)
from app.domain.events import DomainEvent, EventBus, EventListener, PatternDetected
from app.domain.ports import MemoryRepository, ProfileRepository, TaskEventRepository
from app.services.background import PerUserSingleFlight
from app.services.analytical.missed_deadlines import parse_deadline
from app.services.planning.local_time import user_zone
from app.services.task_service import SLOT_OUTCOMES, as_aware, latest_outcome_per_slot

logger = logging.getLogger(__name__)

_SET_ASIDE = frozenset({TaskEventType.SKIPPED, TaskEventType.MISSED})
# A remembered pattern can be strengthened (made ACTIVE) from these states, per the
# domain's MEMORY_TRANSITIONS; dismissed and forgotten stick, also when the user lets go
# of it while PEARL is looking (a compare-and-set on status).
_STRENGTHENABLE = memory_moves_to(MemoryStatus.ACTIVE)
# How an outcome reads in evidence: the user's words, never "skipped" or "missed".
_EVIDENCE_LABELS = {
    TaskEventType.COMPLETED: "done",
    TaskEventType.SKIPPED: "set aside",
    TaskEventType.MISSED: "set aside",
}

MIN_OCCURRENCES = 3  # never a pattern from fewer outcomes
SKIP_RATE = 0.7
PEAK_RATE = 0.8
CRUNCH_RATE = 0.7
CRUNCH_WINDOW = timedelta(hours=24)
MAX_EVIDENCE = 5
MAX_PATTERN_CONFIDENCE = 0.9  # an inferred pattern is never remembered as certain
INSIGHT_CONFIDENCE = 0.7
PENDING_PER_USER = 10  # learning moments held for a user's next turn, newest kept
OBSERVER_SHUTDOWN_GRACE_S = 2.0


def _slot_time(event: TaskEvent) -> datetime:
    return as_aware(event.scheduled_start or event.occurred_at)


def _done_time(event: TaskEvent) -> datetime:
    return as_aware(event.occurred_at)


def _newest(events: list[TaskEvent]) -> datetime:
    """When the newest of ``events`` was recorded."""
    return max(_done_time(e) for e in events)


def _by_local_hour(events: list[TaskEvent], zone: ZoneInfo) -> dict[int, list[TaskEvent]]:
    """Outcomes with a slot, by the local hour the slot started at; hours in order."""
    buckets: dict[int, list[TaskEvent]] = defaultdict(list)
    for event in events:
        if event.scheduled_start is not None:
            buckets[as_aware(event.scheduled_start).astimezone(zone).hour].append(event)
    return dict(sorted(buckets.items()))


def _title(event: TaskEvent) -> str:
    return str(event.metadata.get("title") or event.task_id)


def _evidence(
    events: list[TaskEvent], zone: ZoneInfo, at: Callable[[TaskEvent], datetime] = _slot_time
) -> list[str]:
    """Up to MAX_EVIDENCE observations, newest first: what happened, when (local), to what."""
    newest = sorted(events, key=at, reverse=True)[:MAX_EVIDENCE]
    return [
        f"{_EVIDENCE_LABELS[e.event_type]} {at(e).astimezone(zone):%a %d %b %H:%M} · {_title(e)}"
        for e in newest
    ]


def clock_hour(hour: int) -> str:
    """A 24-hour ``hour`` as the user reads it: 0 is 12 AM, 9 is 9 AM, 19 is 7 PM."""
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


_clock_hour = clock_hour  # this module's own name for it


def _lowered(sentence: str) -> str:
    """A sentence to follow "I noticed": first letter lowered, final full stop dropped."""
    words = sentence.rstrip(".")
    return words[:1].lower() + words[1:]


def _part_of_day(hour: int) -> str:
    """Morning (5-11), afternoon (12-16), evening (17-20) or night."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _key_hour(pattern_key: str) -> int | None:
    """The hour in a ``{name}:{h}`` pattern key; None when there is none."""
    _, _, hour = pattern_key.partition(":")
    return int(hour) if hour.isdigit() and int(hour) < 24 else None


class PatternDetector(ABC):
    """One way of reading the outcome history for a recurring behaviour (Strategy).

    A detector owns its pattern: the key, the words, how planning applies it
    (``applied_as``) and what Jarvis tells the user about it (``insight``).
    """

    name: ClassVar[str]
    # How planning applies a remembered pattern of this kind; None when it does not.
    applied_as: ClassVar[str | None] = None
    # Whether a pattern of this kind (one with an hour) may be offered as a standing
    # rule keeping that hour clear. The pattern-rule trigger asks; it names no detector.
    proposes_rule: ClassVar[bool] = False

    @abstractmethod
    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        """The patterns in ``events``, hours read on ``zone``'s clock.

        Each carries a ``pattern_key`` and ``last_observed_at`` (when the newest
        observation behind it was recorded). ``events`` may hold more than one
        outcome per slot; read them through ``latest_outcome_per_slot``.
        """

    def insight(self, pattern: Pattern) -> str:
        """What Jarvis tells the user about ``pattern``, from its live numbers."""
        return pattern.description

    def noticed(self, pattern: Pattern) -> str:
        """The morning brief's line "I noticed ...": the pattern's own second-person words,
        with the evidence once."""
        n, total = round(pattern.confidence * pattern.occurrences), pattern.occurrences
        return f"I noticed {_lowered(pattern.description)} ({n} of the last {total} times)."

    def text_for_key(self, pattern_key: str) -> str | None:
        """The description of a pattern with this key, when the key holds everything the
        words say; None when they say more (so a stored row keeps its own words)."""
        return None


class SkipTimeWindowDetector(PatternDetector):
    """An hour the user keeps setting aside: 3 or more outcomes, at least 70% skipped or missed."""

    name: ClassVar[str] = "skip_time_window"
    applied_as: ClassVar[str | None] = "soft_preference"
    proposes_rule: ClassVar[bool] = True

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        patterns = []
        for hour, outcomes in _by_local_hour(latest_outcome_per_slot(events), zone).items():
            set_aside = [e for e in outcomes if e.event_type in _SET_ASIDE]
            n, total = len(set_aside), len(outcomes)
            if total < MIN_OCCURRENCES or n / total < SKIP_RATE:
                continue
            patterns.append(Pattern(
                detector=self.name,
                description=self._describe(hour),
                occurrences=total,
                confidence=n / total,
                evidence=_evidence(set_aside, zone),
                pattern_key=f"{self.name}:{hour}",
                hour=hour,
                last_observed_at=_newest(set_aside),
            ))
        return patterns

    @staticmethod
    def _describe(hour: int) -> str:
        return f"You tend to set work aside around {_clock_hour(hour)}"

    def text_for_key(self, pattern_key: str) -> str | None:
        hour = _key_hour(pattern_key)
        return self._describe(hour) if hour is not None else None

    def insight(self, pattern: Pattern) -> str:
        n = round(pattern.confidence * pattern.occurrences)
        return (
            f"I keep demanding work out of the {_clock_hour(pattern.hour)} hour"
            f" — you've set aside {n} of {pattern.occurrences} there."
        )


class PeakHourDetector(PatternDetector):
    """The user's single best hour: 3 or more outcomes, at least 80% done.

    The best is the highest rate done, then the most outcomes, then the earlier hour.
    """

    name: ClassVar[str] = "peak_hour"

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        best: tuple[tuple[float, int], int, list[TaskEvent]] | None = None
        for hour, outcomes in _by_local_hour(latest_outcome_per_slot(events), zone).items():
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
            description=self._describe(hour),
            occurrences=total,
            confidence=rate,
            evidence=_evidence(done, zone),
            pattern_key=f"{self.name}:{hour}",
            hour=hour,
            last_observed_at=_newest(done),
        )]

    @staticmethod
    def _describe(hour: int) -> str:
        return f"Your {_part_of_day(hour)}s around {_clock_hour(hour)} go well"

    def text_for_key(self, pattern_key: str) -> str | None:
        hour = _key_hour(pattern_key)
        return self._describe(hour) if hour is not None else None

    def insight(self, pattern: Pattern) -> str:
        return f"You do your best work around {_clock_hour(pattern.hour)}."


_FINISHES_CLOSE_TO_DEADLINES = "You tend to finish close to deadlines"


class DeadlineCrunchDetector(PatternDetector):
    """Deadlined work finished in its last day: 3 or more completions, at least 70% of them.

    A date-only deadline passes at 23:59:59 on the user's clock.
    """

    name: ClassVar[str] = "deadline_crunch"

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        deadlined = []
        for event in latest_outcome_per_slot(events):
            if event.event_type is TaskEventType.COMPLETED:
                deadline = parse_deadline(event.metadata.get("deadline_hint"), zone)
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
            description=_FINISHES_CLOSE_TO_DEADLINES,
            occurrences=n,
            confidence=k / n,
            evidence=_evidence(last_day, zone, at=_done_time),
            pattern_key=self.name,
            last_observed_at=_newest(last_day),
        )]

    def text_for_key(self, pattern_key: str) -> str | None:
        return _FINISHES_CLOSE_TO_DEADLINES if pattern_key == self.name else None

    def insight(self, pattern: Pattern) -> str:
        return "You tend to finish deadlined work on the last day."


class RepeatedSkipDetector(PatternDetector):
    """One task set aside again and again: skipped 3 or more times.

    Confidence is the share of the task's outcomes that were skips. Jarvis tells the
    user the description itself. The title is only in the words, so a stored row keeps
    its own (``text_for_key`` is None).
    """

    name: ClassVar[str] = "repeated_skip"

    def noticed(self, pattern: Pattern) -> str:
        """Its words already count the times: no second count."""
        return f"I noticed {_lowered(pattern.description)}."

    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]:
        by_task: dict[str, list[TaskEvent]] = defaultdict(list)
        for event in latest_outcome_per_slot(events):
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
                    f"You've set '{_title(skipped[-1])}' aside {n} times;"
                    " smaller steps may help"
                ),
                occurrences=n,
                confidence=n / len(outcomes),
                evidence=_evidence(skipped, zone),
                pattern_key=f"{self.name}:{task_id}",
                last_observed_at=_newest(skipped),
            ))
        return patterns


class PatternChange(BaseModel):
    """What ``PatternService.observe`` changed for one pattern."""

    pattern: Pattern = Field(description="The pattern as detected now.")
    memory_id: str = Field(description="Id of the memory that holds the pattern.")
    action: Literal["created", "strengthened"] = Field(
        description="created: newly remembered; strengthened: seen again since last remembered."
    )


class PatternView(BaseModel):
    """A pattern detected now, with the memory that holds it, if any."""

    pattern: Pattern = Field(description="The pattern as detected now.")
    memory_id: str | None = Field(
        description="Id of the memory that holds the pattern; None until it is remembered."
    )
    confirmed: bool = Field(description="Whether the user has confirmed the remembered pattern.")


class PatternLook(BaseModel):
    """One look at the outcome history: what it changed, and every pattern it sees."""

    changes: list[PatternChange] = Field(description="What the look remembered, in order.")
    views: list[PatternView] = Field(
        description="The patterns seen now with their memories; dismissed and forgotten left out."
    )


async def _to_the_end(
    write: Coroutine[Any, Any, "PatternChange | None"],
) -> "PatternChange | None":
    """Await ``write`` (a memory write and its pending moment), which finishes even if the
    caller is cancelled meanwhile (a time cap): the change is then held, never lost."""
    task = asyncio.ensure_future(write)
    # Retrieve the outcome, so a write left running by a cancelled caller that then
    # fails is not reported as an unretrieved task exception.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.shield(task)


def _id_of(memory: Memory | None) -> str | None:
    return memory.id if memory is not None else None


class PatternService:
    """Finds the user's recurring behaviour in their outcome history and remembers it."""

    def __init__(
        self,
        task_events: TaskEventRepository,
        memories: MemoryRepository,
        profiles: ProfileRepository,
        bus: EventBus,
        *,
        detectors: list[PatternDetector],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """``detectors``: the strategies PEARL reads the history with, chosen by the
        composition root (``app.container.default_pattern_detectors``)."""
        self._task_events = task_events
        self._memories = memories
        self._profiles = profiles
        self._bus = bus
        self._detectors = list(detectors)
        self._by_name = {detector.name: detector for detector in self._detectors}
        self._clock = clock
        self._pending: dict[str, deque[PatternChange]] = {}
        # The look running for each user, for a caller that arrives meanwhile to join.
        self._looking: dict[str, asyncio.Future[PatternLook]] = {}

    async def observe(self, user_id: str) -> list[PatternChange]:
        """Remember what the detectors find now; return what changed, each one announced.

        Looking at the same history again changes nothing: a memory is strengthened
        only by an observation recorded after it was last renewed. Every change is
        also held for the user's next turn (``drain_pending``).
        """
        return (await self.look(user_id)).changes

    async def look(
        self,
        user_id: str,
        *,
        events: list[TaskEvent] | None = None,
        zone: ZoneInfo | None = None,
    ) -> PatternLook:
        """``observe``, also returning every pattern seen now with its memory (``current``).

        One detection and one read of the memories behind the patterns, however many
        there are. ``events`` (the user's outcome events of the last FOCUS_WINDOW_DAYS)
        and ``zone`` may be given by a caller that has already read them; otherwise
        they are read here.

        One look per user at a time: a caller arriving while one is running (two page
        loads at once, a turn while the background observer looks) waits for it and gets
        its answer, rather than racing it to the same writes. A caller that stops
        waiting (a time cap) never cancels the look for the others: it runs to its end,
        and what it learns is held for the user's next turn.
        """
        running = self._looking.get(user_id)
        if running is None:
            running = asyncio.ensure_future(self._look_now(user_id, events=events, zone=zone))
            self._looking[user_id] = running
            running.add_done_callback(lambda done: self._looked(user_id, done))
        return await asyncio.shield(running)

    def _looked(self, user_id: str, done: asyncio.Future) -> None:
        """A look ended: the next caller starts a new one. Its outcome is retrieved, so
        a look nobody waits for any more that then fails is not reported as unretrieved."""
        if self._looking.get(user_id) is done:
            del self._looking[user_id]
        if not done.cancelled():
            done.exception()

    async def _look_now(
        self,
        user_id: str,
        *,
        events: list[TaskEvent] | None = None,
        zone: ZoneInfo | None = None,
    ) -> PatternLook:
        now = self._clock()
        patterns = await self._detect(user_id, now, events=events, zone=zone)
        held = await self._held(user_id, patterns)
        changes: list[PatternChange] = []
        views: list[PatternView] = []
        for pattern in patterns:
            existing = held.get(pattern.pattern_key)
            change = await self._remember(user_id, pattern, now, existing)
            if change is not None:
                changes.append(change)
                await self._bus.publish(
                    PatternDetected(occurred_at=now, user_id=user_id, pattern=pattern)
                )
            if existing is not None and existing.status in SUPPRESSING_STATUSES:
                continue
            views.append(PatternView(
                pattern=pattern,
                memory_id=change.memory_id if change is not None else _id_of(existing),
                confirmed=existing is not None and existing.last_confirmed_at is not None,
            ))
        return PatternLook(changes=changes, views=views)

    def describe(self, memory: Memory) -> str:
        """How a remembered pattern reads now.

        A row stored in older words (before patterns spoke to the user) reads in
        today's, re-rendered from its pattern key by its detector, when the key holds
        everything the words say. Anything else reads as stored: another kind of
        memory, a repeated skip (its title is only in the words), or a forgotten one.
        """
        keyed = memory.memory_type == "behavioral_pattern" and memory.pattern_key
        if not keyed or not memory.content:  # not a pattern, or forgotten: no words to give
            return memory.content
        detector = self._by_name.get(memory.pattern_key.partition(":")[0])
        text = detector.text_for_key(memory.pattern_key) if detector is not None else None
        return text or memory.content

    def drain_pending(self, user_id: str) -> list[PatternChange]:
        """The learning moments held for the user since the last drain, oldest first; cleared.

        Whoever ran PEARL (a Complete, a Skip, the slot sweep, a turn) held them here,
        so the next turn can tell the user. One moment per pattern, with its latest
        numbers; at most PENDING_PER_USER, the newest.
        """
        return list(self._pending.pop(user_id, ()))

    async def current(
        self,
        user_id: str,
        *,
        events: list[TaskEvent] | None = None,
        zone: ZoneInfo | None = None,
    ) -> list[PatternView]:
        """The patterns detected now, each with its memory; dismissed and forgotten left out.

        One read of the memories behind them, however many there are. ``events`` and
        ``zone`` as for ``look``.
        """
        patterns = await self._detect(user_id, self._clock(), events=events, zone=zone)
        held = await self._held(user_id, patterns)
        views = []
        for pattern in patterns:
            memory = held.get(pattern.pattern_key)
            if memory is not None and memory.status in SUPPRESSING_STATUSES:
                continue
            views.append(PatternView(
                pattern=pattern,
                memory_id=_id_of(memory),
                confirmed=memory is not None and memory.last_confirmed_at is not None,
            ))
        return views

    async def insights(self, user_id: str) -> list[dict]:
        """What to tell the user about the patterns detected now (``insights_of``)."""
        return self.insights_of(await self.current(user_id))

    def insights_of(self, views: list[PatternView]) -> list[dict]:
        """What to tell the user: remembered patterns seen 3+ times at confidence 0.7 or more.

        The words are the detector's, from the live pattern's numbers, never the
        memory's capped confidence. Each is ``{"insight", "confidence", "occurrences",
        "memory_id"}``.
        """
        return [
            {
                "insight": self.insight(view.pattern),
                "confidence": view.pattern.confidence,
                "occurrences": view.pattern.occurrences,
                "memory_id": view.memory_id,
            }
            for view in views
            if self.worth_telling(view)
        ]

    def worth_telling(self, view: PatternView) -> bool:
        """A remembered pattern seen MIN_OCCURRENCES+ times at INSIGHT_CONFIDENCE or more."""
        return (
            view.memory_id is not None
            and view.pattern.confidence >= INSIGHT_CONFIDENCE
            and view.pattern.occurrences >= MIN_OCCURRENCES
        )

    def insight(self, pattern: Pattern) -> str:
        """What Jarvis tells the user about ``pattern``: its detector's words."""
        return self._by_name[pattern.detector].insight(pattern)

    def noticed(self, pattern: Pattern) -> str:
        """The morning brief's "I noticed ..." line about ``pattern``: its detector's words."""
        return self._by_name[pattern.detector].noticed(pattern)

    def proposes_rule(self, pattern: Pattern) -> bool:
        """Whether ``pattern`` may be offered as a standing rule: its detector says so."""
        detector = self._by_name.get(pattern.detector)
        return detector is not None and detector.proposes_rule and pattern.hour is not None

    async def _detect(
        self,
        user_id: str,
        now: datetime,
        *,
        events: list[TaskEvent] | None = None,
        zone: ZoneInfo | None = None,
    ) -> list[Pattern]:
        """Every detector over the user's outcomes of the last FOCUS_WINDOW_DAYS, their clock.

        ``events`` and ``zone`` are read only when not given.
        """
        if zone is None:
            zone = user_zone(await self._profiles.get_or_create(user_id))
        if events is None:
            events = await self._task_events.list_for_user(
                user_id,
                since=now - timedelta(days=config.FOCUS_WINDOW_DAYS),
                types=set(SLOT_OUTCOMES),
            )
        return [p for detector in self._detectors for p in detector.detect(events, zone)]

    async def _held(self, user_id: str, patterns: list[Pattern]) -> dict[str, Memory]:
        """The memories holding ``patterns``, by pattern key: one read, none for no patterns."""
        keys = [p.pattern_key for p in patterns if p.pattern_key]
        if not keys:
            return {}
        found = await self._memories.find_by_pattern_keys(user_id, keys)
        return {m.pattern_key: m for m in found if m.pattern_key}

    async def _remember(
        self, user_id: str, pattern: Pattern, now: datetime, existing: Memory | None
    ) -> PatternChange | None:
        """Create or strengthen the pattern's memory (``existing``, read by the caller);
        None when nothing changed."""
        if existing is None:
            return await _to_the_end(self._create(user_id, pattern, now))
        if existing.status not in _STRENGTHENABLE:  # dismissed or forgotten: never again
            return None
        seen = pattern.last_observed_at
        if seen is None or seen <= existing.strength_as_of:  # nothing new behind it
            return None
        return await _to_the_end(self._strengthen(user_id, existing, pattern, now))

    async def _create(
        self, user_id: str, pattern: Pattern, now: datetime
    ) -> PatternChange | None:
        memory = Memory(
            user_id=user_id,
            memory_type="behavioral_pattern",
            content=pattern.description,
            provenance=MemorySource.INFERRED,
            origin="behavior",
            pattern_key=pattern.pattern_key,
            confidence=min(MAX_PATTERN_CONFIDENCE, pattern.confidence),
            observation_count=pattern.occurrences,
            strength_as_of=now,
            created_at=now,
            applied_as=self._by_name[pattern.detector].applied_as,
        )
        try:
            stored = await self._memories.add(user_id, memory)
        except errors.ConflictError:  # a concurrent observe remembered it first
            return None
        change = PatternChange(pattern=pattern, memory_id=stored.id, action="created")
        self._hold(user_id, change)
        return change

    async def _strengthen(
        self, user_id: str, existing: Memory, pattern: Pattern, now: datetime
    ) -> PatternChange | None:
        try:
            await self._memories.update(
                user_id,
                existing.id,
                only_if_status=_STRENGTHENABLE,
                content=pattern.description,
                confidence=min(MAX_PATTERN_CONFIDENCE, pattern.confidence),
                observation_count=pattern.occurrences,
                status=MemoryStatus.ACTIVE,
                strength=1.0,
                strength_as_of=now,
            )
        except errors.StaleVersionError:  # dismissed or forgotten since it was read
            return None
        change = PatternChange(pattern=pattern, memory_id=existing.id, action="strengthened")
        self._hold(user_id, change)
        return change

    def _hold(self, user_id: str, change: PatternChange) -> None:
        """Keep ``change`` for the user's next turn: one moment per pattern, the newest ten.

        A pattern the user has not been told about yet is still news ("created") when
        it is strengthened again before they are.
        """
        held = self._pending.setdefault(user_id, deque(maxlen=PENDING_PER_USER))
        for earlier in [c for c in held if c.pattern.pattern_key == change.pattern.pattern_key]:
            held.remove(earlier)
            if earlier.action == "created":
                change = change.model_copy(update={"action": "created"})
        held.append(change)


class PatternObserver(EventListener):
    """Runs PEARL for the user after each of their task outcomes, in the background.

    The Complete, Skip or sweep that published the outcome never waits for it. At
    most one run per user is in flight; outcomes that arrive meanwhile ask for one
    more run after it, however many there were (``PerUserSingleFlight``, shared with
    the anticipation listener). A failed run is logged by its type.
    """

    def __init__(self, patterns: PatternService) -> None:
        self._patterns = patterns
        self._flight = PerUserSingleFlight(self._observe, label="PEARL run")

    async def handle(self, event: DomainEvent) -> None:
        if event.user_id is not None:
            self._flight.request(event.user_id)

    async def settle(self) -> None:
        """Wait until no run is in flight."""
        await self._flight.settle()

    async def shutdown(self, grace_s: float = OBSERVER_SHUTDOWN_GRACE_S) -> None:
        """Take no more outcomes; give the runs in flight ``grace_s`` to finish, then
        cancel the rest and wait for them to end."""
        await self._flight.shutdown(grace_s)

    async def _observe(self, user_id: str) -> None:
        await self._patterns.observe(user_id)
