"""The consent chain: which proactive actions Jarvis may take, ask about, or drop."""

from typing import ClassVar

import pytest

from app.domain.anticipation import (
    ActionResult,
    ActionRisk,
    AskFirst,
    AutoLowRisk,
    ConsentContext,
    ConsentPolicy,
    ModePolicyRule,
    ProactiveAction,
    RepeatDismissalRule,
    RiskCheckRule,
    StatedRuleCoverageRule,
    SuggestOnly,
    autonomy_policy_for,
)
from app.domain.entities import AutonomyMode, ConsentReasonKind


class _Action(ProactiveAction):
    kind: ClassVar[str] = "test_action"
    risk: ClassVar[ActionRisk] = ActionRisk.SCHEDULE_CHANGE

    def __init__(self, hours: tuple[int, int] | None = None, key: str | None = None) -> None:
        self._hours = hours
        self._key = key

    def describe(self) -> str:
        return "Block an hour for review"

    def to_payload(self) -> dict:
        return {"kind": self.kind, "dedupe_key": self.dedupe_key()}

    async def execute(self, user_id: str) -> ActionResult:
        return ActionResult(kind=self.kind, message="Done.")

    def dedupe_key(self) -> str:
        return self._key or self.kind

    def blocked_hours(self) -> tuple[int, int] | None:
        return self._hours


class _ReadOnly(_Action):
    risk: ClassVar[ActionRisk] = ActionRisk.READ_ONLY


class _LowRisk(_Action):
    risk: ClassVar[ActionRisk] = ActionRisk.LOW_RISK


def _ctx(
    mode: AutonomyMode = AutonomyMode.ASK_FIRST,
    *,
    blocks: list[tuple[int, int, str]] | None = None,
    dismissed: frozenset[str] = frozenset(),
) -> ConsentContext:
    return ConsentContext(autonomy_mode=mode, stated_blocks=blocks or [], dismissed_keys=dismissed)


# --- One test per rule outcome -------------------------------------------------------


def test_consent__dismissed_before__deny_learned_preference():
    decision = ConsentPolicy.default().evaluate(
        _Action(key="skip_window:22"), _ctx(dismissed=frozenset({"skip_window:22"}))
    )

    assert decision.behavior == "deny"
    assert decision.kind is ConsentReasonKind.LEARNED_PREFERENCE
    assert decision.message == "You dismissed this before, so I won't bring it up again."


def test_consent__dismissal_outranks_read_only__deny():
    decision = ConsentPolicy.default().evaluate(
        _ReadOnly(), _ctx(dismissed=frozenset({"test_action"}))
    )

    assert (decision.behavior, decision.kind) == ("deny", ConsentReasonKind.LEARNED_PREFERENCE)


def test_consent__read_only__allow_risk_check():
    decision = ConsentPolicy.default().evaluate(_ReadOnly(), _ctx(AutonomyMode.SUGGEST_ONLY))

    assert decision.behavior == "allow"
    assert decision.kind is ConsentReasonKind.RISK_CHECK
    assert decision.message == "Nothing changes until you act on it."


def test_consent__blocked_hours_inside_stated_block__deny_rule_match():
    blocks = [(22, 8, "No work after 10 PM")]

    decision = ConsentPolicy.default().evaluate(_Action(hours=(23, 1)), _ctx(blocks=blocks))

    assert decision.behavior == "deny"
    assert decision.kind is ConsentReasonKind.RULE_MATCH
    assert decision.message == 'You already told me: "No work after 10 PM".'


def test_consent__blocked_hours_reach_past_stated_block__falls_through_to_mode():
    blocks = [(14, 15, "No tasks between 14:00 and 15:00")]

    decision = ConsentPolicy.default().evaluate(_Action(hours=(14, 16)), _ctx(blocks=blocks))

    assert (decision.behavior, decision.kind) == ("ask", ConsentReasonKind.MODE_DECISION)


def test_consent__ask_first_schedule_change__ask_mode_decision():
    decision = ConsentPolicy.default().evaluate(
        _Action(hours=(9, 10)), _ctx(AutonomyMode.ASK_FIRST)
    )

    assert decision.behavior == "ask"
    assert decision.kind is ConsentReasonKind.MODE_DECISION
    assert decision.message == "This changes your schedule, so I'm asking first."


def test_consent__auto_low_risk_low_risk_action__allow_mode_decision():
    decision = ConsentPolicy.default().evaluate(_LowRisk(), _ctx(AutonomyMode.AUTO_LOW_RISK))

    assert (decision.behavior, decision.kind) == ("allow", ConsentReasonKind.MODE_DECISION)


# --- The chain's shape -----------------------------------------------------------------


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [RepeatDismissalRule(), RiskCheckRule()],
        [ModePolicyRule(), StatedRuleCoverageRule()],
    ],
    ids=["empty", "no_mode_rule", "mode_rule_not_last"],
)
def test_consent_policy__last_rule_not_mode__value_error(rules):
    with pytest.raises(ValueError):
        ConsentPolicy(rules)


def test_consent_policy__only_mode_rule__decides_by_mode():
    decision = ConsentPolicy([ModePolicyRule()]).evaluate(
        _Action(), _ctx(AutonomyMode.SUGGEST_ONLY)
    )

    assert (decision.behavior, decision.message) == ("ask", "You asked me to only suggest.")


# --- Autonomy policies (Strategy) ------------------------------------------------------


def test_autonomy__suggest_only__never_allows_a_schedule_change():
    policy = SuggestOnly()

    for action in (_Action(), _LowRisk(), _ReadOnly()):
        decision = policy.decide(action)
        assert decision.behavior == "ask"
        assert decision.kind is ConsentReasonKind.MODE_DECISION
        assert decision.message == "You asked me to only suggest."


def test_autonomy__ask_first__asks_before_any_change():
    for action in (_Action(), _LowRisk()):
        assert AskFirst().decide(action).behavior == "ask"


def test_autonomy__auto_low_risk__allows_low_risk_asks_schedule_change():
    policy = AutoLowRisk()

    allowed = policy.decide(_LowRisk())
    asked = policy.decide(_Action())

    assert (allowed.behavior, allowed.kind) == ("allow", ConsentReasonKind.MODE_DECISION)
    assert (asked.behavior, asked.kind) == ("ask", ConsentReasonKind.MODE_DECISION)
    assert asked.message == "This changes your schedule, so I'm asking first."


@pytest.mark.parametrize(
    ("mode", "policy"),
    [
        (AutonomyMode.SUGGEST_ONLY, SuggestOnly),
        (AutonomyMode.ASK_FIRST, AskFirst),
        (AutonomyMode.AUTO_LOW_RISK, AutoLowRisk),
    ],
)
def test_autonomy_policy_for__each_mode__matching_policy(mode, policy):
    assert isinstance(autonomy_policy_for(mode), policy)
