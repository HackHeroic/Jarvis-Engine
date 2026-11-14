"""The first move: what Jarvis says before the user asks.

``MorningBriefTrigger`` has today's plan drafted by the user's wake time: the pending
tasks, plus those whose slot passed since yesterday, each in a new slot, and one thing
Jarvis noticed. Its words are put together from facts, with no model call, and never
blame: a passed slot is "set aside", and it "found a new slot". ``PatternRuleTrigger``
offers an hour the user keeps setting aside as a standing rule; ``AnticipationListener``
runs it as soon as PEARL finds such an hour.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

import app.core.config as config
from app.core.model_router import attribute_llm_calls
from app.domain import errors
from app.domain.anticipation import SuggestionDraft, Trigger, TriggerContext, clock_hours
from app.domain.entities import (
    DraftSchedule,
    FocusProfile,
    SuggestionStatus,
    Task,
    TaskEventType,
    TaskStatus,
)
from app.domain.events import DomainEvent, EventListener, PatternDetected
from app.domain.ports import (
    ConstraintRepository,
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
from app.services.anticipation.service import (
    ActionFactory,
    AnticipationService,
    stated_blocks,
)
from app.services.background import PerUserSingleFlight
from app.services.draft_service import slot_map
from app.services.memory.patterns import (
    OBSERVER_SHUTDOWN_GRACE_S,
    PatternService,
    PatternView,
    clock_hour,
)
from app.services.planning.local_time import end_of_local_day
from app.services.task_service import latest_outcome_per_slot

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
    insight: PatternView | None
    focus: FocusProfile


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


class MorningBriefTrigger(Trigger):
    """Today's plan, drafted by the time the user wakes, and one thing Jarvis noticed."""

    name: ClassVar[str] = "morning_brief"
    description: ClassVar[str] = (
        "Today's plan, drafted by your wake time, with yesterday's set-aside tasks in new"
        " slots and one thing Jarvis noticed."
    )

    def __init__(
        self,
        tasks: TaskRepository,
        task_events: TaskEventRepository,
        planner: DraftPlanner,
        energy: EnergyEstimator,
        patterns: PatternService,
        actions: ActionFactory,
        *,
        suggestions: SuggestionRepository,
    ) -> None:
        self._tasks = tasks
        self._task_events = task_events
        self._planner = planner
        self._energy = energy
        self._patterns = patterns
        self._actions = actions
        self._suggestions = suggestions

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
        insight = await self._insight(ctx)
        if not pending and not reslot and insight is None:
            return None
        focus = await self._energy.estimate(ctx.user_id, zone, now=ctx.now)
        return _Brief(pending=pending, reslot=reslot, insight=insight, focus=focus)

    async def build_suggestion(self, ctx: TriggerContext, gathered: _Brief) -> SuggestionDraft:
        zone = ZoneInfo(ctx.profile.timezone)
        expires_at = end_of_local_day(ctx.local_day, zone)
        insight_line = (
            self._patterns.noticed(gathered.insight.pattern) if gathered.insight else None
        )
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
                    action=self._actions.from_payload({
                        "kind": ShareInsightAction.kind,
                        "insight": COULDNT_FIT,
                        "key": f"brief:{ctx.local_day.isoformat()}",  # today's note only
                    }),
                    expires_at=expires_at,
                )
            return SuggestionDraft(
                kind=self.name,
                title="Today is ready",
                body=_lines(
                    _planned_line(draft, ctx.local_day, zone, gathered.focus),
                    *_reslot_lines(draft, gathered.reslot, ctx.local_day, zone),
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
            action=self._actions.from_payload({
                "kind": ShareInsightAction.kind,
                "insight": insight_line,
                "key": gathered.insight.pattern.pattern_key,  # dismissed: never this pattern again
            }),
            expires_at=expires_at,
        )

    async def _insight(self, ctx: TriggerContext) -> PatternView | None:
        """The first pattern worth telling the user has not dismissed in any form.

        A pattern whose memory was dismissed or forgotten is not current at all; one
        whose "Something I noticed" note was dismissed is not told again either, not
        even as a line inside a brief. The dismissals are read only when there is a
        pattern to tell.
        """
        views = [
            v for v in await self._patterns.current(ctx.user_id) if self._patterns.worth_telling(v)
        ]
        if not views:
            return None
        dismissed = set(await self._suggestions.dedupe_keys(
            ctx.user_id, status=SuggestionStatus.DISMISSED, limit=_SUGGESTION_READ_LIMIT
        ))
        untold = (
            v for v in views if ShareInsightAction.key_for(v.pattern.pattern_key) not in dismissed
        )
        return next(untold, None)

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
        # Each slot's latest outcome, oldest first: the last one per task is its latest.
        latest = {e.task_id: e for e in latest_outcome_per_slot(events, outcomes=_OUTCOMES)}
        return [
            t for t in missed
            if t.task_id in latest and latest[t.task_id].event_type is TaskEventType.MISSED
        ]


def _lines(*lines: str | None) -> str:
    return "\n".join(line for line in lines if line)


def _reslot_lines(
    draft: DraftSchedule, reslot: list[Task], day: date, zone: ZoneInfo
) -> list[str]:
    """How many set-aside tasks the draft found a new slot for: those whose slot passed
    on an earlier day ("from yesterday"), and those whose slot passed today."""
    placed = set(slot_map(draft.schedule) or {})
    earlier_days = today = 0
    for task in reslot:
        if task.task_id not in placed:
            continue
        passed_on = task.scheduled_start.astimezone(zone).date() if task.scheduled_start else None
        if passed_on == day:
            today += 1
        else:
            earlier_days += 1
    lines = []
    if earlier_days:
        lines.append(f"{earlier_days} from yesterday found a new slot.")
    if today:
        lines.append(f"{today} from earlier today found a new slot.")
    return lines


def _planned_line(
    draft: DraftSchedule, day: date, zone: ZoneInfo, focus: FocusProfile
) -> str | None:
    """How many of the draft's tasks land today, and when the hardest of them starts.

    When none lands today (what is left of it is taken), when the first one does.
    """
    slots = slot_map(draft.schedule) or {}
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
    """Offers an hour the user keeps setting aside as a rule: no demanding work there.

    Which patterns may become rules is their detector's to say (``proposes_rule``):
    a new detector needs no change here.
    """

    name: ClassVar[str] = "pattern_rule"
    description: ClassVar[str] = (
        "Offers an hour you keep setting aside as a standing rule that keeps it clear."
    )

    def __init__(
        self,
        patterns: PatternService,
        suggestions: SuggestionRepository,
        actions: ActionFactory,
        constraints: ConstraintRepository,
    ) -> None:
        self._patterns = patterns
        self._suggestions = suggestions
        self._actions = actions
        self._constraints = constraints

    async def already_done(self, ctx: TriggerContext, suggestions: SuggestionRepository) -> bool:
        """Never: each pattern is proposed once, which ``gather`` decides."""
        return False

    async def should_fire(self, ctx: TriggerContext) -> bool:
        return True

    async def gather(self, ctx: TriggerContext) -> PatternView | None:
        """The first remembered, unconfirmed skip window seen 3+ times at 0.7+ not yet proposed.

        A window already proposed (pending, accepted, expired) is never proposed again.
        One the user dismissed, or one a rule they stated already covers, comes only
        after every fresh one: the consent chain turns it down, saying why, and it never
        hides a fresh one.
        """
        windows = [v for v in await self._patterns.current(ctx.user_id) if self._qualifies(v)]
        if not windows:
            return None
        made = await self._suggestions.list(
            ctx.user_id, status=None, limit=_SUGGESTION_READ_LIMIT
        )
        dismissed = {_key_of(s.action) for s in made if s.status is SuggestionStatus.DISMISSED}
        proposed = {_key_of(s.action) for s in made if s.status is not SuggestionStatus.DISMISSED}
        fresh = [v for v in windows if _rule_key(v) not in proposed]
        if not fresh:
            return None
        said = stated_blocks(await self._constraints.list_active(ctx.user_id))

        def covered(view: PatternView) -> bool:
            return any(view.pattern.hour in clock_hours(start, end) for start, end, _ in said)

        # Stable: fresh first, then dismissed, then already covered by what the user said.
        fresh.sort(key=lambda v: (covered(v), _rule_key(v) in dismissed))
        return fresh[0]

    def _qualifies(self, view: PatternView) -> bool:
        """A remembered, unconfirmed pattern worth telling whose detector proposes rules."""
        return (
            self._patterns.proposes_rule(view.pattern)
            and self._patterns.worth_telling(view)
            and not view.confirmed
        )

    async def build_suggestion(self, ctx: TriggerContext, gathered: PatternView) -> SuggestionDraft:
        pattern = gathered.pattern
        h12 = clock_hour(pattern.hour)
        n, total = round(pattern.confidence * pattern.occurrences), pattern.occurrences
        return SuggestionDraft(
            kind=self.name,
            title=f"A pattern around {h12}",
            body=(
                f"You've set aside {n} of the last {total} tasks around {h12}."
                " Want me to keep that hour clear?"
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


def _rule_key(view: PatternView) -> str:
    return f"pattern_rule:{view.pattern.pattern_key}"


def _key_of(action: dict) -> str | None:
    key = action.get("dedupe_key")
    return key if isinstance(key, str) else None


class AnticipationListener(EventListener):
    """Runs the pattern-rule trigger for the user when PEARL finds a pattern that may
    become a rule (its detector says so).

    In the background, like PEARL itself: whoever published the pattern (an outcome,
    a turn, the You page) never waits for it. One run per user at a time; patterns
    found meanwhile ask for one more run after it (``PerUserSingleFlight``). The run is
    a manual one (forced), so it re-evaluates even after the day's tick settled the
    trigger; model calls in it are the user's. A failed run is logged by its type.
    """

    def __init__(self, anticipation: AnticipationService, patterns: PatternService) -> None:
        self._anticipation = anticipation
        self._patterns = patterns
        self._flight = PerUserSingleFlight(self._propose, label="Pattern proposal")

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, PatternDetected) or event.user_id is None:
            return
        if self._patterns.proposes_rule(event.pattern):
            self._flight.request(event.user_id)

    async def settle(self) -> None:
        """Wait until no run is in flight."""
        await self._flight.settle()

    async def shutdown(self, grace_s: float = OBSERVER_SHUTDOWN_GRACE_S) -> None:
        """Take no more patterns; give runs in flight ``grace_s``, then cancel the rest."""
        await self._flight.shutdown(grace_s)

    async def _propose(self, user_id: str) -> None:
        end_attribution = attribute_llm_calls(user_id, None)
        try:
            await self._anticipation.run_trigger(user_id, PatternRuleTrigger.name, force=True)
        finally:
            end_attribution()
