"""The first move: the morning brief and learned-rule proposals, under consent.

``MorningBriefTrigger`` drafts today's plan at the user's wake time: the pending
tasks, plus the ones whose slot passed since yesterday, each with a new slot, and
one thing Jarvis noticed. ``PatternRuleTrigger`` offers a confirmed habit as a rule.
Both run through the real ``AnticipationService`` over the Supabase repositories on
``FakeSupabase``; the brief drafts with the real planning sub-graph and CP-SAT, or,
where a test pins times, with a planner that places tasks where the test says.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
from app.container import default_pattern_detectors
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
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase, planning_services

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

    async def reslot_from_now(self, user_id: str, draft: DraftSchedule) -> dict | None:
        raise AssertionError("the brief never re-solves a draft in place")

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
    planner = planner or PlanningGraphReplanner(planning_services(repos))
    patterns = PatternService(
        repos.task_events, repos.memories, repos.profiles, bus,
        detectors=default_pattern_detectors(), clock=clock,
    )
    energy = HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator())
    drafts = DraftService(
        repos.drafts, bus, task_events=repos.task_events, tasks=repos.tasks, clock=clock
    )
    memory = MemoryService(repos.memories, BagOfWordsEmbedder())
    actions = build_action_factory(
        drafts=drafts, constraints=repos.constraints, memory=memory, planner=planner
    )
    brief = MorningBriefTrigger(
        repos.tasks, repos.task_events, planner, energy, patterns, actions,
        suggestions=repos.suggestions,
    )
    rule = PatternRuleTrigger(patterns, repos.suggestions, actions, repos.constraints)
    service = AnticipationService(
        {brief.name: brief, rule.name: rule}, actions, ConsentPolicy.default(),
        repos.suggestions, repos.profiles, repos.constraints, clock=clock,
    )
    listener = AnticipationListener(service, patterns)
    return _World(repos, bus, clock, patterns, drafts, service, listener)


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


def _starts(draft: DraftSchedule) -> dict[str, datetime]:
    """Each drafted task's start on the user's clock."""
    origin = datetime.fromisoformat(draft.horizon_start)
    return {
        task_id: (origin + timedelta(minutes=slot["start_min"])).astimezone(IST)
        for task_id, slot in draft.schedule.items()
    }


class _Habits:
    """A model that reads habits (structured, no slots) once ``readable``; until then it
    cannot be reached."""

    def __init__(self) -> None:
        self.readable = False
        self.calls: list[str] = []

    def install(self) -> None:
        from app.domain.entities import LLMLocation
        from app.domain.errors import LLMUnavailableError
        from app.domain.ports import LLMProvider
        from app.infrastructure.llm.chain import FallbackChainProvider
        from app.infrastructure.llm.factory import set_chain
        from app.schemas.context import SemanticTimeSlotsResponse

        habits = self

        class _Model(LLMProvider):
            name = "habits"
            location = LLMLocation.CLOUD

            def is_available(self) -> bool:
                return True

            async def complete(self, req):
                habits.calls.append(req.task)
                if not habits.readable:
                    raise LLMUnavailableError("The model is not answering.")
                return SemanticTimeSlotsResponse(semantic_slots=[])

            async def stream(self, req):
                yield ("content", "")

        set_chain(FallbackChainProvider([_Model()]))


def _choir_habit() -> BehavioralConstraint:
    # No deterministic pattern matches it: only the model can read it.
    return BehavioralConstraint(
        id=str(uuid4()), user_id=U, raw_text="Choir practice with the college band on Tuesdays",
        constraint_type="habit", recurrence="weekly",
    )


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
    # Pacing spreads a small plan over at least two days: one of the two lands today.
    assert re.fullmatch(
        r"1 task is planned for today; the hardest, 'Revise \w+', is at 08:00\.", suggestion.body
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
        "I noticed you tend to set work aside around 2 PM (3 of the last 3 times).",
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


@pytest.mark.asyncio
async def test_brief__automatic_at_07_01__todays_tasks_land_today_from_8am(world, clock):
    at = _ist(MONDAY, 7, 1)  # just after the default 07:00 wake, before the day starts
    clock.freeze(at, schedule_endpoint)
    await _seed_tasks(world.repos, [_row(name) for name in ("a", "b", "c", "d")])  # 2 a day

    outcome = await _run_brief(world, at)

    draft = await world.repos.drafts.get(U, outcome.suggestion.action["draft_id"])
    starts = _starts(draft)
    today = sorted(start for start in starts.values() if start.date() == MONDAY)
    assert datetime.fromisoformat(draft.horizon_start) == _ist(MONDAY, 8)  # today, not yesterday
    assert today == [_ist(MONDAY, 8), _ist(MONDAY, 8, 25)]
    assert all(start.date() <= TUESDAY for start in starts.values())  # nothing pushed further
    assert outcome.suggestion.body.startswith("2 tasks are planned for today;")


@pytest.mark.asyncio
async def test_brief__run_at_10_30__nothing_before_10_30(world, clock):
    at = _ist(MONDAY, 10, 30)
    clock.freeze(at, schedule_endpoint)
    await _seed_tasks(world.repos, [_row(name) for name in ("a", "b", "c", "d")])

    outcome = await _run_brief(world, at)

    draft = await world.repos.drafts.get(U, outcome.suggestion.action["draft_id"])
    starts = _starts(draft)
    assert min(starts.values()) >= at
    assert len([start for start in starts.values() if start.date() == MONDAY]) == 2


@pytest.mark.asyncio
async def test_brief__every_task_overdue__drafted_and_left_pending(world, clock):
    import app.services.analytical.missed_deadlines as missed_deadlines

    clock.freeze(MONDAY_7_05, schedule_endpoint, missed_deadlines)
    yesterday = SUNDAY.isoformat()
    await _seed_tasks(world.repos, [
        _row("graphs", deadline_hint=yesterday), _row("trees", deadline_hint=yesterday),
    ])

    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion

    assert suggestion.title == "Today is ready"
    assert suggestion.action["kind"] == "apply_plan"
    for task_id in ("graphs", "trees"):
        assert (await world.repos.tasks.get(U, task_id)).status is TaskStatus.PENDING
    assert await world.repos.task_events.list_for_user(
        U, types={TaskEventType.DEADLINE_MISSED}
    ) == []


@pytest.mark.asyncio
async def test_brief__slot_passed_this_morning__counted_as_earlier_today(pinned_world, pinned):
    this_morning, yesterday = _ist(MONDAY, 8), _ist(SUNDAY, 10)
    await _seed_tasks(pinned_world.repos, [
        _row("early", status="missed", start=this_morning),
        _row("dp", status="missed", start=yesterday),
    ])
    await pinned_world.repos.task_events.append_many(U, [
        _outcome("early", TaskEventType.MISSED, this_morning, this_morning + timedelta(minutes=55)),
        _outcome("dp", TaskEventType.MISSED, yesterday, yesterday + timedelta(minutes=55)),
    ])

    outcome = await _run_brief(pinned_world, _ist(MONDAY, 11))

    lines = outcome.suggestion.body.splitlines()
    assert sorted(pinned.calls[0]) == ["dp", "early"]
    assert "1 from yesterday found a new slot." in lines
    assert "1 from earlier today found a new slot." in lines


@pytest.mark.asyncio
async def test_brief__couldnt_fit_note_dismissed__next_day_still_briefed(fake, repos):
    planner = _PinnedPlanner(repos, raises=errors.ScheduleInfeasibleError("Too much for 30 days."))
    world = _world(fake, repos, planner)
    await _seed_tasks(repos, [_row("graphs")])
    monday = (await _run_brief(world, MONDAY_7_05)).suggestion
    await world.service.dismiss(U, monday.id)

    world.clock.now = _ist(TUESDAY, 7, 5)
    tuesday = await _run_brief(world, world.clock.now)

    assert monday.action["dedupe_key"] == "share_insight:brief:2026-11-16"
    assert tuesday.status == "created"
    assert tuesday.suggestion.action["dedupe_key"] == "share_insight:brief:2026-11-17"
    assert tuesday.suggestion.body == COULDNT_FIT


@pytest.mark.asyncio
async def test_brief__insight_dismissed__not_shared_again_when_its_numbers_change(pinned_world):
    world = pinned_world
    await _set_aside_at(world, 14)  # 3 of 3
    monday = (await _run_brief(world, MONDAY_7_05)).suggestion
    await world.service.dismiss(U, monday.id)
    slot = _ist(SUNDAY - timedelta(days=4), 14)  # one more set aside: now 4 of 4
    await world.repos.task_events.append(U, _outcome("past_14_x", TaskEventType.SKIPPED, slot, slot))

    world.clock.now = _ist(TUESDAY, 7, 5)
    tuesday = await _run_brief(world, world.clock.now)

    assert monday.title == "Something I noticed"
    assert monday.action["dedupe_key"] == "share_insight:skip_time_window:14"
    # Dismissed, it is no longer something to say: nothing is drafted, nothing asked.
    assert (tuesday.status, tuesday.suggestion) == ("nothing_to_say", None)


@pytest.mark.asyncio
async def test_brief__insight_dismissed__never_the_noticed_line_of_a_later_brief(pinned_world):
    """Dismissed once, a pattern is not shared again in any form: not as its own note,
    and not as the line about it inside a later brief that has a plan to offer."""
    world = pinned_world
    await _set_aside_at(world, 14)
    monday = (await _run_brief(world, MONDAY_7_05)).suggestion
    await world.service.dismiss(U, monday.id)
    await _seed_tasks(world.repos, [_row("graphs")])

    world.clock.now = _ist(TUESDAY, 7, 5)
    tuesday = (await _run_brief(world, world.clock.now)).suggestion

    assert monday.title == "Something I noticed"
    assert tuesday.title == "Today is ready"
    assert "2 PM" not in tuesday.body, tuesday.body


@pytest.mark.asyncio
async def test_brief__habits_could_not_be_read__tick_failed_nothing_settled_next_tick_retries(
    world, clock
):
    from app.services.analytical.habit_translator import invalidate_habit_cache

    invalidate_habit_cache()
    habits = _Habits()
    habits.install()
    clock.freeze(MONDAY_7_05, schedule_endpoint)
    await world.repos.constraints.add(U, _choir_habit())
    await _seed_tasks(world.repos, [_row("graphs")])

    first = await world.service.run_due(U, MONDAY_7_05)
    habits.readable = True
    clock.freeze(MONDAY_7_05 + timedelta(minutes=1), schedule_endpoint)
    second = await world.service.run_due(U, MONDAY_7_05 + timedelta(minutes=1))

    assert first[0].status == "failed"  # the morning brief; nothing stored, nothing settled
    assert second[0].status == "created"
    assert habits.calls.count("habit_translation") >= 2
    (brief,) = await world.repos.suggestions.list(U, status=None)
    assert brief.trigger == "morning_brief"


@pytest.mark.asyncio
async def test_brief__learned_evening_peak__hard_task_in_the_peak_and_due_today_reslot_first(
    fake, repos, clock, deterministic_solver
):
    """The demo: what Jarvis learned moves the math. Evenings go well and mornings get set
    aside, so the hardest task that is not due today goes to the evening peak, while
    yesterday's set-aside task, due today, takes the first slot of the day."""
    import app.core.config as config
    import app.modules.planning_graph as planning_graph
    from app.container import AppContainer
    from app.core import runtime
    from app.orchestrator.hooks import ActionHooks

    clock.freeze(MONDAY_7_05, schedule_endpoint, planning_graph)
    runtime.set_container(AppContainer.build(  # planning reads the focus profile through it
        supabase_client=fake, cfg=config, hooks=ActionHooks(), embedder=BagOfWordsEmbedder(),
        install_chain=False,
    ))
    world = _world(fake, repos)
    history = []
    for day in range(1, 15):  # two weeks of evenings done, mornings set aside
        date_ = MONDAY - timedelta(days=day)
        evening = _ist(date_, 19)
        history.append(_outcome(f"eve_{day}", TaskEventType.COMPLETED, evening,
                                evening + timedelta(minutes=25)))
        if day <= 3:
            for hour in (9, 10, 11, 12):
                morning = _ist(date_, hour)
                history.append(_outcome(f"am_{hour}_{day}", TaskEventType.SKIPPED, morning, morning))
    yesterday_10 = _ist(SUNDAY, 10)
    history.append(_outcome("dp", TaskEventType.MISSED, yesterday_10,
                            yesterday_10 + timedelta(minutes=55)))
    await repos.task_events.append_many(U, history)
    # Eight tasks, four a day: the hardest has no deadline, so it may lean to the peak;
    # yesterday's set-aside task is due today, so it may not.
    await _seed_tasks(repos, [
        _row("graphs", difficulty_weight=0.9),
        _row("arrays", difficulty_weight=0.5),
        _row("notes", difficulty_weight=0.3),
        *[_row(name, difficulty_weight=0.2) for name in ("cards", "recap")],
        *[_row(name, difficulty_weight=0.1) for name in ("tidy", "links")],
        _row("dp", status="missed", start=yesterday_10, difficulty_weight=0.8,
             deadline_hint=MONDAY.isoformat()),
    ])

    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion

    draft = await repos.drafts.get(U, suggestion.action["draft_id"])
    starts = _starts(draft)
    assert starts["dp"] == _ist(MONDAY, 8)  # due today: the first slot of the day
    assert starts["graphs"] == _ist(MONDAY, 19), starts  # the learned peak, not the morning
    today = [task_id for task_id, start in starts.items() if start.date() == MONDAY]
    assert suggestion.body.splitlines()[0] == (
        f"{len(today)} tasks are planned for today; the hardest, 'Revise graphs', is at 19:00"
        " — your strongest hour."
    )
    assert "1 from yesterday found a new slot." in suggestion.body.splitlines()


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


def _task_service(world: _World):
    from app.services.task_service import TaskService

    return TaskService(world.repos.tasks, world.repos.task_events, world.bus, clock=world.clock)


REFRESHED = "I refreshed the plan first — it's in place now."


@pytest.mark.asyncio
async def test_brief__task_done_before_accept__plan_refreshed_and_accepted(pinned_world, pinned):
    """The user finishes a task the brief planned, then taps Accept: the brief is no longer
    current, so it is drafted again from what is still open and put in place, once."""
    world = pinned_world
    await _seed_tasks(world.repos, [_row("graphs", difficulty_weight=0.9), _row("trees")])
    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion
    await _task_service(world).complete(U, "graphs")

    accepted, result = await world.service.accept(U, suggestion.id)
    again = await asyncio.gather(world.service.accept(U, suggestion.id), return_exceptions=True)

    assert accepted.status is SuggestionStatus.ACCEPTED
    assert result.message == REFRESHED
    assert result.tasks_written == 1 and result.draft_id != suggestion.action["draft_id"]
    assert pinned.calls == [[], []]  # the brief, then the refresh
    assert (await world.repos.tasks.get(U, "graphs")).status is TaskStatus.COMPLETED
    trees = await world.repos.tasks.get(U, "trees")
    assert trees.status is TaskStatus.PENDING and trees.scheduled_start == _ist(MONDAY, 9)
    assert (await world.repos.drafts.get(U, suggestion.action["draft_id"])).status is (
        DraftStatus.SUPERSEDED
    )
    assert isinstance(again[0], errors.StaleVersionError)  # done: a second tap is a 409
    _no_blame(result.message)


@pytest.mark.asyncio
async def test_brief__everything_done_before_accept__nothing_left_and_done(pinned_world, pinned):
    world = pinned_world
    await _seed_tasks(world.repos, [_row("graphs")])
    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion
    await _task_service(world).complete(U, "graphs")

    accepted, result = await world.service.accept(U, suggestion.id)

    assert accepted.status is SuggestionStatus.ACCEPTED
    assert (result.message, result.tasks_written, result.draft_id) == (
        "Nothing left to place today.", None, None
    )
    assert (await world.repos.tasks.get(U, "graphs")).status is TaskStatus.COMPLETED
    assert (await world.repos.drafts.get(U, suggestion.action["draft_id"])).status is (
        DraftStatus.EXPIRED
    )


@pytest.mark.asyncio
async def test_brief__accepted_three_hours_late__refreshed_from_now_and_nothing_set_aside(
    world, clock, deterministic_solver
):
    """Accepting the 07:05 brief at 10:05 would write slots that have gone by, and the
    next sweep would set them aside as if the user had: it is re-planned from now."""
    clock.freeze(MONDAY_7_05, schedule_endpoint)
    await _seed_tasks(world.repos, [_row(name) for name in ("a", "b", "c")])
    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion
    late = _ist(MONDAY, 10, 5)
    world.clock.now = late
    clock.freeze(late, schedule_endpoint)

    _, result = await world.service.accept(U, suggestion.id)

    assert result.message == REFRESHED
    for task_id in ("a", "b", "c"):
        task = await world.repos.tasks.get(U, task_id)
        assert task.status is TaskStatus.PENDING and task.scheduled_start >= late, task
    swept = await _task_service(world).sweep_passed_slots(U, late + timedelta(minutes=1))
    assert swept == []


@pytest.mark.asyncio
async def test_brief__stale_double_click__refreshed_once_and_second_409(
    pinned_world, pinned, fake
):
    world = pinned_world
    await _seed_tasks(world.repos, [_row("graphs"), _row("trees")])
    suggestion = (await _run_brief(world, MONDAY_7_05)).suggestion
    await _task_service(world).complete(U, "graphs")

    results = await asyncio.gather(
        world.service.accept(U, suggestion.id), world.service.accept(U, suggestion.id),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, Exception)]
    assert len(refused) == 1 and isinstance(refused[0], errors.StaleVersionError)
    assert pinned.calls == [[], []]  # one refresh, not two
    accepted = [r for r in fake.rows["draft_schedules"] if r["status"] == "accepted"]
    assert len(accepted) == 2  # the seeding draft, and the refresh


class _ClashingPlanner(_PinnedPlanner):
    """A refresh that still clashes: it drafts a task id the user has finished."""

    async def replan(self, user_id: str, *, reslot_task_ids: list[str]) -> DraftSchedule | None:
        self.calls.append(list(reslot_task_ids))
        if len(self.calls) == 1:
            return await super().replan(user_id, reslot_task_ids=reslot_task_ids)
        return await self._repos.drafts.create(
            user_id, tasks=[{"task_id": "graphs", "title": "Revise graphs"}],
            horizon_start=_ist(MONDAY, 8).isoformat(), goal_id=None,
            schedule={"graphs": {"start_min": 60, "end_min": 85}},
        )


@pytest.mark.asyncio
async def test_brief__accept_still_clashes__expired_never_pending_and_a_fresh_brief_today(
    fake, repos
):
    planner = _ClashingPlanner(repos)
    world = _world(fake, repos, planner)
    await _seed_tasks(repos, [_row("graphs"), _row("trees")])
    first = await world.service.run_due(U, MONDAY_7_05)
    suggestion = first[0].suggestion
    await _task_service(world).complete(U, "graphs")

    with pytest.raises(errors.ConflictError) as raised:
        await world.service.accept(U, suggestion.id)

    assert raised.value.http_status == 409
    assert (await repos.suggestions.get(U, suggestion.id)).status is SuggestionStatus.EXPIRED
    assert (await repos.tasks.get(U, "graphs")).status is TaskStatus.COMPLETED
    world.clock.now = MONDAY_7_05 + timedelta(minutes=2)
    planner.calls.clear()  # the next brief drafts like the first
    fresh = await world.service.run_due(U, world.clock.now)
    assert fresh[0].status == "created" and fresh[0].suggestion.id != suggestion.id


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
        "You've set aside 3 of the last 3 tasks around 9 AM. Want me to keep that hour clear?"
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
        "No work between 09:00 and 10:00", "habit", "daily", "inferred"
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
        "Saved. I've drafted a plan that keeps 9 AM clear — accept it when it suits you."
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
    world, fake, clock, monkeypatch
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
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(fake), raising=False)
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
        "You've set aside 4 of the last 5 tasks around 9 AM. Want me to keep that hour clear?"
    )
    assert accepted.status_code == 200
    draft_id = accepted.json()["result"]["draft_id"]
    draft = await world.repos.drafts.get(U, draft_id)
    assert draft.status is DraftStatus.PENDING
    assert {t["task_id"] for t in draft.tasks} == {"graphs", "trees"}


@pytest.mark.asyncio
async def test_pattern_rule__accept_after_pattern_dismissed__no_rule_stored_and_expired(
    pinned_world, pinned
):
    """The user dismisses the pattern on the You page, then taps Accept on the older
    proposal: no rule is stored, no plan drafted, and the proposal is expired."""
    world = pinned_world
    await _set_aside_at(world, 9)
    suggestion = (await _run_rule(world)).suggestion
    memory = MemoryService(world.repos.memories, BagOfWordsEmbedder())
    await memory.dismiss(U, suggestion.action["memory_id"])

    with pytest.raises(errors.ConflictError) as raised:
        await world.service.accept(U, suggestion.id)

    assert raised.value.http_status == 409
    assert raised.value.message == (
        "You told me this pattern doesn't fit, so I haven't made it a rule."
    )
    assert await world.repos.constraints.list_active(U) == []
    assert pinned.calls == []
    assert (await world.repos.suggestions.get(U, suggestion.id)).status is (
        SuggestionStatus.EXPIRED
    )
    _no_blame(raised.value.message)


@pytest.mark.asyncio
async def test_pattern_rule__window_covered_by_stated_rule__later_window_proposed(pinned_world):
    await pinned_world.repos.constraints.add(U, BehavioralConstraint(
        id=str(uuid4()), user_id=U, raw_text="no work before 11 AM", constraint_type="habit",
        recurrence="daily", source="said",
    ))
    await _set_aside_at(pinned_world, 9)  # already covered by what the user said
    await _set_aside_at(pinned_world, 14)

    outcome = await _run_rule(pinned_world)

    assert outcome.status == "created"
    assert outcome.suggestion.action["pattern_key"] == "skip_time_window:14"


@pytest.mark.asyncio
async def test_pattern_rule__accept_while_habits_unreadable__saved_and_says_the_redraft_waits(
    fake, repos
):
    planner = _PinnedPlanner(repos, raises=errors.LLMUnavailableError("The model is not answering."))
    world = _world(fake, repos, planner)
    await _set_aside_at(world, 9)
    suggestion = (await _run_rule(world)).suggestion

    accepted, result = await world.service.accept(U, suggestion.id)

    assert accepted.status is SuggestionStatus.ACCEPTED
    assert result.draft_id is None
    assert result.message == (
        "Saved. Every plan from now on keeps 9 AM clear — I'll draft today's around it"
        " as soon as I can read your habits again."
    )
    (rule,) = await repos.constraints.list_active(U)
    assert rule.structured_semantics["pattern_key"] == "skip_time_window:9"
    _no_blame(result.message)


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
