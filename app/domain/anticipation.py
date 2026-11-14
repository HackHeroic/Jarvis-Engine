"""Anticipation: the actions Jarvis proposes on its own, and who decides whether they run.

A ``Trigger`` (Template Method) notices something worth saying and drafts a
``ProactiveAction`` (Command). Before anything is stored, the action passes the
``ConsentPolicy``: a Chain of Responsibility whose first decisive rule wins, so
every decision carries a typed reason (``ConsentReasonKind``) and a plain sentence
the user can read. The last link always decides, by the user's autonomy mode
(``AutonomyPolicy``, a Strategy).
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities import (
    HOURS_PER_DAY,
    AutonomyMode,
    ConsentDecision,  # noqa: F401  (defined beside Suggestion; this module's public name)
    ConsentReasonKind,
    Suggestion,
    SuggestionStatus,
    UserProfile,
    check_suggestion_move,
)
from app.domain.ports import SuggestionRepository

logger = logging.getLogger(__name__)


class ActionRisk(str, Enum):
    """How much an action changes if it runs."""

    READ_ONLY = "read_only"
    LOW_RISK = "low_risk"
    SCHEDULE_CHANGE = "schedule_change"


class ActionResult(BaseModel):
    """What running a proactive action did."""

    kind: str = Field(description="Kind of the action that ran.")
    message: str = Field(description="What happened, as shown to the user.")
    tasks_written: int | None = Field(
        default=None, description="Tasks written to the user's plan, if the action wrote any."
    )
    draft_id: str | None = Field(
        default=None, description="Draft the action proposed for review, if it made one."
    )


class ProactiveAction(ABC):
    """Something Jarvis could do on the user's behalf (Command).

    Stored inside a suggestion as ``to_payload()``, and rebuilt from it when the
    user accepts.
    """

    kind: ClassVar[str]
    risk: ClassVar[ActionRisk]

    @abstractmethod
    def describe(self) -> str:
        """One line saying what the action would do."""

    @abstractmethod
    def to_payload(self) -> dict:
        """The action as stored data; includes "kind" and "dedupe_key"."""

    @abstractmethod
    async def execute(self, user_id: str) -> ActionResult:
        """Do it for ``user_id`` and say what happened."""

    def dedupe_key(self) -> str:
        """Identity of the proposal: a dismissed key is never proposed again."""
        return self.kind

    def blocked_hours(self) -> tuple[int, int] | None:
        """The local clock hours [start, end) the action would block, if any.

        ``end <= start`` wraps past midnight: (22, 8) is 22:00 to 08:00.
        """
        return None


class ConsentContext(BaseModel):
    """What the consent chain knows about the user when it decides."""

    autonomy_mode: AutonomyMode = Field(description="How far the user lets Jarvis act alone.")
    stated_blocks: list[tuple[int, int, str]] = Field(
        default_factory=list,
        description=(
            "Hours the user said to keep free, as local (start hour, end hour, their words);"
            " end <= start wraps past midnight."
        ),
    )
    dismissed_keys: frozenset[str] = Field(
        default_factory=frozenset,
        description="dedupe_key of every suggestion the user dismissed.",
    )


def clock_hours(start: int, end: int) -> set[int]:
    """The local hours in [start, end); ``end <= start`` wraps past midnight."""
    span = end - start if end > start else end + HOURS_PER_DAY - start
    return {(start + i) % HOURS_PER_DAY for i in range(span)}


class ConsentRule(ABC):
    """One link of the consent chain: decide, or pass to the next link (return None)."""

    @abstractmethod
    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision | None:
        """Return a decision, or None to let the next rule decide."""


class RepeatDismissalRule(ConsentRule):
    """Something the user dismissed is never brought up again."""

    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision | None:
        if action.dedupe_key() not in ctx.dismissed_keys:
            return None
        return ConsentDecision(
            behavior="deny",
            kind=ConsentReasonKind.LEARNED_PREFERENCE,
            message="You dismissed this before, so I won't bring it up again.",
        )


class RiskCheckRule(ConsentRule):
    """Information changes nothing, so it may always be shown."""

    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision | None:
        if action.risk is not ActionRisk.READ_ONLY:
            return None
        return ConsentDecision(
            behavior="allow",
            kind=ConsentReasonKind.RISK_CHECK,
            message="Nothing changes until you act on it.",
        )


class StatedRuleCoverageRule(ConsentRule):
    """Blocking hours the user already said to keep free would only repeat them."""

    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision | None:
        hours = action.blocked_hours()
        if hours is None:
            return None
        wanted = clock_hours(*hours)
        for start, end, raw_text in ctx.stated_blocks:
            if wanted <= clock_hours(start, end):
                return ConsentDecision(
                    behavior="deny",
                    kind=ConsentReasonKind.RULE_MATCH,
                    message=f'You already told me: "{raw_text}".',
                )
        return None


class AutonomyPolicy(ABC):
    """How far Jarvis may act alone under one autonomy mode (Strategy)."""

    @abstractmethod
    def decide(self, action: ProactiveAction) -> ConsentDecision:
        """Allow or ask; a mode never denies."""


def _mode(behavior: Literal["allow", "ask"], message: str) -> ConsentDecision:
    return ConsentDecision(behavior=behavior, kind=ConsentReasonKind.MODE_DECISION, message=message)


_ASKING_FIRST = "This changes your schedule, so I'm asking first."


class SuggestOnly(AutonomyPolicy):
    def decide(self, action: ProactiveAction) -> ConsentDecision:
        return _mode("ask", "You asked me to only suggest.")


class AskFirst(AutonomyPolicy):
    def decide(self, action: ProactiveAction) -> ConsentDecision:
        return _mode("ask", _ASKING_FIRST)


class AutoLowRisk(AutonomyPolicy):
    def decide(self, action: ProactiveAction) -> ConsentDecision:
        if action.risk is ActionRisk.LOW_RISK:
            return _mode("allow", "You let me make small changes like this on my own.")
        return _mode("ask", _ASKING_FIRST)


_POLICIES: dict[AutonomyMode, type[AutonomyPolicy]] = {
    AutonomyMode.SUGGEST_ONLY: SuggestOnly,
    AutonomyMode.ASK_FIRST: AskFirst,
    AutonomyMode.AUTO_LOW_RISK: AutoLowRisk,
}


def autonomy_policy_for(mode: AutonomyMode) -> AutonomyPolicy:
    """The policy for ``mode``."""
    return _POLICIES[AutonomyMode(mode)]()


class ModePolicyRule(ConsentRule):
    """The chain's last link: it always decides, by the user's autonomy mode."""

    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision:
        return autonomy_policy_for(ctx.autonomy_mode).decide(action)


class ConsentPolicy:
    """The consent chain (Chain of Responsibility): the first rule that decides, wins."""

    def __init__(self, rules: list[ConsentRule]) -> None:
        if not rules or not isinstance(rules[-1], ModePolicyRule):
            raise ValueError(
                "The consent chain must end with a ModePolicyRule, which always decides."
            )
        self._rules = list(rules[:-1])
        self._last: ModePolicyRule = rules[-1]

    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision:
        for rule in self._rules:
            decision = rule.evaluate(action, ctx)
            if decision is not None:
                return decision
        return self._last.evaluate(action, ctx)

    @classmethod
    def default(cls) -> "ConsentPolicy":
        """Repeat dismissal, then read-only, then an already-stated rule, then the mode."""
        return cls(
            [RepeatDismissalRule(), RiskCheckRule(), StatedRuleCoverageRule(), ModePolicyRule()]
        )


# --- Triggers ---------------------------------------------------------------------------


class TriggerContext(BaseModel):
    """One run of a trigger for one user."""

    user_id: str = Field(description="User the trigger runs for.")
    profile: UserProfile = Field(description="The user's profile; its timezone is a real zone.")
    now: datetime = Field(description="The instant the trigger runs at.")
    local_day: date = Field(description="The calendar date of ``now`` on the user's clock.")
    force: bool = Field(
        default=False, description="Run even if not due; once a day still holds."
    )


class SuggestionDraft(BaseModel):
    """What a trigger wants to say, before consent decides whether it is stored."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: str = Field(description="Kind of suggestion (e.g. morning_brief, pattern_rule).")
    title: str = Field(description="Short headline shown to the user.")
    body: str = Field(description="Full suggestion text shown to the user.")
    action: ProactiveAction = Field(description="What accepting the suggestion would do.")
    expires_at: datetime | None = Field(
        default=None, description="After this instant the suggestion is stale."
    )


class TriggerOutcome(BaseModel):
    """How one trigger run ended."""

    status: Literal[
        "created", "already_done", "not_due", "nothing_to_say", "denied", "failed"
    ] = Field(description="Where the run stopped; 'failed' means the trigger raised.")
    suggestion: Suggestion | None = Field(
        default=None,
        description=(
            "The suggestion stored, when one was; for a manual run that found the day's"
            " suggestion already made, that one."
        ),
    )
    decision: ConsentDecision | None = Field(
        default=None, description="The consent decision, when consent was asked."
    )


async def move_suggestion(
    suggestions: SuggestionRepository,
    user_id: str,
    suggestion_id: str,
    frm: SuggestionStatus,
    to: SuggestionStatus,
    *,
    consent: ConsentDecision | None = None,
) -> Suggestion:
    """The one way a suggestion's status changes: a move SUGGESTION_TRANSITIONS allows,
    stored as a compare-and-set from ``frm``.

    InvalidStateTransition for a move the table forbids (nothing is written);
    otherwise as ``SuggestionRepository.set_status``.
    """
    to = check_suggestion_move(frm, to)
    return await suggestions.set_status(
        user_id, suggestion_id, to, expected=frm, consent=consent
    )


# A suggestion whose automatic change did not go through waits for the user instead.
ASK_AFTER_FAILED_APPLY = ConsentDecision(
    behavior="ask",
    kind=ConsentReasonKind.MODE_DECISION,
    message="I couldn't make this change on my own, so I'm asking first.",
)


async def _ask_instead(suggestions: SuggestionRepository, user_id: str, suggestion_id: str) -> None:
    """Release a claimed auto-apply back to PENDING, asking; a failure is logged, not raised."""
    try:
        await move_suggestion(
            suggestions,
            user_id,
            suggestion_id,
            SuggestionStatus.ACCEPTED,
            SuggestionStatus.PENDING,
            consent=ASK_AFTER_FAILED_APPLY,
        )
    except Exception as exc:  # the caller raises the action's own error
        logger.warning(
            "Suggestion %s of user %s stays claimed: could not hand it back (%s)",
            suggestion_id,
            user_id,
            type(exc).__name__,
        )


class Trigger(ABC):
    """A proactive trigger (Template Method).

    ``run`` is the fixed skeleton; a trigger supplies the steps. Timing, once a
    day, gathering, drafting, consent and storage always happen in that order.
    """

    name: ClassVar[str]
    # One line saying what the trigger offers, for the list of registered triggers.
    description: ClassVar[str] = ""

    async def run(
        self,
        ctx: TriggerContext,
        *,
        consent: ConsentPolicy,
        consent_ctx: ConsentContext | Callable[[], Awaitable[ConsentContext]],
        suggestions: SuggestionRepository,
    ) -> TriggerOutcome:
        """Run the trigger once; subclasses never override this.

        ``consent_ctx`` may be a loader, awaited only when a run reaches consent: a
        run that stops earlier reads nothing for it.

        An allowed action that changes something runs at once. Its suggestion is
        stored already claimed (ACCEPTED), so no accept can run the action a second
        time, and becomes AUTO_APPLIED once the action is done. If the action fails
        or is cancelled, the suggestion goes back to PENDING, asking the user, and
        the error is raised. An allowed read-only one is stored as PENDING: it is
        information. A denied one is not stored; the decision is returned.
        """
        # The timing check is free and the once-a-day check is a read: a tick outside
        # the trigger's window reads nothing.
        if not ctx.force and not await self.should_fire(ctx):
            return TriggerOutcome(status="not_due")
        if await self.already_done(ctx, suggestions):
            return TriggerOutcome(status="already_done")
        gathered = await self.gather(ctx)
        if gathered is None:
            return TriggerOutcome(status="nothing_to_say")
        draft = await self.build_suggestion(ctx, gathered)
        if not isinstance(consent_ctx, ConsentContext):
            consent_ctx = await consent_ctx()
        decision = consent.evaluate(draft.action, consent_ctx)
        if decision.behavior == "deny":
            return TriggerOutcome(status="denied", decision=decision)

        applies_now = decision.behavior == "allow" and draft.action.risk is not ActionRisk.READ_ONLY
        stored = await suggestions.add(
            ctx.user_id,
            Suggestion(
                user_id=ctx.user_id,
                trigger=self.name,
                kind=draft.kind,
                title=draft.title,
                body=draft.body,
                action=draft.action.to_payload(),
                status=SuggestionStatus.ACCEPTED if applies_now else SuggestionStatus.PENDING,
                consent_behavior=decision.behavior,
                consent_reason=decision.message,
                consent_kind=decision.kind,
                created_at=ctx.now,
                expires_at=draft.expires_at,
            ),
        )
        if applies_now:
            try:
                await draft.action.execute(ctx.user_id)
            except BaseException:
                # Whatever stopped it, cancellation included: hand it to the user.
                await asyncio.shield(_ask_instead(suggestions, ctx.user_id, stored.id))
                raise
            stored = await move_suggestion(
                suggestions,
                ctx.user_id,
                stored.id,
                SuggestionStatus.ACCEPTED,
                SuggestionStatus.AUTO_APPLIED,
            )
        return TriggerOutcome(status="created", suggestion=stored, decision=decision)

    async def already_done(self, ctx: TriggerContext, suggestions: SuggestionRepository) -> bool:
        """Whether this trigger already made the user a suggestion on their local day."""
        return await suggestions.exists_today(
            ctx.user_id, self.name, ctx.local_day, tz=ctx.profile.timezone
        )

    @abstractmethod
    async def should_fire(self, ctx: TriggerContext) -> bool:
        """Whether now is the time (a forced run skips this)."""

    @abstractmethod
    async def gather(self, ctx: TriggerContext) -> Any | None:
        """What there is to say; None means nothing: no model call, nothing stored."""

    @abstractmethod
    async def build_suggestion(self, ctx: TriggerContext, gathered: Any) -> SuggestionDraft:
        """Turn what was gathered into a suggestion and the action behind it."""
