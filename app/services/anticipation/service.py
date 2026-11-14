"""Run the proactive triggers under consent, and settle the suggestions they make.

A user's triggers run one at a time under that user's lock, so a run from the API
and the scheduler's tick never overlap for one user: once a day holds (the check
and the insert happen inside the lock), and no two triggers' model calls overlap.

A trigger that has settled the user's local day (it made its suggestion, found one
already made, was denied, or had nothing to say) is remembered in this process, and
the scheduler's tick does not run it again until the next local day, so a denied
trigger does not gather and draft again every minute. A manual run never trusts
that memory: it always re-evaluates (once a day still holds, by the stored
suggestion), and what it settles updates the memory. A second manual run of the
same trigger within ``ANTICIPATION_MANUAL_COOLDOWN_SECONDS`` gets the first one's
outcome. Consent is read only when a run reaches it. Per user per tick that comes
to: no reads while every trigger is settled; otherwise one profile read, plus one
existence check per unsettled trigger that is due (a trigger outside its window
reads nothing), and the two consent reads (constraints, dismissed keys) only when a
run reaches consent.

A trigger whose run fails (its habits cannot be read while the model is down, say)
is not run again on every tick: the scheduler's tick waits 1, 2, 4, ... minutes
before trying it again for that user, never more than 30, and a run that does not
fail resets the wait. A tick that is waiting reads nothing. In this process only, for
at most ``_BACKOFF_ENTRIES`` (user, trigger) pairs.

Accepting a suggestion is claim-then-execute: a compare-and-set moves it from
pending to accepted before its action runs, so a double-click runs the action once
and the second click is a 409. However the action ends without finishing (any
error, or a cancellation) the claim is released again.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.settings import AnticipationSettings, default_settings
from app.domain import errors
from app.domain.anticipation import (
    ActionResult,
    ConsentContext,
    ConsentPolicy,
    ProactiveAction,
    Trigger,
    TriggerContext,
    TriggerOutcome,
    move_suggestion,
)
from app.domain.entities import (
    HOURS_PER_DAY,
    AutonomyMode,
    BehavioralConstraint,
    Suggestion,
    SuggestionExpiry,
    SuggestionStatus,
)
from app.domain.ports import ConstraintRepository, ProfileRepository, SuggestionRepository
from app.services.anticipation.user_locks import UserLocks
from app.services.planning.local_time import user_zone
from app.services.planning.rule_windows import DAY_START_MIN, blocked_windows
from app.services.task_service import utc_now

logger = logging.getLogger(__name__)

# The two list reads, both newest first, so a bound only ever drops the oldest rows.
# A user has a handful of open suggestions; dismissals pile up a few a week.
_LIST_LIMIT = 100
_DISMISSED_LIMIT = 500

_OUT_OF_DATE = "This suggestion is out of date — a newer plan replaced it."

# Outcomes that hold for the rest of the user's local day.
_SETTLES_THE_DAY = frozenset({"created", "already_done", "denied", "nothing_to_say"})

# A failing trigger's wait before the tick tries it again: doubling from the first, capped.
_BACKOFF_FIRST = timedelta(minutes=1)
_BACKOFF_CAP = timedelta(minutes=30)
_BACKOFF_ENTRIES = 10_000  # the oldest wait is forgotten past this many


class ActionFactory:
    """Rebuilds the action stored in a suggestion from its payload (Factory)."""

    def __init__(self) -> None:
        self._builders: dict[str, Callable[[dict], ProactiveAction]] = {}

    def register(self, kind: str, build: Callable[[dict], ProactiveAction]) -> None:
        """Rebuild payloads of ``kind`` with ``build``."""
        self._builders[kind] = build

    def from_payload(self, payload: dict) -> ProactiveAction:
        """The action ``payload`` describes; ValidationError for a kind with no builder."""
        kind = payload.get("kind")
        build = self._builders.get(kind) if isinstance(kind, str) else None
        if build is None:
            raise errors.ValidationError(
                "This suggestion can't be carried out.", details={"kind": kind}
            )
        return build(payload)


def _whole_local_hours(start_min: int, end_min: int) -> tuple[int, int] | None:
    """A window in plan minutes (0 = DAY_START_HOUR local) as the local hours it fully covers.

    Rounded inwards (14:30-16:00 covers 15:00-16:00), so a rule never reads as
    covering more than the user said. None when it covers no whole hour.
    """
    start = -(-(DAY_START_MIN + start_min) // 60)
    end = (DAY_START_MIN + end_min) // 60
    if end <= start:
        return None
    return start % HOURS_PER_DAY, end % HOURS_PER_DAY


def stated_blocks(constraints: list[BehavioralConstraint]) -> list[tuple[int, int, str]]:
    """The hours the user said (never inferred) to keep free, on their local clock.

    Each is (start hour, end hour, their words); end <= start wraps past midnight. The
    hours are what the words block (``rule_windows.blocked_windows``): "No work after
    10 PM" keeps 22:00-08:00, "I work only after 6 PM" keeps 08:00-18:00.
    """
    blocks = []
    for constraint in constraints:
        if constraint.source != "said":
            continue
        for window in blocked_windows(constraint.raw_text):
            hours = _whole_local_hours(*window)
            if hours is not None:
                blocks.append((*hours, constraint.raw_text))
    return blocks


def _aware(now: datetime) -> datetime:
    """A naive instant is UTC, as it is in storage."""
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


@dataclass(frozen=True)
class _Backoff:
    """A failing trigger's wait for one user: not before ``until``; the last wait, ``delay``."""

    until: datetime
    delay: timedelta


@dataclass(frozen=True)
class _Settled:
    """A trigger's outcome for one local day: [since, until) as instants."""

    since: datetime
    until: datetime
    outcome: TriggerOutcome


class AnticipationService:
    """The proactive triggers, the consent chain they answer to, and their suggestions."""

    def __init__(
        self,
        triggers: dict[str, Trigger],
        actions: ActionFactory,
        consent: ConsentPolicy,
        suggestions: SuggestionRepository,
        profiles: ProfileRepository,
        constraints: ConstraintRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
        settings: AnticipationSettings | None = None,
    ) -> None:
        self._triggers = dict(triggers)
        self._actions = actions
        self._consent = consent
        self._suggestions = suggestions
        self._profiles = profiles
        self._constraints = constraints
        self._clock = clock
        self._settings = settings or default_settings().anticipation
        self._locks = UserLocks()
        # One entry per (user, trigger), replaced each local day.
        self._settled: dict[tuple[str, str], _Settled] = {}
        # The last manual run per (user, trigger): when it ran and how it ended.
        self._manual: dict[tuple[str, str], tuple[datetime, TriggerOutcome]] = {}
        # Per (user, trigger) whose last tick failed: when the tick may try it again.
        self._backoff: OrderedDict[tuple[str, str], _Backoff] = OrderedDict()

    def register_trigger(self, trigger: Trigger) -> None:
        """Add ``trigger`` (or replace the one with its name)."""
        self._triggers[trigger.name] = trigger

    def triggers(self) -> list[tuple[str, str]]:
        """Every registered trigger as (name, one-line description), in registration order."""
        return [(name, trigger.description) for name, trigger in self._triggers.items()]

    async def run_trigger(
        self, user_id: str, name: str, *, force: bool = False, now: datetime | None = None
    ) -> TriggerOutcome:
        """Run one trigger for the user; NotFoundError if no trigger has that name.

        A forced run is a manual one (``POST /anticipation/run``). It ignores the day
        memory and always re-evaluates; the stored-suggestion check still keeps it to
        one suggestion per local day. Within ``ANTICIPATION_MANUAL_COOLDOWN_SECONDS``
        of the previous manual run of this trigger for this user, it returns that
        run's outcome instead. An unforced run returns the remembered outcome of a
        trigger that already settled the user's local day.

        A manual run that finds the trigger's suggestion for the day already made
        returns ``already_done`` together with that suggestion.
        """
        trigger = self._triggers.get(name)
        if trigger is None:
            raise errors.NotFoundError("That trigger does not exist.", details={"trigger": name})
        now = _aware(now or self._clock())
        async with self._locks.hold(user_id):
            previous = (
                self._recent_manual(user_id, name, now)
                if force
                else self._remembered(user_id, name, now)
            )
            if previous is not None:
                return previous
            ctx = await self._context(user_id, now, force=force)
            outcome = await self._run(trigger, ctx, self._consent_loader(ctx))
            if force:
                if outcome.status == "already_done" and outcome.suggestion is None:
                    made = await self._made_today(ctx, name)
                    outcome = outcome.model_copy(update={"suggestion": made})
                self._manual[(user_id, name)] = (now, outcome)
            return outcome

    async def run_due(self, user_id: str, now: datetime) -> list[TriggerOutcome]:
        """Run every trigger for the user as of ``now``, one after another.

        One outcome per trigger. A trigger that raises is logged (its name and the
        exception type) and reported as "failed"; the next one still runs, and the
        failed one waits out its backoff (reported "not_due" meanwhile).
        """
        triggers = list(self._triggers.values())
        if not triggers:
            return []
        now = _aware(now)
        async with self._locks.hold(user_id):
            remembered = {t.name: self._remembered(user_id, t.name, now) for t in triggers}
            for trigger in triggers:
                if remembered[trigger.name] is None and self._backing_off(user_id, trigger, now):
                    remembered[trigger.name] = TriggerOutcome(status="not_due")
            if all(outcome is not None for outcome in remembered.values()):
                return [remembered[t.name] for t in triggers]
            ctx = await self._context(user_id, now, force=False)
            load_consent = self._consent_loader(ctx)
            outcomes = []
            for trigger in triggers:
                outcome = remembered[trigger.name]
                if outcome is None:
                    try:
                        outcome = await self._run(trigger, ctx, load_consent)
                    except Exception as exc:  # one trigger's failure never stops the next
                        logger.warning(
                            "Trigger %s for user %s could not finish (%s)",
                            trigger.name,
                            user_id,
                            type(exc).__name__,
                        )
                        outcome = TriggerOutcome(status="failed")
                        self._back_off(user_id, trigger, now)
                    else:
                        self._backoff.pop((user_id, trigger.name), None)
                outcomes.append(outcome)
            return outcomes

    def _backing_off(self, user_id: str, trigger: Trigger, now: datetime) -> bool:
        """Whether the trigger failed for the user lately and its wait is not over."""
        wait = self._backoff.get((user_id, trigger.name))
        return wait is not None and now < wait.until

    def _back_off(self, user_id: str, trigger: Trigger, now: datetime) -> None:
        """Double the trigger's wait for the user (the first is a minute, at most 30)."""
        key = (user_id, trigger.name)
        last = self._backoff.pop(key, None)
        delay = _BACKOFF_FIRST if last is None else min(last.delay * 2, _BACKOFF_CAP)
        self._backoff[key] = _Backoff(until=now + delay, delay=delay)
        while len(self._backoff) > _BACKOFF_ENTRIES:
            self._backoff.popitem(last=False)

    async def list_suggestions(
        self, user_id: str, *, status: SuggestionStatus | None = SuggestionStatus.PENDING
    ) -> list[Suggestion]:
        """The user's newest suggestions in ``status`` (every status for None), newest first.

        A pending suggestion past its ``expires_at`` is not listed as pending.
        """
        found = await self._suggestions.list(user_id, status=status, limit=_LIST_LIMIT)
        if status == SuggestionStatus.PENDING:
            now = self._clock()
            found = [s for s in found if s.expires_at is None or s.expires_at > now]
        return found

    async def accept(self, user_id: str, suggestion_id: str) -> tuple[Suggestion, ActionResult]:
        """Claim the pending suggestion, then run its action.

        Raises NotFoundError if the user has no such suggestion, StaleVersionError
        (409) if it is no longer pending, and InvalidStateTransition (409) if it is
        past its ``expires_at`` or its action is out of date, after marking it
        expired. Any other conflict the action meets (its plan clashes with the
        user's tasks: no retry can fix it) also marks it expired and is raised as it
        came, and its trigger may make a fresh suggestion that day. Any other way the
        action ends without finishing, an error or a cancellation, puts it back to
        pending and is raised as it came.
        """
        suggestion = await self._suggestions.get(user_id, suggestion_id)
        if suggestion is None:
            raise errors.NotFoundError(
                "That suggestion does not exist.", details={"suggestion_id": suggestion_id}
            )
        if suggestion.expires_at is not None and self._clock() >= suggestion.expires_at:
            await move_suggestion(
                self._suggestions, user_id, suggestion_id,
                SuggestionStatus.PENDING, SuggestionStatus.EXPIRED,
                expired_because=SuggestionExpiry.OUT_OF_DATE,
            )
            raise errors.InvalidStateTransition(
                _OUT_OF_DATE, details={"suggestion_id": suggestion_id}
            )
        action = self._actions.from_payload(suggestion.action)
        claimed = await move_suggestion(
            self._suggestions, user_id, suggestion_id,
            SuggestionStatus.PENDING, SuggestionStatus.ACCEPTED,
        )
        try:
            result = await action.execute(user_id)
        except (errors.InvalidStateTransition, errors.NotFoundError) as exc:
            # A newer plan replaced it: it was still today's, and counts as made.
            await asyncio.shield(self._release(
                user_id, suggestion_id, SuggestionStatus.EXPIRED,
                because=SuggestionExpiry.OUT_OF_DATE,
            ))
            raise errors.InvalidStateTransition(
                _OUT_OF_DATE, details={"suggestion_id": suggestion_id}
            ) from exc
        except errors.ConflictError:
            # Never handed back as pending: every tap would meet the same conflict. Its
            # plan no longer fits, so a fresh one may be made today.
            await asyncio.shield(self._release(
                user_id, suggestion_id, SuggestionStatus.EXPIRED,
                because=SuggestionExpiry.NO_FIT,
            ))
            self._settled.pop((user_id, suggestion.trigger), None)
            raise
        except BaseException:
            # Shielded: a cancellation must not also cancel the release.
            await asyncio.shield(self._release(user_id, suggestion_id, SuggestionStatus.PENDING))
            raise
        return claimed, result

    async def dismiss(self, user_id: str, suggestion_id: str) -> Suggestion:
        """Dismiss a pending suggestion: its dedupe_key is never proposed again.

        NotFoundError if the user has no such suggestion; StaleVersionError if it
        is no longer pending.
        """
        return await move_suggestion(
            self._suggestions, user_id, suggestion_id,
            SuggestionStatus.PENDING, SuggestionStatus.DISMISSED,
        )

    async def _run(
        self,
        trigger: Trigger,
        ctx: TriggerContext,
        load_consent: Callable[[], Awaitable[ConsentContext]],
    ) -> TriggerOutcome:
        outcome = await trigger.run(
            ctx, consent=self._consent, consent_ctx=load_consent, suggestions=self._suggestions
        )
        if outcome.status in _SETTLES_THE_DAY:
            zone = ZoneInfo(ctx.profile.timezone)
            since = datetime.combine(ctx.local_day, time(0), tzinfo=zone)
            self._settled[(ctx.user_id, trigger.name)] = _Settled(
                since=since,
                until=datetime.combine(ctx.local_day + timedelta(days=1), time(0), tzinfo=zone),
                # Made today means done for today.
                outcome=TriggerOutcome(status="already_done")
                if outcome.status == "created"
                else outcome,
            )
        return outcome

    async def _made_today(self, ctx: TriggerContext, name: str) -> Suggestion | None:
        """The newest suggestion trigger ``name`` made the user on their local day, if any."""
        zone = ZoneInfo(ctx.profile.timezone)
        for suggestion in await self._suggestions.list(ctx.user_id, status=None, limit=_LIST_LIMIT):
            made_on = _aware(suggestion.created_at).astimezone(zone).date()
            if suggestion.trigger == name and made_on == ctx.local_day:
                return suggestion
        return None

    def _recent_manual(self, user_id: str, name: str, now: datetime) -> TriggerOutcome | None:
        """The previous manual run's outcome, if it ran within the cooldown before ``now``."""
        recent = self._manual.get((user_id, name))
        if recent is None:
            return None
        ran_at, outcome = recent
        cooldown = timedelta(seconds=self._settings.manual_cooldown_seconds)
        return outcome if ran_at <= now < ran_at + cooldown else None

    def _remembered(self, user_id: str, name: str, now: datetime) -> TriggerOutcome | None:
        """The trigger's outcome if it already settled the local day ``now`` falls on."""
        settled = self._settled.get((user_id, name))
        if settled is None or not settled.since <= now < settled.until:
            return None
        return settled.outcome

    async def _release(
        self,
        user_id: str,
        suggestion_id: str,
        to: SuggestionStatus,
        *,
        because: SuggestionExpiry | None = None,
    ) -> None:
        """Move a claimed suggestion whose action did not finish to ``to`` (an expiry with
        its reason, ``because``).

        A failure is logged, never raised: the caller re-raises the action's own error.
        """
        try:
            await move_suggestion(
                self._suggestions, user_id, suggestion_id, SuggestionStatus.ACCEPTED, to,
                expired_because=because,
            )
        except Exception as exc:
            logger.warning(
                "Suggestion %s of user %s stays accepted: could not mark it %s (%s)",
                suggestion_id,
                user_id,
                to.value,
                type(exc).__name__,
            )

    async def _context(self, user_id: str, now: datetime, *, force: bool) -> TriggerContext:
        profile = await self._profiles.get_or_create(user_id)
        zone = user_zone(profile)
        if profile.timezone != zone.key:
            # An unknown zone falls back to the default; triggers and the repositories
            # read the local day off profile.timezone, so it must name that same zone.
            profile = profile.model_copy(update={"timezone": zone.key})
        return TriggerContext(
            user_id=user_id,
            profile=profile,
            now=now,
            local_day=now.astimezone(zone).date(),
            force=force,
        )

    def _consent_loader(self, ctx: TriggerContext) -> Callable[[], Awaitable[ConsentContext]]:
        """Reads the user's consent context the first time a run reaches consent, once."""
        loaded: list[ConsentContext] = []
        user_id, mode = ctx.user_id, ctx.profile.autonomy_mode

        async def load() -> ConsentContext:
            if not loaded:
                loaded.append(await self._consent_context(user_id, mode))
            return loaded[0]

        return load

    async def _consent_context(self, user_id: str, mode: AutonomyMode) -> ConsentContext:
        dismissed = await self._suggestions.dedupe_keys(
            user_id, status=SuggestionStatus.DISMISSED, limit=_DISMISSED_LIMIT
        )
        return ConsentContext(
            autonomy_mode=mode,
            stated_blocks=await self._stated_blocks(user_id),
            dismissed_keys=frozenset(dismissed),
        )

    async def _stated_blocks(self, user_id: str) -> list[tuple[int, int, str]]:
        """The hours the user said (never inferred) to keep free, on their local clock."""
        return stated_blocks(await self._constraints.list_active(user_id))
