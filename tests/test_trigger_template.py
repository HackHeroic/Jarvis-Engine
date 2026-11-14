"""Trigger.run, the Template Method every proactive trigger shares.

The skeleton owns the order: idempotency, timing, gathering, consent, storage.
A counting fake trigger proves which steps ran and which never did.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain.anticipation import ConsentContext, ConsentPolicy, TriggerContext
from app.domain.entities import (
    AutonomyMode,
    ConsentReasonKind,
    SuggestionStatus,
    UserProfile,
)
from app.infrastructure.persistence.factory import RepositoryFactory
from tests.anticipation_fakes import (
    ActionLog,
    CountingTrigger,
    FakeAction,
    FakeLowRiskAction,
    FakeReadOnlyAction,
)

U1 = "u1"
NOW = datetime(2026, 11, 14, 3, 30, tzinfo=timezone.utc)  # 09:00 in India


@pytest.fixture
def suggestions():
    return RepositoryFactory.in_memory().suggestions


def _ctx(*, force: bool = False, now: datetime = NOW) -> TriggerContext:
    profile = UserProfile(user_id=U1)
    local_day = now.astimezone(ZoneInfo(profile.timezone)).date()
    return TriggerContext(user_id=U1, profile=profile, now=now, local_day=local_day, force=force)


def _consent_ctx(
    mode: AutonomyMode = AutonomyMode.ASK_FIRST, dismissed: frozenset[str] = frozenset()
) -> ConsentContext:
    return ConsentContext(autonomy_mode=mode, stated_blocks=[], dismissed_keys=dismissed)


async def _run(trigger, suggestions, *, ctx=None, consent_ctx=None):
    return await trigger.run(
        ctx or _ctx(),
        consent=ConsentPolicy.default(),
        consent_ctx=consent_ctx or _consent_ctx(),
        suggestions=suggestions,
    )


@pytest.mark.asyncio
async def test_trigger_run__already_done__gather_never_called(suggestions):
    trigger = CountingTrigger()
    await _run(trigger, suggestions)
    trigger.calls.clear()

    outcome = await _run(trigger, suggestions, ctx=_ctx(now=NOW + timedelta(hours=5)))

    assert outcome.status == "already_done"
    assert trigger.calls == {}
    assert len(await suggestions.list(U1)) == 1


@pytest.mark.asyncio
async def test_trigger_run__not_due__nothing_stored(suggestions):
    trigger = CountingTrigger(due=False)

    outcome = await _run(trigger, suggestions)

    assert outcome.status == "not_due"
    assert (outcome.suggestion, outcome.decision) == (None, None)
    assert trigger.calls["gather"] == 0
    assert await suggestions.list(U1) == []


@pytest.mark.asyncio
async def test_trigger_run__force__skips_should_fire_not_idempotency(suggestions):
    trigger = CountingTrigger(due=False)

    forced = await _run(trigger, suggestions, ctx=_ctx(force=True))
    again = await _run(trigger, suggestions, ctx=_ctx(force=True))

    assert (forced.status, again.status) == ("created", "already_done")
    assert trigger.calls["should_fire"] == 0
    assert trigger.calls["gather"] == 1
    assert len(await suggestions.list(U1)) == 1


@pytest.mark.asyncio
async def test_trigger_run__nothing_to_say__no_suggestion_and_zero_llm_calls(
    suggestions, recording_llm
):
    trigger = CountingTrigger(gathered=None, ask_model=True)

    outcome = await _run(trigger, suggestions)

    assert outcome.status == "nothing_to_say"
    assert outcome.suggestion is None
    assert trigger.calls["build_suggestion"] == 0
    assert recording_llm.calls == []
    assert await suggestions.list(U1) == []


@pytest.mark.asyncio
async def test_trigger_run__deny__nothing_stored_decision_returned(suggestions):
    log = ActionLog()
    trigger = CountingTrigger(action=FakeAction(log, key="move:deep-work"))

    outcome = await _run(
        trigger, suggestions, consent_ctx=_consent_ctx(dismissed=frozenset({"move:deep-work"}))
    )

    assert outcome.status == "denied"
    assert outcome.suggestion is None
    assert outcome.decision.behavior == "deny"
    assert outcome.decision.kind is ConsentReasonKind.LEARNED_PREFERENCE
    assert log.runs == []
    assert await suggestions.list(U1) == []


@pytest.mark.asyncio
async def test_trigger_run__ask__pending_with_typed_reason(suggestions):
    log = ActionLog()
    action = FakeAction(log, key="move:deep-work", hours=(9, 10))
    trigger = CountingTrigger(action=action)

    outcome = await _run(trigger, suggestions)

    stored = outcome.suggestion
    assert outcome.status == "created"
    assert stored.status is SuggestionStatus.PENDING
    assert stored.consent_behavior == "ask"
    assert stored.consent_kind is ConsentReasonKind.MODE_DECISION
    assert stored.consent_reason == "This changes your schedule, so I'm asking first."
    assert stored.action == action.to_payload()
    assert (stored.trigger, stored.kind, stored.created_at) == ("counting", "nudge", NOW)
    assert log.runs == []  # asking means nothing ran
    assert await suggestions.list(U1) == [stored]


@pytest.mark.asyncio
async def test_trigger_run__allowed_change__executed_once_and_auto_applied(suggestions):
    log = ActionLog()
    trigger = CountingTrigger(action=FakeLowRiskAction(log))

    outcome = await _run(
        trigger, suggestions, consent_ctx=_consent_ctx(AutonomyMode.AUTO_LOW_RISK)
    )

    assert outcome.status == "created"
    assert outcome.suggestion.status is SuggestionStatus.AUTO_APPLIED
    assert outcome.suggestion.consent_behavior == "allow"
    assert log.runs == [U1]
    assert await suggestions.list(U1) == [outcome.suggestion]


@pytest.mark.asyncio
async def test_trigger_run__allowed_read_only__pending_and_never_executed(suggestions):
    log = ActionLog()
    trigger = CountingTrigger(action=FakeReadOnlyAction(log))

    outcome = await _run(trigger, suggestions)

    assert outcome.suggestion.status is SuggestionStatus.PENDING
    assert outcome.suggestion.consent_kind is ConsentReasonKind.RISK_CHECK
    assert log.runs == []
