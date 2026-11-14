"""The first move: the morning brief and learned-rule proposals, under consent.

``MorningBriefTrigger`` drafts today's plan at the user's wake time: the pending
tasks, plus the ones whose slot passed since yesterday, each with a new slot, and
one thing Jarvis noticed. ``PatternRuleTrigger`` offers a confirmed habit as a rule.
Both run through the real ``AnticipationService`` over the Supabase repositories on
``FakeSupabase``; the brief drafts with the real planning sub-graph and CP-SAT, or,
where a test pins times, with a planner that places tasks where the test says.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
from app.domain import errors
from app.domain.anticipation import ConsentPolicy
from app.domain.entities import (
    BehavioralConstraint,
    ConsentReasonKind,
    DraftSchedule,
    DraftStatus,
    Pattern,
    SuggestionStatus,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from app.domain.events import EventBus, PatternDetected
from app.domain.ports import DraftPlanner
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.modules.planning_replan import PlanningGraphReplanner
from app.services.anticipation.actions import build_action_factory
from app.services.anticipation.service import AnticipationService
from app.services.anticipation.triggers import (
    AnticipationListener,
    MorningBriefTrigger,
    PatternRuleTrigger,
)
from app.services.draft_service import DraftService
from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator
from app.services.memory.memory_service import MemoryService
from app.services.memory.patterns import PatternService
from app.services.planning.local_time import end_of_local_day
from tests.behaviour.conftest import blame_phrases_in
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

IST = ZoneInfo("Asia/Kolkata")
U = "alice"
MONDAY = date(2026, 11, 16)
TUESDAY = MONDAY + timedelta(days=1)
SUNDAY = MONDAY - timedelta(days=1)


def _ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


MONDAY_7_05 = _ist(MONDAY, 7, 5)  # 01:35 UTC: five minutes after the default 07:00 wake
ASKING_FIRST = "This changes your schedule, so I'm asking first."
COULDNT_FIT = "I couldn't fit everything into today — want to look at it together?"


class _Clock:
    """The instant the services read; tests move it."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _PinnedPlanner(DraftPlanner):
    """Drafts the pending tasks and the re-slotted ones back to back from 09:00 local
    on ``day``, hardest first, through the real draft repository. Records every call."""

    def __init__(self, repos: Repositories, day: date = MONDAY, *, raises: Exception | None = None):
        self._repos = repos
        self._day = day
        self._raises = raises
        self.calls: list[list[str]] = []

    async def replan(self, user_id: str, *, reslot_task_ids: list[str]) -> DraftSchedule | None:
        self.calls.append(list(reslot_task_ids))
        if self._raises is not None:
            raise self._raises
        tasks = await self._repos.tasks.list_by_status(user_id, frozenset({TaskStatus.PENDING}))
        missed = await self._repos.tasks.list_by_status(user_id, frozenset({TaskStatus.MISSED}))
        tasks += [t for t in missed if t.task_id in reslot_task_ids]
        if not tasks:
            return None
        tasks.sort(key=lambda t: (-t.difficulty_weight, t.task_id))
        chunks, schedule, minute = [], {}, 60  # minute 0 is 08:00
        for task in tasks:
            chunks.append({
                "task_id": task.task_id, "title": task.title, "goal_id": task.goal_id,
                "duration_minutes": task.duration_minutes,
                "difficulty_weight": task.difficulty_weight,
            })
            schedule[task.task_id] = {"start_min": minute, "end_min": minute + 25}
            minute += 30
        return await self._repos.drafts.create(
            user_id, tasks=chunks, horizon_start=_ist(self._day, 8).isoformat(), goal_id=None,
            schedule=schedule,
        )


@dataclass
class _World:
    repos: Repositories
    bus: EventBus
    clock: _Clock
    patterns: PatternService
    drafts: DraftService
    service: AnticipationService
    listener: AnticipationListener


def _world(fake: FakeSupabase, repos: Repositories, planner: DraftPlanner | None = None) -> _World:
    clock = _Clock(MONDAY_7_05)
    bus = EventBus()
    planner = planner or PlanningGraphReplanner(
        repos.drafts, db_provider=lambda: FakeDBClient(fake), memory_provider=lambda: None
    )
    patterns = PatternService(repos.task_events, repos.memories, repos.profiles, bus, clock=clock)
    energy = HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator())
    drafts = DraftService(repos.drafts, bus, task_events=repos.task_events, clock=clock)
    memory = MemoryService(repos.memories, BagOfWordsEmbedder())
    actions = build_action_factory(
        drafts=drafts, constraints=repos.constraints, memory=memory, planner=planner
    )
    brief = MorningBriefTrigger(repos.tasks, repos.task_events, planner, energy, patterns, actions)
    rule = PatternRuleTrigger(patterns, repos.suggestions, actions)
    service = AnticipationService(
        {brief.name: brief, rule.name: rule}, actions, ConsentPolicy.default(),
        repos.suggestions, repos.profiles, repos.constraints, clock=clock,
    )
    return _World(repos, bus, clock, patterns, drafts, service, AnticipationListener(service))


@pytest.fixture
def fake() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def repos(fake) -> Repositories:
    return RepositoryFactory.supabase(fake)


@pytest.fixture
def world(fake, repos) -> _World:
    """Drafts with the real planner (freeze the schedule endpoint's clock to the run's)."""
    return _world(fake, repos)


@pytest.fixture
def pinned(repos) -> _PinnedPlanner:
    return _PinnedPlanner(repos)


@pytest.fixture
def pinned_world(fake, repos, pinned) -> _World:
    return _world(fake, repos, pinned)


# --- seeding --------------------------------------------------------------------------


def _row(task_id: str, *, status: str = "pending", start: datetime | None = None, **extra) -> dict:
    row = {
        "task_id": task_id, "title": f"Revise {task_id}", "goal_id": "dsa", "status": status,
        "duration_minutes": 25, "difficulty_weight": 0.5, "dependencies": [],
        "deadline_hint": None, **extra,
    }
    if start is not None:
        row["scheduled_start"] = start.isoformat()
        row["scheduled_end"] = (start + timedelta(minutes=25)).isoformat()
    return row


async def _seed_tasks(repos: Repositories, rows: list[dict], user_id: str = U) -> None:
    """Write tasks the only way they are ever written: by accepting a draft."""
    draft = await repos.drafts.create(
        user_id, tasks=[{"task_id": "seed", "title": "seed"}], horizon_start=None,
        goal_id=None, schedule=None,
    )
    await repos.drafts.accept_atomic(user_id, draft.id, rows)


def _outcome(task_id: str, kind: TaskEventType, slot: datetime | None, at: datetime) -> TaskEvent:
    return TaskEvent(
        user_id=U, task_id=task_id, event_type=kind, occurred_at=at, scheduled_start=slot,
        scheduled_end=slot + timedelta(minutes=25) if slot else None,
        metadata={"title": f"Revise {task_id}"},
    )


async def _set_aside_at(world: _World, hour: int, times: int = 3) -> None:
    """``times`` slots at ``hour`` local on the days before Monday, each set aside, and
    PEARL has looked at them: the pattern is remembered."""
    events = []
    for day in range(1, times + 1):
        slot = _ist(MONDAY - timedelta(days=day), hour)
        events.append(_outcome(f"past_{hour}_{day}", TaskEventType.SKIPPED, slot, slot))
    await world.repos.task_events.append_many(U, events)
    await world.patterns.observe(U)


async def _run_brief(world: _World, at: datetime, *, force: bool = False):
    return await world.service.run_trigger(U, "morning_brief", force=force, now=at)


def _no_blame(text: str) -> None:
    assert blame_phrases_in(text) == [], text
    for word in ("missed", "behind", "failed"):
        assert word not in text.lower(), text


# --- MorningBrief: when ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_brief__before_wake__not_due(pinned_world, pinned):
    await _seed_tasks(pinned_world.repos, [_row("graphs")])

    early = await _run_brief(pinned_world, _ist(MONDAY, 6, 55))
    late = await _run_brief(pinned_world, _ist(MONDAY, 13, 0))  # the window is wake + 6 h

    assert (early.status, late.status) == ("not_due", "not_due")
    assert pinned.calls == []
    assert await pinned_world.repos.suggestions.list(U) == []


@pytest.mark.asyncio
async def test_brief__after_wake__pending_suggestion_with_draft_and_ask_reason(world, clock):
    clock.freeze(MONDAY_7_05, schedule_endpoint)
    await _seed_tasks(world.repos, [_row("graphs"), _row("trees", difficulty_weight=0.8)])

    outcome = await _run_brief(world, MONDAY_7_05)

    suggestion = outcome.suggestion
    assert outcome.status == "created"
    assert (suggestion.status, suggestion.kind, suggestion.title) == (
        SuggestionStatus.PENDING, "morning_brief", "Today is ready"
    )
    assert suggestion.action["kind"] == "apply_plan"
    draft = await world.repos.drafts.get(U, suggestion.action["draft_id"])
    assert draft.status is DraftStatus.PENDING
    assert {t["task_id"] for t in draft.tasks} == {"graphs", "trees"}
    assert (suggestion.consent_behavior, suggestion.consent_kind, suggestion.consent_reason) == (
        "ask", ConsentReasonKind.MODE_DECISION, ASKING_FIRST
    )
    # Pacing spreads a small plan over two days, so today holds one or both of them.
    assert re.fullmatch(
        r"[12] tasks? (is|are) planned for today; the hardest, 'Revise \w+', is at \d\d:\d\d\.",
        suggestion.body,
    ), suggestion.body
    assert suggestion.expires_at == end_of_local_day(MONDAY, IST)


@pytest.mark.asyncio
async def test_brief__second_run_same_local_day__already_done(pinned_world, pinned):
    await _seed_tasks(pinned_world.repos, [_row("graphs")])

    first = await _run_brief(pinned_world, MONDAY_7_05)
    again = await _run_brief(pinned_world, _ist(MONDAY, 9, 0))
    forced = await _run_brief(pinned_world, _ist(MONDAY, 9, 30), force=True)  # skips the memo

    assert (first.status, again.status, forced.status) == ("created", "already_done", "already_done")
    assert len(pinned.calls) == 1
    assert len(await pinned_world.repos.suggestions.list(U, status=None)) == 1


@pytest.mark.asyncio
async def test_brief__utc_midnight_crossing__fires_once_per_local_day(fake, repos, pinned):
    await _seed_tasks(repos, [_row("graphs")])

    # A fresh service per run: only what was stored can say "already done".
    monday = await _run_brief(_world(fake, repos, pinned), MONDAY_7_05)  # 01:35 UTC Monday
    later = await _run_brief(_world(fake, repos, pinned), _ist(MONDAY, 11, 0))  # 05:30 UTC
    tuesday = await _run_brief(_world(fake, repos, pinned), _ist(TUESDAY, 7, 5))  # 01:35 UTC

    assert (monday.status, later.status, tuesday.status) == ("created", "already_done", "created")
    briefs = await repos.suggestions.list(U, status=None)
    assert len(briefs) == 2
    assert {s.expires_at for s in briefs} == {
        end_of_local_day(MONDAY, IST), end_of_local_day(TUESDAY, IST)
    }


@pytest.mark.asyncio
async def test_brief__forced_at_00_30_ist__counts_against_the_new_local_day(
    fake, repos, pinned
):
    await _seed_tasks(repos, [_row("graphs")])
    monday = await _run_brief(_world(fake, repos, pinned), MONDAY_7_05)

    # 00:30 Tuesday in India is 19:00 Monday in UTC: still Monday's UTC date.
    half_past_midnight = await _run_brief(
        _world(fake, repos, pinned), _ist(TUESDAY, 0, 30), force=True
    )
    tuesday_morning = await _run_brief(_world(fake, repos, pinned), _ist(TUESDAY, 7, 5))

    assert (monday.status, half_past_midnight.status, tuesday_morning.status) == (
        "created", "created", "already_done"
    )
    assert half_past_midnight.suggestion.expires_at == end_of_local_day(TUESDAY, IST)


# --- MorningBrief: what it says --------------------------------------------------------


@pytest.mark.asyncio
async def test_brief__nothing_to_say__no_suggestion_and_zero_llm_calls(world, fake, recording_llm):
    outcome = await _run_brief(world, MONDAY_7_05, force=True)

    assert outcome.status == "nothing_to_say"
    assert await world.repos.suggestions.list(U, status=None) == []
    assert fake.rows.get("draft_schedules", []) == []
    assert recording_llm.calls == []


@pytest.mark.asyncio
async def test_brief__yesterdays_slot_misses__reslot_ids_passed_and_deadline_misses_excluded(
    pinned_world, pinned
):
    yesterday_10 = _ist(SUNDAY, 10)
    await _seed_tasks(pinned_world.repos, [
        _row("pending"),
        _row("slot_yesterday", status="missed", start=yesterday_10),
        _row("deadline_yesterday", status="missed", start=yesterday_10),
        _row("slot_last_week", status="missed", start=yesterday_10 - timedelta(days=5)),
        _row("slot_then_deadline", status="missed", start=yesterday_10),
    ])
    passed = yesterday_10 + timedelta(minutes=55)  # the sweep's grace after the slot
    await pinned_world.repos.task_events.append_many(U, [
        _outcome("slot_yesterday", TaskEventType.MISSED, yesterday_10, passed),
        _outcome("deadline_yesterday", TaskEventType.DEADLINE_MISSED, None, passed),
        _outcome("slot_last_week", TaskEventType.MISSED, yesterday_10 - timedelta(days=5),
                 passed - timedelta(days=5)),
        _outcome("slot_then_deadline", TaskEventType.MISSED, yesterday_10, passed),
        _outcome("slot_then_deadline", TaskEventType.DEADLINE_MISSED, None, _ist(MONDAY, 0, 1)),
    ])

    outcome = await _run_brief(pinned_world, MONDAY_7_05)

    assert outcome.status == "created"
    assert pinned.calls == [["slot_yesterday"]]
    assert "1 from yesterday found a new slot." in outcome.suggestion.body.splitlines()


@pytest.mark.asyncio
async def test_brief__copy__no_blame_phrases_and_peak_claim_only_when_learned(
    pinned_world, pinned
):
    # Bob is new: his focus profile is still the prior, which never makes a claim.
    await _seed_tasks(pinned_world.repos, [_row("intro", difficulty_weight=0.7)], user_id="bob")
    # Alice: ten slots done at 09:00 (a learned profile peaking there), three set aside at
    # 14:00 (a pattern), a hard task, an easy one, and a slot that passed yesterday.
    yesterday_10 = _ist(SUNDAY, 10)
    await _seed_tasks(pinned_world.repos, [
        _row("graphs", difficulty_weight=0.9),
        _row("notes", difficulty_weight=0.2),
        _row("dp", status="missed", start=yesterday_10),
    ])
    done = [
        _outcome(f"done_{d}", TaskEventType.COMPLETED, _ist(MONDAY - timedelta(days=d), 9),
                 _ist(MONDAY - timedelta(days=d), 9, 25))
        for d in range(1, 11)
    ]
    passed = yesterday_10 + timedelta(minutes=55)
    await pinned_world.repos.task_events.append_many(
        U, [*done, _outcome("dp", TaskEventType.MISSED, yesterday_10, passed)]
    )
    await _set_aside_at(pinned_world, 14)

    alice = (await _run_brief(pinned_world, MONDAY_7_05)).suggestion
    bob = (await pinned_world.service.run_trigger("bob", "morning_brief", now=MONDAY_7_05)).suggestion

    assert alice.body.splitlines() == [
        "3 tasks are planned for today; the hardest, 'Revise graphs', is at 09:00"
        " — your strongest hour.",
        "1 from yesterday found a new slot.",
        "I noticed: I keep demanding work out of the 2 PM hour — you've set aside 3 of 3 there"
        " (seen 3 times).",
    ]
    assert bob.body == "1 task is planned for today; the hardest, 'Revise intro', is at 09:00."
    for suggestion in (alice, bob):
        _no_blame(suggestion.title)
        _no_blame(suggestion.body)


@pytest.mark.asyncio
async def test_brief__replan_infeasible__read_only_suggestion_no_draft(fake, repos):
    planner = _PinnedPlanner(repos, raises=errors.ScheduleInfeasibleError("Too much for 30 days."))
    world = _world(fake, repos, planner)
    await _seed_tasks(repos, [_row("graphs")])

    outcome = await _run_brief(world, MONDAY_7_05)

    suggestion = outcome.suggestion
    assert outcome.status == "created"
    assert suggestion.status is SuggestionStatus.PENDING
    assert suggestion.body == COULDNT_FIT
    assert suggestion.action["kind"] == "share_insight"
    assert "draft_id" not in suggestion.action
    assert (suggestion.consent_behavior, suggestion.consent_kind) == (
        "allow", ConsentReasonKind.RISK_CHECK
    )
    assert fake.rows.get("draft_schedules", [])[1:] == []  # only the seeding draft
    _no_blame(suggestion.title)


# --- MorningBrief: accepting it ---------------------------------------------------------


@pytest.mark.asyncio
async def test_brief__accept__tasks_written_and_rescheduled_events(world, clock):
    clock.freeze(MONDAY_7_05, schedule_endpoint)
    yesterday_10 = _ist(SUNDAY, 10)
    await _seed_tasks(world.repos, [
        _row("graphs"), _row("trees"), _row("dp", status="missed", start=yesterday_10),
    ])
    await world.repos.task_events.append_many(U, [
        _outcome("dp", TaskEventType.SCHEDULED, yesterday_10, yesterday_10 - timedelta(days=1)),
        _outcome("dp", TaskEventType.MISSED, yesterday_10, yesterday_10 + timedelta(minutes=55)),
    ])
    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion

    accepted, result = await world.service.accept(U, suggestion.id)

    assert accepted.status is SuggestionStatus.ACCEPTED
    assert (result.kind, result.tasks_written, result.draft_id) == (
        "apply_plan", 3, suggestion.action["draft_id"]
    )
    for task_id in ("graphs", "trees", "dp"):
        task = await world.repos.tasks.get(U, task_id)
        assert task.status is TaskStatus.PENDING
        assert task.scheduled_start >= _ist(MONDAY, 8)  # a new slot, from this morning on
    slots = await world.repos.task_events.list_for_user(
        U, types={TaskEventType.SCHEDULED, TaskEventType.RESCHEDULED}, since=MONDAY_7_05
    )
    assert {(e.task_id, e.event_type) for e in slots} == {
        ("graphs", TaskEventType.SCHEDULED),
        ("trees", TaskEventType.SCHEDULED),
        ("dp", TaskEventType.RESCHEDULED),
    }


@pytest.mark.asyncio
async def test_brief__accept_after_newer_plan_accepted__409_and_expired_tasks_unchanged(
    pinned_world, fake
):
    world = pinned_world
    await _seed_tasks(world.repos, [_row("graphs")])
    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion
    # Meanwhile the user plans in chat and accepts that plan: the brief's draft is superseded.
    newer = await world.repos.drafts.create(
        U, tasks=[{"task_id": "chat_1", "title": "Mock interview", "duration_minutes": 25}],
        horizon_start=_ist(MONDAY, 8).isoformat(), goal_id="chat",
        schedule={"chat_1": {"start_min": 120, "end_min": 145}},
    )
    await world.drafts.accept(U, newer.id)
    tasks_before = sorted(fake.rows["user_tasks"], key=lambda r: r["task_id"])

    with pytest.raises(errors.InvalidStateTransition) as raised:
        await world.service.accept(U, suggestion.id)

    assert raised.value.http_status == 409
    assert (await world.repos.suggestions.get(U, suggestion.id)).status is SuggestionStatus.EXPIRED
    assert sorted(fake.rows["user_tasks"], key=lambda r: r["task_id"]) == tasks_before
    assert (await world.repos.drafts.get(U, suggestion.action["draft_id"])).status is (
        DraftStatus.SUPERSEDED
    )


# --- PatternRule ------------------------------------------------------------------------


async def _run_rule(world: _World, at: datetime = MONDAY_7_05):
    return await world.service.run_trigger(U, "pattern_rule", force=True, now=at)


@pytest.mark.asyncio
async def test_pattern_rule__qualifying_skip_window__suggestion_with_adopt_rule(pinned_world):
    await _set_aside_at(pinned_world, 9)

    outcome = await _run_rule(pinned_world)
    again = await _run_rule(pinned_world, MONDAY_7_05 + timedelta(minutes=1))

    suggestion = outcome.suggestion
    (memory,) = await pinned_world.repos.memories.list(U, types=frozenset({"behavioral_pattern"}))
    assert outcome.status == "created"
    assert (suggestion.kind, suggestion.title) == ("pattern_rule", "A pattern around 9 AM")
    assert suggestion.body == (
        "You've set aside 3 of the last 3 tasks around 9 AM."
        " Want me to stop putting demanding work there?"
    )
    assert suggestion.action == {
        "kind": "adopt_rule",
        "dedupe_key": "pattern_rule:skip_time_window:9",
        "pattern_key": "skip_time_window:9",
        "hour": 9,
        "memory_id": memory.id,
        "confidence": 1.0,
        "occurrences": 3,
    }
    assert (suggestion.status, suggestion.consent_behavior) == (SuggestionStatus.PENDING, "ask")
    assert again.status == "nothing_to_say"  # already proposed: never twice
    _no_blame(suggestion.title + suggestion.body)


@pytest.mark.asyncio
async def test_pattern_rule__dismissed_before__denied(pinned_world):
    await _set_aside_at(pinned_world, 9)
    first = await _run_rule(pinned_world)
    await pinned_world.service.dismiss(U, first.suggestion.id)

    again = await _run_rule(pinned_world, MONDAY_7_05 + timedelta(minutes=1))

    assert again.status == "denied"
    assert again.decision.kind is ConsentReasonKind.LEARNED_PREFERENCE
    assert len(await pinned_world.repos.suggestions.list(U, status=None)) == 1


@pytest.mark.asyncio
async def test_pattern_rule__user_already_said_rule__denied_rule_match(pinned_world):
    await pinned_world.repos.constraints.add(U, BehavioralConstraint(
        id=str(uuid4()), user_id=U, raw_text="no work before 11 AM", constraint_type="habit",
        recurrence="daily", source="said",
    ))
    await _set_aside_at(pinned_world, 9)

    outcome = await _run_rule(pinned_world)

    assert outcome.status == "denied"
    assert outcome.decision.kind is ConsentReasonKind.RULE_MATCH
    assert outcome.decision.message == 'You already told me: "no work before 11 AM".'
    assert await pinned_world.repos.suggestions.list(U, status=None) == []


@pytest.mark.asyncio
async def test_pattern_rule__accept__constraint_with_semantic_slot_memory_confirmed_and_draft(
    world, clock, recording_llm
):
    clock.freeze(_ist(MONDAY, 8), schedule_endpoint)
    await _seed_tasks(world.repos, [_row(name) for name in ("a", "b", "c", "d", "e", "f")])
    await _set_aside_at(world, 9)
    suggestion = (await _run_rule(world)).suggestion

    accepted, result = await world.service.accept(U, suggestion.id)

    (rule,) = await world.repos.constraints.list_active(U)
    assert (rule.raw_text, rule.constraint_type, rule.recurrence, rule.source) == (
        "No demanding work between 09:00 and 10:00", "habit", "daily", "inferred"
    )
    assert rule.structured_semantics["pattern_key"] == "skip_time_window:9"
    (slot,) = rule.structured_semantics["semantic_slots"]
    assert {k: slot[k] for k in ("name", "start_min", "end_min", "availability", "recurrence")} == {
        "name": "learned_rule_09", "start_min": 60, "end_min": 120, "availability": "blocked",
        "recurrence": "daily",
    }
    memory = await world.repos.memories.get(U, suggestion.action["memory_id"])
    assert memory.last_confirmed_at is not None
    assert accepted.status is SuggestionStatus.ACCEPTED
    assert result.kind == "adopt_rule"
    assert result.message == (
        "Saved. I've drafted a plan that keeps 9 AM free — accept it when it suits you."
    )
    draft = await world.repos.drafts.get(U, result.draft_id)
    assert draft.status is DraftStatus.PENDING
    start = datetime.fromisoformat(draft.horizon_start)
    for placed in draft.schedule.values():
        begin = (start + timedelta(minutes=placed["start_min"])).astimezone(IST)
        end = (start + timedelta(minutes=placed["end_min"])).astimezone(IST)
        assert not (begin.hour < 10 and end > begin.replace(hour=9, minute=0)), (begin, end)
    assert recording_llm.calls == []  # the rule's slot is known: no habit translation


@pytest.mark.asyncio
async def test_pattern_rule__over_the_api__details_carry_the_numbers_and_accept_the_new_draft_id(
    world, clock, monkeypatch
):
    """What the dashboard reads: the pattern's numbers on the card, and the redraft's id
    in the accept response, so the user can be taken to it."""
    from types import SimpleNamespace

    import httpx

    from app.main import app

    clock.freeze(_ist(MONDAY, 8), schedule_endpoint)
    await _seed_tasks(world.repos, [_row("graphs"), _row("trees")])
    await _set_aside_at(world, 9, times=4)
    await world.repos.task_events.append(U, _outcome(  # one of the five done after all
        "kept", TaskEventType.COMPLETED, _ist(SUNDAY - timedelta(days=4), 9),
        _ist(SUNDAY - timedelta(days=4), 9, 25),
    ))  # the live pattern now reads 4 of 5
    await _run_rule(world)
    monkeypatch.setattr(app.state, "container", SimpleNamespace(anticipation=world.service),
                        raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        listed = await api.get("/api/v1/anticipation/suggestions", params={"user_id": U})
        (card,) = listed.json()["suggestions"]
        accepted = await api.post(
            f"/api/v1/anticipation/suggestions/{card['id']}/accept", params={"user_id": U}
        )

    details = card["details"]
    assert (details["pattern_key"], details["hour"]) == ("skip_time_window:9", 9)
    assert (details["confidence"], details["occurrences"]) == (0.8, 5)
    assert card["body"] == (
        "You've set aside 4 of the last 5 tasks around 9 AM."
        " Want me to stop putting demanding work there?"
    )
    assert accepted.status_code == 200
    draft_id = accepted.json()["result"]["draft_id"]
    draft = await world.repos.drafts.get(U, draft_id)
    assert draft.status is DraftStatus.PENDING
    assert {t["task_id"] for t in draft.tasks} == {"graphs", "trees"}


# --- the listener -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anticipation_listener__pattern_detected__suggestion_created(pinned_world):
    world = pinned_world
    world.bus.subscribe(PatternDetected, world.listener)
    peak = Pattern(detector="peak_hour", description="Best around 10", occurrences=3,
                   confidence=1.0, pattern_key="peak_hour:10", hour=10)
    await world.bus.publish(PatternDetected(occurred_at=MONDAY_7_05, user_id=U, pattern=peak))
    await world.listener.settle()
    assert await world.repos.suggestions.list(U, status=None) == []  # not a skip window

    await _set_aside_at(world, 9)  # PEARL remembers the pattern and announces it
    await world.listener.settle()

    (suggestion,) = await world.repos.suggestions.list(U, status=None)
    assert suggestion.trigger == "pattern_rule"
    assert suggestion.action["dedupe_key"] == "pattern_rule:skip_time_window:9"
