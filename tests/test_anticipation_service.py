"""AnticipationService: runs triggers under consent, and accepts or dismisses suggestions.

Accept is claim-then-execute: the suggestion is claimed with a compare-and-set
before its action runs, so a double-click runs the action once.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.domain import errors
from app.domain.anticipation import ConsentPolicy
from app.domain.entities import BehavioralConstraint, ConsentReasonKind, SuggestionStatus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.anticipation.service import ActionFactory, AnticipationService
from tests.anticipation_fakes import ActionLog, CountingTrigger, FakeAction

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
    return AnticipationService(
        {t.name: t for t in triggers},
        actions,
        ConsentPolicy.default(),
        repos.suggestions,
        repos.profiles,
        repos.constraints,
        clock=lambda: NOW,
    )


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
