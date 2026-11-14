"""Design conformance of the anticipation package.

Strategies decide (a pattern detector says whether its patterns become rules), one
table rules every suggestion status change, the registry of triggers is the
container's, background runs share one single-flight helper, and no module reaches
into another's private names.
"""

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

from app.domain import errors
from app.domain.anticipation import ConsentPolicy
from app.domain.entities import SUGGESTION_TRANSITIONS, Pattern, SuggestionStatus
from app.domain.events import EventBus, PatternDetected
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.anticipation.actions import build_action_factory
from app.services.anticipation.service import ActionFactory, AnticipationService
from app.services.anticipation.triggers import AnticipationListener, PatternRuleTrigger
from app.services.background import PerUserSingleFlight
from app.services.draft_service import DraftService
from app.services.memory.memory_service import MemoryService
from app.services.memory.patterns import PatternDetector, PatternService
from tests.anticipation_fakes import ActionLog, CountingTrigger, FakeAction
from tests.fakes import BagOfWordsEmbedder

_ANTICIPATION = Path(__file__).resolve().parent.parent / "app" / "services" / "anticipation"
U = "u1"
NOW = datetime(2026, 11, 16, 1, 35, tzinfo=timezone.utc)  # 07:05 in India
S = SuggestionStatus


# --- no private reach ---------------------------------------------------------------------


def test_anticipation__imports_no_private_name_from_another_module():
    offenders = []
    for path in sorted(_ANTICIPATION.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                offenders += [
                    f"{path.name}: from {node.module} import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("_")
                ]
    assert offenders == []


# --- one transition table for suggestions -------------------------------------------------


def test_suggestion_transitions__table__the_legal_moves():
    assert SUGGESTION_TRANSITIONS == {
        S.PENDING: frozenset({S.ACCEPTED, S.DISMISSED, S.EXPIRED}),
        S.ACCEPTED: frozenset({S.PENDING, S.EXPIRED, S.AUTO_APPLIED}),
        S.DISMISSED: frozenset(),
        S.EXPIRED: frozenset(),
        S.AUTO_APPLIED: frozenset(),
    }


@pytest.mark.asyncio
async def test_suggestion_move__not_in_the_table__invalid_state_transition_and_unchanged(
    monkeypatch,
):
    """Every status change goes through the table: a move it forbids never reaches the
    store, whoever asks for it."""
    repos = RepositoryFactory.in_memory()
    log = ActionLog()
    actions = ActionFactory()
    actions.register(FakeAction.kind, FakeAction.builder(log))
    trigger = CountingTrigger(action=FakeAction(log))
    service = AnticipationService(
        {trigger.name: trigger}, actions, ConsentPolicy.default(), repos.suggestions,
        repos.profiles, repos.constraints, clock=lambda: NOW,
    )
    made = (await service.run_trigger(U, trigger.name, force=True)).suggestion
    monkeypatch.setitem(SUGGESTION_TRANSITIONS, S.PENDING, frozenset({S.ACCEPTED, S.EXPIRED}))

    with pytest.raises(errors.InvalidStateTransition):
        await service.dismiss(U, made.id)

    assert (await repos.suggestions.get(U, made.id)).status is S.PENDING


# --- the trigger registry -----------------------------------------------------------------


def test_anticipation__triggers__the_registered_names_and_descriptions():
    class _Evening(CountingTrigger):
        name: ClassVar[str] = "evening_review"
        description: ClassVar[str] = "How the day went, before bed."

    repos = RepositoryFactory.in_memory()
    service = AnticipationService(
        {}, ActionFactory(), ConsentPolicy.default(), repos.suggestions, repos.profiles,
        repos.constraints,
    )
    service.register_trigger(_Evening())

    assert service.triggers() == [("evening_review", "How the day went, before bed.")]


# --- a detector says whether its patterns become rules ------------------------------------


class _LateNightDetector(PatternDetector):
    """A detector the triggers have never heard of, whose patterns may become rules."""

    name: ClassVar[str] = "late_night_skip"
    proposes_rule: ClassVar[bool] = True

    def detect(self, events, zone: ZoneInfo) -> list[Pattern]:
        return [Pattern(
            detector=self.name, description="You tend to set work aside around 11 PM",
            occurrences=4, confidence=1.0, pattern_key=f"{self.name}:23", hour=23,
            last_observed_at=NOW,
        )]

    def insight(self, pattern: Pattern) -> str:
        return "Late nights rarely go to plan."


class _NoPlan:
    async def replan(self, user_id, *, reslot_task_ids):
        return None


@pytest.mark.asyncio
async def test_pattern_rule__new_detector_that_proposes_rules__offered_without_a_trigger_edit():
    repos = RepositoryFactory.in_memory()
    bus = EventBus()
    patterns = PatternService(
        repos.task_events, repos.memories, repos.profiles, bus,
        detectors=[_LateNightDetector()], clock=lambda: NOW,
    )
    actions = build_action_factory(
        drafts=DraftService(repos.drafts, bus, tasks=repos.tasks),
        constraints=repos.constraints,
        memory=MemoryService(repos.memories, BagOfWordsEmbedder()),
        planner=_NoPlan(),
    )
    rule = PatternRuleTrigger(patterns, repos.suggestions, actions, repos.constraints)
    service = AnticipationService(
        {rule.name: rule}, actions, ConsentPolicy.default(), repos.suggestions,
        repos.profiles, repos.constraints, clock=lambda: NOW,
    )
    listener = AnticipationListener(service, patterns)
    bus.subscribe(PatternDetected, listener)

    await patterns.observe(U)  # remembered, announced: the listener runs the rule trigger
    await listener.settle()

    (offer,) = await repos.suggestions.list(U, status=None)
    assert offer.action["pattern_key"] == "late_night_skip:23"
    assert offer.title == "A pattern around 11 PM"


class _WordyDetector(_LateNightDetector):
    """The same detector, with its own words for the brief's line."""

    def noticed(self, pattern: Pattern) -> str:
        return "I noticed late nights rarely go to plan."


@pytest.mark.asyncio
async def test_brief__noticed_line__in_the_detectors_own_words():
    """The brief names no detector: the line about a pattern is its detector's."""
    from app.services.anticipation.triggers import MorningBriefTrigger
    from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator

    repos = RepositoryFactory.in_memory()
    bus = EventBus()
    patterns = PatternService(
        repos.task_events, repos.memories, repos.profiles, bus,
        detectors=[_WordyDetector()], clock=lambda: NOW,
    )
    await patterns.observe(U)
    actions = build_action_factory(
        drafts=DraftService(repos.drafts, bus, tasks=repos.tasks),
        constraints=repos.constraints,
        memory=MemoryService(repos.memories, BagOfWordsEmbedder()),
        planner=_NoPlan(),
    )
    brief = MorningBriefTrigger(
        repos.tasks, repos.task_events, _NoPlan(),
        HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator()), patterns,
        actions, suggestions=repos.suggestions,
    )
    service = AnticipationService(
        {brief.name: brief}, actions, ConsentPolicy.default(), repos.suggestions,
        repos.profiles, repos.constraints, clock=lambda: NOW,
    )

    outcome = await service.run_trigger(U, brief.name, force=True, now=NOW)

    assert outcome.suggestion.body == "I noticed late nights rarely go to plan."


# --- one single-flight helper -------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_flight__requests_during_a_run__one_more_run_per_user():
    runs: list[str] = []
    gate = asyncio.Event()

    async def run(user_id: str) -> None:
        runs.append(user_id)
        await gate.wait()

    flight = PerUserSingleFlight(run, label="Test run")
    flight.request("a")
    flight.request("b")
    await asyncio.sleep(0)
    for _ in range(3):  # three more requests while a's run is in flight
        flight.request("a")
    gate.set()
    await flight.settle()

    assert sorted(runs) == ["a", "a", "b"]


@pytest.mark.asyncio
async def test_single_flight__a_run_raises__logged_by_type_and_the_next_request_runs(caplog):
    calls = []

    async def run(user_id: str) -> None:
        calls.append(user_id)
        if len(calls) == 1:
            raise RuntimeError("secret connection string")

    flight = PerUserSingleFlight(run, label="Test run")
    flight.request("a")
    await flight.settle()
    flight.request("a")
    await flight.settle()

    assert calls == ["a", "a"]
    (record,) = [r for r in caplog.records if "Test run" in r.getMessage()]
    assert "RuntimeError" in record.getMessage() and "secret" not in record.getMessage()


@pytest.mark.asyncio
async def test_single_flight__shutdown__takes_no_more_and_cancels_after_the_grace():
    started = asyncio.Event()

    async def run(user_id: str) -> None:
        started.set()
        await asyncio.sleep(60)

    flight = PerUserSingleFlight(run, label="Test run")
    flight.request("a")
    await started.wait()

    await asyncio.wait_for(flight.shutdown(grace_s=0.01), 1.0)
    flight.request("b")

    assert flight.closed
    await flight.settle()  # nothing in flight, nothing new started


# --- a cached habit translation never waits for the PRIMARY lock ---------------------------


@pytest.mark.asyncio
async def test_translate_habits_step__translation_cached__never_waits_for_the_primary_lock(
    monkeypatch,
):
    """While a planning turn holds the heavy model, a brief whose habits are already
    translated reads them from the cache at once: only a real translation takes the lock."""
    import app.core.config as config
    from app.core.model_router import hold_primary_model
    from app.infrastructure.llm.chain import FallbackChainProvider
    from app.infrastructure.llm.factory import set_chain
    from app.modules.planning_graph import planning_module
    from app.schemas.context import SemanticTimeSlot, SemanticTimeSlotsResponse
    from app.services.analytical import habit_translator
    from tests.anticipation_fakes import RecordingModel

    monkeypatch.setattr(config, "GEMINI_PRIMARY", False)  # the local model: the lock applies
    monkeypatch.setattr(config, "CLAUDE_PRIMARY", False)
    habit_translator.invalidate_habit_cache()
    habits = "Choir practice with the college band on Tuesdays"
    step = next(s for s in planning_module.steps if s.name == "translate_habits")
    assert step.holds_primary({"habits_text": habits}) is True  # not cached: it translates

    choir = SemanticTimeSlot(name="choir", start_min=600, end_min=720, availability="blocked",
                             recurrence="weekly", weekday=1)

    class _Translator(RecordingModel):
        async def complete(self, req):
            self.calls.append(req)
            return SemanticTimeSlotsResponse(semantic_slots=[choir])

    model = _Translator()
    set_chain(FallbackChainProvider([model]))
    await habit_translator.translate_habits_to_slots(habits)  # translated once: cached
    assert step.holds_primary({"habits_text": habits}) is False

    held, release = asyncio.Event(), asyncio.Event()

    async def planning_turn() -> None:
        async with hold_primary_model():
            held.set()
            await release.wait()

    turn = asyncio.create_task(planning_turn())
    await held.wait()
    try:
        result = await asyncio.wait_for(step.handler({"habits_text": habits}), 1.0)
    finally:
        release.set()
        await turn
    assert result["habits_translated"] is True
    assert len(model.calls) == 1  # the cache answered; no second translation
