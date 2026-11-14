"""The first move: what Jarvis says before the user asks.

``MorningBriefTrigger`` has today's plan drafted by the user's wake time: the pending
tasks, plus those whose slot passed since yesterday, each in a new slot, and one thing
Jarvis noticed. Its words are put together from facts, with no model call, and never
blame: a passed slot is "set aside", and it "found a new slot". ``PatternRuleTrigger``
offers an hour the user keeps setting aside as a standing rule; ``AnticipationListener``
runs it as soon as PEARL finds such an hour.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

import app.core.config as config
from app.core.model_router import attribute_llm_calls
from app.domain import errors
from app.domain.anticipation import SuggestionDraft, Trigger, TriggerContext
from app.domain.entities import (
    DraftSchedule,
    FocusProfile,
    SuggestionStatus,
    Task,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from app.domain.events import DomainEvent, EventListener, PatternDetected
from app.domain.ports import (
    DraftPlanner,
    EnergyEstimator,
    SuggestionRepository,
    TaskEventRepository,
    TaskRepository,
)
from app.services.anticipation.actions import (
    AdoptRuleAction,
    ApplyDraftAction,
    ShareInsightAction,
)
from app.services.anticipation.service import ActionFactory, AnticipationService
from app.services.draft_service import _slot_map
from app.services.memory.patterns import (
    INSIGHT_CONFIDENCE,
    MIN_OCCURRENCES,
    OBSERVER_SHUTDOWN_GRACE_S,
    PatternService,
    PatternView,
    SkipTimeWindowDetector,
    _clock_hour,
)
from app.services.planning.local_time import end_of_local_day

logger = logging.getLogger(__name__)

# What became of a task, latest first wins: a passed slot (MISSED) is re-slotted; a
# passed deadline (DEADLINE_MISSED) stays set aside until the user asks for it back.
_OUTCOMES = frozenset({
    TaskEventType.COMPLETED,
    TaskEventType.SKIPPED,
    TaskEventType.MISSED,
    TaskEventType.DEADLINE_MISSED,
})
# The brief only claims an hour is the user's strongest when the learned score says so.
PEAK_CLAIM_MIN_SCORE = 0.7
COULDNT_FIT = "I couldn't fit everything into today — want to look at it together?"
# A user has a handful of suggestions a day; the dedupe read covers well over a year.
_SUGGESTION_READ_LIMIT = 500


@dataclass(frozen=True)
class _Brief:
    """What the morning brief found to say."""

    pending: list[Task]
    reslot: list[Task]
    insight: dict | None
    focus: FocusProfile


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


class MorningBriefTrigger(Trigger):
    """Today's plan, drafted by the time the user wakes, and one thing Jarvis noticed."""

    name: ClassVar[str] = "morning_brief"

    def __init__(
        self,
        tasks: TaskRepository,
        task_events: TaskEventRepository,
        planner: DraftPlanner,
        energy: EnergyEstimator,
        patterns: PatternService,
        actions: ActionFactory,
    ) -> None:
        self._tasks = tasks
        self._task_events = task_events
        self._planner = planner
        self._energy = energy
        self._patterns = patterns
        self._actions = actions

    async def should_fire(self, ctx: TriggerContext) -> bool:
        """From the user's wake time, for MORNING_BRIEF_WINDOW_HOURS, on their clock."""
        zone = ZoneInfo(ctx.profile.timezone)
        wake = datetime.combine(ctx.local_day, ctx.profile.wake_time, tzinfo=zone)
        window = timedelta(hours=config.MORNING_BRIEF_WINDOW_HOURS)
        return wake <= ctx.now.astimezone(zone) < wake + window

    async def gather(self, ctx: TriggerContext) -> _Brief | None:
        """Pending tasks, slots passed since yesterday, the first insight; None if none."""
        zone = ZoneInfo(ctx.profile.timezone)
        pending = await self._tasks.list_by_status(ctx.user_id, frozenset({TaskStatus.PENDING}))
        reslot = await self._passed_slots(ctx, zone)
        insights = await self._patterns.insights(ctx.user_id)
        insight = insights[0] if insights else None
        if not pending and not reslot and insight is None:
            return None
        focus = await self._energy.estimate(ctx.user_id, zone, now=ctx.now)
        return _Brief(pending=pending, reslot=reslot, insight=insight, focus=focus)

    async def build_suggestion(self, ctx: TriggerContext, gathered: _Brief) -> SuggestionDraft:
        zone = ZoneInfo(ctx.profile.timezone)
        expires_at = end_of_local_day(ctx.local_day, zone)
        insight_line = _insight_line(gathered.insight)
        if gathered.pending or gathered.reslot:
            try:
                draft = await self._planner.replan(
                    ctx.user_id, reslot_task_ids=[t.task_id for t in gathered.reslot]
                )
            except errors.ScheduleInfeasibleError:
                draft = None
            if draft is None:
                return SuggestionDraft(
                    kind=self.name,
                    title="About today",
                    body=_lines(COULDNT_FIT, insight_line),
                    action=self._actions.from_payload(
                        {"kind": ShareInsightAction.kind, "insight": COULDNT_FIT}
                    ),
                    expires_at=expires_at,
                )
            reslotted = {t.task_id for t in gathered.reslot} & set(_slot_map(draft.schedule) or {})
            return SuggestionDraft(
                kind=self.name,
                title="Today is ready",
                body=_lines(
                    _planned_line(draft, ctx.local_day, zone, gathered.focus),
                    f"{len(reslotted)} from yesterday found a new slot." if reslotted else None,
                    insight_line,
                ),
                action=self._actions.from_payload(
                    {"kind": ApplyDraftAction.kind, "draft_id": draft.id}
                ),
                expires_at=expires_at,
            )
        return SuggestionDraft(
            kind=self.name,
            title="Something I noticed",
            body=insight_line,
            action=self._actions.from_payload(
                {"kind": ShareInsightAction.kind, "insight": gathered.insight["insight"]}
            ),
            expires_at=expires_at,
        )

    async def _passed_slots(self, ctx: TriggerContext, zone: ZoneInfo) -> list[Task]:
        """Set-aside tasks whose latest outcome is a passed slot, recorded since 00:00
        yesterday on the user's clock. A passed deadline is not re-slotted."""
        missed = await self._tasks.list_by_status(ctx.user_id, frozenset({TaskStatus.MISSED}))
        if not missed:
            return []
        since = datetime.combine(ctx.local_day - timedelta(days=1), time(0), tzinfo=zone)
        events = await self._task_events.list_for_user(
            ctx.user_id, since=since, types=set(_OUTCOMES), task_ids={t.task_id for t in missed}
        )
        latest: dict[str, TaskEvent] = {}
        for event in events:  # oldest first: the later of two wins
            seen = latest.get(event.task_id)
            if seen is None or event.occurred_at >= seen.occurred_at:
                latest[event.task_id] = event
        return [
            t for t in missed
            if t.task_id in latest and latest[t.task_id].event_type is TaskEventType.MISSED
        ]


def _lines(*lines: str | None) -> str:
    return "\n".join(line for line in lines if line)


def _insight_line(insight: dict | None) -> str | None:
    if insight is None:
        return None
    words = insight["insight"].rstrip(".")
    return f"I noticed: {words} (seen {insight['occurrences']} times)."


def _planned_line(
    draft: DraftSchedule, day: date, zone: ZoneInfo, focus: FocusProfile
) -> str | None:
    """How many of the draft's tasks land today, and when the hardest of them starts.

    When none lands today (what is left of it is taken), when the first one does.
    """
    slots = _slot_map(draft.schedule) or {}
    origin = datetime.fromisoformat(draft.horizon_start) if draft.horizon_start else None
    placed: list[tuple[dict, datetime]] = []
    for task in draft.tasks:
        slot = slots.get(task.get("task_id"))
        if origin is None or not isinstance(slot, dict) or slot.get("start_min") is None:
            continue
        placed.append((task, (origin + timedelta(minutes=slot["start_min"])).astimezone(zone)))
    if not placed:
        return None
    today = [(task, start) for task, start in placed if start.date() == day]
    if not today:
        n, first = len(placed), min(start for _, start in placed)
        return (
            f"{n} {_plural(n, 'task is', 'tasks are')} planned;"
            f" the first is on {first:%A} at {first:%H:%M}."
        )
    hardest, at = max(today, key=lambda pair: (_difficulty(pair[0]), -pair[1].timestamp()))
    n = len(today)
    return (
        f"{n} {_plural(n, 'task is', 'tasks are')} planned for today; the hardest,"
        f" '{hardest.get('title', '')}', is at {at:%H:%M}{_peak(focus, at.hour)}."
    )


def _difficulty(task: dict) -> float:
    try:
        return float(task.get("difficulty_weight", 0.5))
    except (TypeError, ValueError):
        return 0.5


def _peak(focus: FocusProfile, hour: int) -> str:
    """The words " — your strongest hour", only when the learned profile peaks at ``hour``."""
    if focus.source != "learned":
        return ""
    scores = focus.scores()
    if scores[hour] < max(scores) or scores[hour] < PEAK_CLAIM_MIN_SCORE:
        return ""
    return " — your strongest hour"


class PatternRuleTrigger(Trigger):
    """Offers an hour the user keeps setting aside as a rule: no demanding work there."""

    name: ClassVar[str] = "pattern_rule"

    def __init__(
        self, patterns: PatternService, suggestions: SuggestionRepository, actions: ActionFactory
    ) -> None:
        self._patterns = patterns
        self._suggestions = suggestions
        self._actions = actions

    async def already_done(self, ctx: TriggerContext, suggestions: SuggestionRepository) -> bool:
        """Never: each pattern is proposed once, which ``gather`` decides."""
        return False

    async def should_fire(self, ctx: TriggerContext) -> bool:
        return True

    async def gather(self, ctx: TriggerContext) -> PatternView | None:
        """The first remembered, unconfirmed skip window seen 3+ times at 0.7+ not yet proposed.

        A window already proposed (pending, accepted, expired) is never proposed again.
        One the user dismissed comes only after every fresh one, so the consent chain's
        repeat-dismissal rule turns it down, saying why, and it never hides a fresh one.
        """
        windows = [v for v in await self._patterns.current(ctx.user_id) if _qualifies(v)]
        if not windows:
            return None
        made = await self._suggestions.list(
            ctx.user_id, status=None, limit=_SUGGESTION_READ_LIMIT
        )
        dismissed = {_key_of(s.action) for s in made if s.status is SuggestionStatus.DISMISSED}
        proposed = {_key_of(s.action) for s in made if s.status is not SuggestionStatus.DISMISSED}
        fresh = [v for v in windows if _rule_key(v) not in proposed]
        fresh.sort(key=lambda v: _rule_key(v) in dismissed)  # stable: fresh ones first
        return fresh[0] if fresh else None

    async def build_suggestion(self, ctx: TriggerContext, gathered: PatternView) -> SuggestionDraft:
        pattern = gathered.pattern
        h12 = _clock_hour(pattern.hour)
        n, total = round(pattern.confidence * pattern.occurrences), pattern.occurrences
        return SuggestionDraft(
            kind=self.name,
            title=f"A pattern around {h12}",
            body=(
                f"You've set aside {n} of the last {total} tasks around {h12}."
                " Want me to stop putting demanding work there?"
            ),
            action=self._actions.from_payload({
                "kind": AdoptRuleAction.kind,
                "pattern_key": pattern.pattern_key,
                "hour": pattern.hour,
                "memory_id": gathered.memory_id,
                "confidence": pattern.confidence,
                "occurrences": pattern.occurrences,
            }),
        )


def _qualifies(view: PatternView) -> bool:
    pattern = view.pattern
    return (
        pattern.detector == SkipTimeWindowDetector.name
        and pattern.hour is not None
        and pattern.pattern_key is not None
        and view.memory_id is not None
        and not view.confirmed
        and pattern.confidence >= INSIGHT_CONFIDENCE
        and pattern.occurrences >= MIN_OCCURRENCES
    )


def _rule_key(view: PatternView) -> str:
    return f"pattern_rule:{view.pattern.pattern_key}"


def _key_of(action: dict) -> str | None:
    key = action.get("dedupe_key")
    return key if isinstance(key, str) else None


class AnticipationListener(EventListener):
    """Runs the pattern-rule trigger for the user when PEARL finds a skip window.

    In the background, like PEARL itself: whoever published the pattern (an outcome,
    a turn, the You page) never waits for it. One run per user at a time; patterns
    found meanwhile ask for one more run after it. The run is a manual one (forced),
    so it re-evaluates even after the day's tick settled the trigger; model calls in
    it are the user's. A failed run is logged by its type.
    """

    def __init__(self, anticipation: AnticipationService) -> None:
        self._anticipation = anticipation
        self._running: dict[str, asyncio.Task] = {}
        self._dirty: set[str] = set()
        self._closed = False

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, PatternDetected) or event.user_id is None or self._closed:
            return
        if not (event.pattern.pattern_key or "").startswith(f"{SkipTimeWindowDetector.name}:"):
            return
        user_id = event.user_id
        if user_id in self._running:
            self._dirty.add(user_id)
            return
        self._running[user_id] = asyncio.create_task(self._run(user_id))

    async def settle(self) -> None:
        """Wait until no run is in flight."""
        while self._running:
            await asyncio.wait(set(self._running.values()))

    async def shutdown(self, grace_s: float = OBSERVER_SHUTDOWN_GRACE_S) -> None:
        """Take no more patterns; give runs in flight ``grace_s``, then cancel the rest."""
        self._closed = True
        runs = set(self._running.values())
        if not runs:
            return
        _, unfinished = await asyncio.wait(runs, timeout=grace_s)
        for run in unfinished:
            run.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)

    async def _run(self, user_id: str) -> None:
        try:
            while True:
                self._dirty.discard(user_id)
                end_attribution = attribute_llm_calls(user_id, None)
                try:
                    await self._anticipation.run_trigger(
                        user_id, PatternRuleTrigger.name, force=True
                    )
                except Exception as exc:  # the next pattern found tries again
                    logger.warning(
                        "Pattern proposal for user %s could not finish (%s)",
                        user_id,
                        type(exc).__name__,
                    )
                finally:
                    end_attribution()
                if user_id not in self._dirty:
                    return
        finally:
            self._running.pop(user_id, None)
