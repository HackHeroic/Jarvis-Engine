"""AnticipationService: runs triggers under consent, and accepts or dismisses suggestions.

Accept is claim-then-execute: the suggestion is claimed with a compare-and-set
before its action runs, so a double-click runs the action once.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar
from uuid import uuid4

import pytest

from app.domain import errors
from app.domain.anticipation import ConsentPolicy
from app.domain.entities import (
    AutonomyMode,
    BehavioralConstraint,
    ConsentReasonKind,
    SuggestionStatus,
)
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.anticipation.service import ActionFactory, AnticipationService
from tests.anticipation_fakes import (
    ActionLog,
    CountingTrigger,
    FakeAction,
    FakeLowRiskAction,
    until_started,
)

U1, U2 = "u1", "u2"
NOW = datetime(2026, 11, 14, 3, 30, tzinfo=timezone.utc)  # 09:00 in India


@pytest.fixture
def repos():
    return RepositoryFactory.in_memory()


@pytest.fixture
def log() -> ActionLog:
    return ActionLog()


def _service(
    repos, log: ActionLog, *triggers, actions: ActionFactory | None = None
) -> AnticipationService:
    if actions is None:
        actions = ActionFactory()
        actions.register(FakeAction.kind, FakeAction.builder(log))
        actions.register(FakeLowRiskAction.kind, FakeLowRiskAction.builder(log))
    return AnticipationService(
        {t.name: t for t in triggers},
        actions,
        ConsentPolicy.default(),
        repos.suggestions,
        repos.profiles,
        repos.constraints,
        clock=lambda: NOW,
    )


class _SecondTrigger(CountingTrigger):
    name: ClassVar[str] = "second"


class _CountingCalls:
    """Wraps a repository and counts each call made to it, by method name."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._inner, name)

        async def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            return await method(*args, **kwargs)

        return counted


async def _pending(service: AnticipationService, user_id: str = U1) -> str:
    outcome = await service.run_trigger(user_id, CountingTrigger.name, force=True)
    assert outcome.suggestion.status is SuggestionStatus.PENDING
    return outcome.suggestion.id


# --- accept ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept__pending__claims_executes_accepted(repos, log):
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)

    suggestion, result = await service.accept(U1, suggestion_id)

    assert suggestion.status is SuggestionStatus.ACCEPTED
    assert (result.kind, result.draft_id) == ("fake_action", "draft-1")
    assert log.runs == [U1]
    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.ACCEPTED


@pytest.mark.asyncio
async def test_accept__double_click__second_409_and_action_executed_once(repos, log):
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)

    results = await asyncio.gather(
        service.accept(U1, suggestion_id), service.accept(U1, suggestion_id),
        return_exceptions=True,
    )
    late = await asyncio.gather(service.accept(U1, suggestion_id), return_exceptions=True)

    refused = [r for r in [*results, *late] if isinstance(r, Exception)]
    assert len(refused) == 2
    assert all(isinstance(r, errors.StaleVersionError) for r in refused)
    assert all(r.http_status == 409 for r in refused)
    assert log.runs == [U1]
    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.ACCEPTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "out_of_date",
    [
        errors.InvalidStateTransition("This draft is superseded and cannot become accepted."),
        errors.NotFoundError("That draft does not exist."),
    ],
    ids=["superseded_draft", "draft_gone"],
)
async def test_accept__action_out_of_date__expired_and_409_plain_message(repos, log, out_of_date):
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)
    log.errors.append(out_of_date)

    with pytest.raises(errors.InvalidStateTransition) as raised:
        await service.accept(U1, suggestion_id)

    assert raised.value.http_status == 409
    assert raised.value.message == "This suggestion is out of date — a newer plan replaced it."
    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.EXPIRED
    assert log.runs == []


@pytest.mark.asyncio
async def test_accept__store_down__back_to_pending_and_503(repos, log):
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)
    log.errors.append(errors.PersistenceError("Could not reach the database."))

    with pytest.raises(errors.PersistenceError) as raised:
        await service.accept(U1, suggestion_id)

    assert raised.value.http_status == 503
    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.PENDING
    # Nothing ran, so the same suggestion can be accepted once the store is back.
    suggestion, _ = await service.accept(U1, suggestion_id)
    assert suggestion.status is SuggestionStatus.ACCEPTED
    assert log.runs == [U1]


@pytest.mark.asyncio
async def test_accept__other_users_suggestion__404(repos, log):
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service, U1)

    with pytest.raises(errors.NotFoundError) as raised:
        await service.accept(U2, suggestion_id)

    assert raised.value.http_status == 404
    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.PENDING
    assert log.runs == []


@pytest.mark.asyncio
async def test_accept__unknown_action_kind__validation_error_and_still_pending(repos, log):
    # Nothing registered with the factory: the stored action's kind is unknown to it.
    service = _service(repos, log, actions=ActionFactory())
    service.register_trigger(CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)

    with pytest.raises(errors.ValidationError):
        await service.accept(U1, suggestion_id)

    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.PENDING


# --- dismiss -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss__then_same_key__denied_next_time(repos, log):
    trigger = CountingTrigger(action=FakeAction(log, key="move:deep-work"))
    service = _service(repos, log, trigger)
    suggestion_id = await _pending(service)

    dismissed = await service.dismiss(U1, suggestion_id)
    tomorrow = await service.run_trigger(
        U1, trigger.name, force=True, now=NOW + timedelta(days=1)
    )

    assert dismissed.status is SuggestionStatus.DISMISSED
    assert tomorrow.status == "denied"
    assert tomorrow.decision.kind is ConsentReasonKind.LEARNED_PREFERENCE
    assert await service.list_suggestions(U1) == []


@pytest.mark.asyncio
async def test_dismiss__not_pending__409(repos, log):
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)
    await service.dismiss(U1, suggestion_id)

    with pytest.raises(errors.StaleVersionError):
        await service.dismiss(U1, suggestion_id)


# --- running triggers ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trigger__said_rule_covers_the_hours__denied_quoting_the_user(repos, log):
    for source, text in (("said", "No work after 10 PM"), ("inferred", "Avoids 9 AM to 11 AM")):
        await repos.constraints.add(
            U1,
            BehavioralConstraint(
                id=str(uuid4()), user_id=U1, raw_text=text, constraint_type="rule", source=source
            ),
        )
    late = CountingTrigger(action=FakeAction(log, key="late", hours=(22, 23)))
    service = _service(repos, log, late)

    denied = await service.run_trigger(U1, late.name, force=True)
    late.action = FakeAction(log, key="morning", hours=(9, 10))  # only an inferred rule covers it
    asked = await service.run_trigger(U1, late.name, force=True, now=NOW + timedelta(days=1))

    assert denied.status == "denied"
    assert denied.decision.kind is ConsentReasonKind.RULE_MATCH
    assert denied.decision.message == 'You already told me: "No work after 10 PM".'
    assert (asked.status, asked.decision.behavior) == ("created", "ask")


@pytest.mark.asyncio
async def test_run_trigger__unknown_name__not_found(repos, log):
    service = _service(repos, log)

    with pytest.raises(errors.NotFoundError):
        await service.run_trigger(U1, "morning_brief")


@pytest.mark.asyncio
async def test_run_due__runs_each_trigger_on_the_users_local_day(repos, log):
    trigger = CountingTrigger(action=FakeAction(log))
    service = _service(repos, log, trigger)
    # 00:30 in India on 15 November, still 14 November in UTC.
    just_after_midnight = datetime(2026, 11, 14, 19, 0, tzinfo=timezone.utc)

    first = await service.run_due(U1, NOW)
    second = await service.run_due(U1, just_after_midnight)

    assert [o.status for o in first] == ["created"]
    assert [o.status for o in second] == ["created"]  # a new local day, so not already done


def test_action_factory__unknown_kind__validation_error():
    with pytest.raises(errors.ValidationError):
        ActionFactory().from_payload({"kind": "teleport", "dedupe_key": "teleport"})


# --- fix round 1: claims are always released, once a day holds, triggers are isolated ----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unlisted",
    [errors.LLMUnavailableError("Every model is down."), RuntimeError("driver text")],
    ids=["llm_unavailable", "unexpected"],
)
async def test_accept__unlisted_error_after_the_claim__back_to_pending_and_raised(
    repos, log, unlisted
):
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)
    log.errors.append(unlisted)

    with pytest.raises(type(unlisted)):
        await service.accept(U1, suggestion_id)

    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.PENDING
    suggestion, _ = await service.accept(U1, suggestion_id)  # it can be accepted again
    assert suggestion.status is SuggestionStatus.ACCEPTED


@pytest.mark.asyncio
async def test_accept__cancelled_while_the_action_runs__back_to_pending(repos):
    log = ActionLog(hold=asyncio.Event())
    service = _service(repos, log, CountingTrigger(action=FakeAction(log)))
    suggestion_id = await _pending(service)

    accepting = asyncio.create_task(service.accept(U1, suggestion_id))
    await until_started(log)
    accepting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await accepting

    assert (await repos.suggestions.get(U1, suggestion_id)).status is SuggestionStatus.PENDING
    assert log.runs == []


@pytest.mark.asyncio
async def test_accept__past_expires_at__expired_and_409_never_listed(repos, log):
    stale = CountingTrigger(action=FakeAction(log), expires_at=NOW - timedelta(minutes=1))
    service = _service(repos, log, stale)
    outcome = await service.run_trigger(U1, stale.name, force=True)

    listed = await service.list_suggestions(U1)
    with pytest.raises(errors.InvalidStateTransition) as raised:
        await service.accept(U1, outcome.suggestion.id)

    assert listed == []
    assert raised.value.message == "This suggestion is out of date — a newer plan replaced it."
    stored = await repos.suggestions.get(U1, outcome.suggestion.id)
    assert stored.status is SuggestionStatus.EXPIRED
    assert log.runs == []


@pytest.mark.asyncio
async def test_run_trigger__concurrent_runs__one_suggestion(repos, log):
    trigger = CountingTrigger(action=FakeAction(log))
    service = _service(repos, log, trigger)

    outcomes = await asyncio.gather(
        *(service.run_trigger(U1, trigger.name, force=True) for _ in range(3)),
        service.run_due(U1, NOW),
    )

    every = [*outcomes[:3], *outcomes[3]]
    assert "created" in [o.status for o in every]
    assert len({o.suggestion.id for o in every if o.suggestion is not None}) == 1
    assert len(await repos.suggestions.list(U1, status=None)) == 1
    assert trigger.calls["gather"] == 1


@pytest.mark.asyncio
async def test_run_trigger__tick_settled_nothing_to_say__manual_run_re_evaluates(repos, log):
    trigger = CountingTrigger(action=FakeAction(log), gathered=None)
    service = _service(repos, log, trigger)
    (settled,) = await service.run_due(U1, NOW)
    trigger.gathered = "Your history now shows two hard tasks in low-focus hours."  # seeded

    (still_settled,) = await service.run_due(U1, NOW + timedelta(minutes=1))
    manual = await service.run_trigger(U1, trigger.name, force=True, now=NOW + timedelta(minutes=2))
    (after,) = await service.run_due(U1, NOW + timedelta(minutes=3))

    assert (settled.status, still_settled.status) == ("nothing_to_say", "nothing_to_say")
    assert manual.status == "created"  # a manual run never trusts the day memory
    assert after.status == "already_done"  # and what it made updates that memory
    assert trigger.calls["gather"] == 2
    assert len(await repos.suggestions.list(U1, status=None)) == 1


@pytest.mark.asyncio
async def test_run_trigger__two_manual_runs_inside_the_cooldown__evaluated_once(repos, log):
    trigger = CountingTrigger(action=FakeAction(log), gathered=None)
    service = _service(repos, log, trigger)

    first = await service.run_trigger(U1, trigger.name, force=True, now=NOW)
    second = await service.run_trigger(
        U1, trigger.name, force=True, now=NOW + timedelta(seconds=5)
    )
    gathered_inside = trigger.calls["gather"]
    third = await service.run_trigger(
        U1, trigger.name, force=True, now=NOW + timedelta(seconds=11)
    )

    assert first.status == second.status == third.status == "nothing_to_say"
    assert second is first  # the previous outcome, not a new evaluation
    assert gathered_inside == 1
    assert trigger.calls["gather"] == 2  # past the cooldown it evaluates again


@pytest.mark.asyncio
async def test_run_due__denied__gathered_once_until_the_next_local_day(repos, log):
    trigger = CountingTrigger(action=FakeAction(log, key="move:deep-work"))
    service = _service(repos, log, trigger)
    suggestion_id = await _pending(service)
    await service.dismiss(U1, suggestion_id)
    tomorrow = NOW + timedelta(days=1)
    trigger.calls.clear()

    outcomes = [await service.run_due(U1, tomorrow + timedelta(minutes=m)) for m in range(5)]

    assert [o.status for (o,) in outcomes] == ["denied"] * 5
    assert trigger.calls["gather"] == 1
    assert trigger.calls["build_suggestion"] == 1
    await service.run_due(U1, tomorrow + timedelta(days=1))
    assert trigger.calls["gather"] == 2  # a new local day asks again


@pytest.mark.asyncio
async def test_run_due__one_trigger_raises__the_next_still_runs(repos, log, caplog):
    broken = CountingTrigger(action=FakeAction(log), fail_for=frozenset({U1}))
    second = _SecondTrigger(action=FakeAction(log, key="second"))
    service = _service(repos, log, broken, second)

    with caplog.at_level(logging.WARNING, logger="app.services.anticipation.service"):
        outcomes = await service.run_due(U1, NOW)

    assert [o.status for o in outcomes] == ["failed", "created"]
    (record,) = caplog.records
    assert "counting" in record.getMessage() and "RuntimeError" in record.getMessage()
    assert "secret" not in record.getMessage() and record.exc_info is None


@pytest.mark.asyncio
async def test_auto_apply__racing_accept__409_and_action_runs_once(repos):
    log = ActionLog(hold=asyncio.Event())
    trigger = CountingTrigger(action=FakeLowRiskAction(log))
    service = _service(repos, log, trigger)
    await repos.profiles.update(U1, autonomy_mode=AutonomyMode.AUTO_LOW_RISK)

    applying = asyncio.create_task(service.run_trigger(U1, trigger.name, force=True))
    await until_started(log)
    (in_flight,) = await repos.suggestions.list(U1, status=None)
    with pytest.raises(errors.StaleVersionError):
        await asyncio.wait_for(service.accept(U1, in_flight.id), 1.0)
    log.hold.set()
    outcome = await asyncio.wait_for(applying, 2.0)

    assert outcome.suggestion.status is SuggestionStatus.AUTO_APPLIED
    assert log.runs == [U1]


@pytest.mark.asyncio
async def test_run_due__not_due__one_profile_read_and_one_cheap_read_per_trigger(log):
    repos = RepositoryFactory.in_memory()
    profiles, suggestions = _CountingCalls(repos.profiles), _CountingCalls(repos.suggestions)
    constraints = _CountingCalls(repos.constraints)
    service = AnticipationService(
        {t.name: t for t in (CountingTrigger(due=False), _SecondTrigger(due=False))},
        ActionFactory(),
        ConsentPolicy.default(),
        suggestions,
        profiles,
        constraints,
    )

    outcomes = await service.run_due(U1, NOW)

    assert [o.status for o in outcomes] == ["not_due", "not_due"]
    assert profiles.calls == {"get_or_create": 1}
    assert suggestions.calls == {"exists_today": 2}
    assert constraints.calls == {}  # consent is never loaded for a trigger that is not due


@pytest.mark.asyncio
async def test_run_due__settled_for_the_day__no_reads_until_the_next_local_day(log):
    repos = RepositoryFactory.in_memory()
    profiles, suggestions = _CountingCalls(repos.profiles), _CountingCalls(repos.suggestions)
    constraints = _CountingCalls(repos.constraints)
    triggers = (CountingTrigger(action=FakeAction(log)), _SecondTrigger(gathered=None))
    service = AnticipationService(
        {t.name: t for t in triggers},
        ActionFactory(),
        ConsentPolicy.default(),
        suggestions,
        profiles,
        constraints,
    )
    first = await service.run_due(U1, NOW)
    for repo in (profiles, suggestions, constraints):
        repo.calls.clear()

    later = [await service.run_due(U1, NOW + timedelta(hours=h)) for h in (1, 2, 3)]
    reads_today = profiles.calls + suggestions.calls + constraints.calls
    await service.run_due(U1, NOW + timedelta(days=1))

    assert [o.status for o in first] == ["created", "nothing_to_say"]
    for outcomes in later:
        assert [o.status for o in outcomes] == ["already_done", "nothing_to_say"]
    assert reads_today == Counter()
    assert profiles.calls["get_or_create"] == 1  # the next local day reads again
