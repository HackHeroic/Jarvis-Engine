"""Run the proactive triggers under consent, and settle the suggestions they make.

Triggers run for one user at a time and one after another, so no two triggers'
model calls ever overlap. Accepting a suggestion is claim-then-execute: a
compare-and-set moves it from pending to accepted before its action runs, so a
double-click runs the action once and the second click is a 409.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from app.domain import errors
from app.domain.anticipation import (
    ActionResult,
    ConsentContext,
    ConsentPolicy,
    ProactiveAction,
    Trigger,
    TriggerContext,
    TriggerOutcome,
)
from app.domain.entities import HOURS_PER_DAY, Suggestion, SuggestionStatus
from app.domain.ports import ConstraintRepository, ProfileRepository, SuggestionRepository
from app.services.memory.constraint_bridge import DAY_START_MIN, _parse_time_range
from app.services.planning.local_time import user_zone
from app.services.task_service import utc_now

logger = logging.getLogger(__name__)

# The two list reads, both newest first, so a bound only ever drops the oldest rows.
# A user has a handful of open suggestions; dismissals pile up a few a week.
_LIST_LIMIT = 100
_DISMISSED_LIMIT = 500

_OUT_OF_DATE = "This suggestion is out of date — a newer plan replaced it."


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
    ) -> None:
        self._triggers = dict(triggers)
        self._actions = actions
        self._consent = consent
        self._suggestions = suggestions
        self._profiles = profiles
        self._constraints = constraints
        self._clock = clock

    def register_trigger(self, trigger: Trigger) -> None:
        """Add ``trigger`` (or replace the one with its name)."""
        self._triggers[trigger.name] = trigger

    async def run_trigger(
        self, user_id: str, name: str, *, force: bool = False, now: datetime | None = None
    ) -> TriggerOutcome:
        """Run one trigger for the user; NotFoundError if no trigger has that name."""
        trigger = self._triggers.get(name)
        if trigger is None:
            raise errors.NotFoundError("That trigger does not exist.", details={"trigger": name})
        ctx, consent_ctx = await self._contexts(user_id, now or self._clock(), force=force)
        return await self._run(trigger, ctx, consent_ctx)

    async def run_due(self, user_id: str, now: datetime) -> list[TriggerOutcome]:
        """Run every trigger for the user as of ``now``, one after another."""
        if not self._triggers:
            return []
        ctx, consent_ctx = await self._contexts(user_id, now, force=False)
        triggers = list(self._triggers.values())  # a trigger registered meanwhile waits a tick
        return [await self._run(trigger, ctx, consent_ctx) for trigger in triggers]

    async def list_suggestions(
        self, user_id: str, *, status: SuggestionStatus | None = SuggestionStatus.PENDING
    ) -> list[Suggestion]:
        """The user's newest suggestions in ``status`` (every status for None), newest first."""
        return await self._suggestions.list(user_id, status=status, limit=_LIST_LIMIT)

    async def accept(self, user_id: str, suggestion_id: str) -> tuple[Suggestion, ActionResult]:
        """Claim the pending suggestion, then run its action.

        Raises NotFoundError if the user has no such suggestion, StaleVersionError
        (409) if it is no longer pending, and InvalidStateTransition (409) if its
        action is out of date, after marking it expired. A PersistenceError (503)
        from the action puts it back to pending, so it can be accepted again.
        """
        suggestion = await self._suggestions.get(user_id, suggestion_id)
        if suggestion is None:
            raise errors.NotFoundError(
                "That suggestion does not exist.", details={"suggestion_id": suggestion_id}
            )
        action = self._actions.from_payload(suggestion.action)
        claimed = await self._suggestions.set_status(
            user_id, suggestion_id, SuggestionStatus.ACCEPTED, expected=SuggestionStatus.PENDING
        )
        try:
            result = await action.execute(user_id)
        except (errors.InvalidStateTransition, errors.NotFoundError) as exc:
            await self._release(user_id, suggestion_id, SuggestionStatus.EXPIRED)
            raise errors.InvalidStateTransition(
                _OUT_OF_DATE, details={"suggestion_id": suggestion_id}
            ) from exc
        except errors.PersistenceError:
            await self._release(user_id, suggestion_id, SuggestionStatus.PENDING)
            raise
        return claimed, result

    async def dismiss(self, user_id: str, suggestion_id: str) -> Suggestion:
        """Dismiss a pending suggestion: its dedupe_key is never proposed again.

        NotFoundError if the user has no such suggestion; StaleVersionError if it
        is no longer pending.
        """
        return await self._suggestions.set_status(
            user_id, suggestion_id, SuggestionStatus.DISMISSED, expected=SuggestionStatus.PENDING
        )

    async def _run(
        self, trigger: Trigger, ctx: TriggerContext, consent_ctx: ConsentContext
    ) -> TriggerOutcome:
        return await trigger.run(
            ctx, consent=self._consent, consent_ctx=consent_ctx, suggestions=self._suggestions
        )

    async def _release(self, user_id: str, suggestion_id: str, to: SuggestionStatus) -> None:
        """Move a claimed suggestion whose action did not finish to ``to``.

        A failure is logged, never raised: the caller re-raises the action's own error.
        """
        try:
            await self._suggestions.set_status(
                user_id, suggestion_id, to, expected=SuggestionStatus.ACCEPTED
            )
        except errors.JarvisError as exc:
            logger.warning(
                "Suggestion %s of user %s stays accepted: could not mark it %s (%s)",
                suggestion_id,
                user_id,
                to.value,
                type(exc).__name__,
            )

    async def _contexts(
        self, user_id: str, now: datetime, *, force: bool
    ) -> tuple[TriggerContext, ConsentContext]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)  # a naive instant is UTC, as in storage
        profile = await self._profiles.get_or_create(user_id)
        zone = user_zone(profile)
        if profile.timezone != zone.key:
            # An unknown zone falls back to the default; triggers and the repositories
            # read the local day off profile.timezone, so it must name that same zone.
            profile = profile.model_copy(update={"timezone": zone.key})
        ctx = TriggerContext(
            user_id=user_id,
            profile=profile,
            now=now,
            local_day=now.astimezone(zone).date(),
            force=force,
        )
        consent_ctx = ConsentContext(
            autonomy_mode=profile.autonomy_mode,
            stated_blocks=await self._stated_blocks(user_id),
            dismissed_keys=await self._dismissed_keys(user_id),
        )
        return ctx, consent_ctx

    async def _stated_blocks(self, user_id: str) -> list[tuple[int, int, str]]:
        """The hours the user said (never inferred) to keep free, on their local clock."""
        blocks = []
        for constraint in await self._constraints.list_active(user_id):
            if constraint.source != "said":
                continue
            window = _parse_time_range(constraint.raw_text)
            hours = _whole_local_hours(*window) if window is not None else None
            if hours is not None:
                blocks.append((*hours, constraint.raw_text))
        return blocks

    async def _dismissed_keys(self, user_id: str) -> frozenset[str]:
        dismissed = await self._suggestions.list(
            user_id, status=SuggestionStatus.DISMISSED, limit=_DISMISSED_LIMIT
        )
        keys = (s.action.get("dedupe_key") for s in dismissed)
        return frozenset(key for key in keys if isinstance(key, str))
