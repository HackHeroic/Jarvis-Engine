"""The demo seed: three weeks of history, written only through the live services.

Offline throughout: the InMemory family (and the Supabase family over FakeSupabase),
a bag-of-words embedder, and an LLM chain that fails any call. "Seeded history, live
inference": PEARL, the focus profile and a fresh plan are read off the seeded history
by the real services and the real CP-SAT solver, never asserted from the seed itself.
"""

import asyncio
import random
from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

import scripts.seed_demo_user as seed
from app.domain.entities import LLMLocation, TaskEventType
from app.domain.events import EventBus
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator, solver_focus
from app.services.memory.patterns import PatternService
from tests.behaviour.conftest import blame_phrases_in
from tests.fakes import BagOfWordsEmbedder, FakeSupabase

IST = ZoneInfo("Asia/Kolkata")
DEMO = seed.DEMO_USER_ID
TODAY = date(2026, 11, 14)  # a Saturday: the 21 days before it are three whole weeks
# The seed runs on whatever day it is run: the regularities must hold from any weekday.
A_WEEK_OF_RUN_DAYS = [TODAY + timedelta(days=i) for i in range(7)]
MORNING = range(9, 13)
EVENING = range(18, 22)
# What the live writers put in event metadata: TaskService (Task 1) on every outcome,
# and DraftService (Task 2), which adds previous_start to a rescheduled event.
LIVE_METADATA_KEYS = {
    "title", "goal_id", "deadline_hint", "actual_duration_minutes", "quality", "reason",
    "previous_start",
}


class _FailEveryCall(LLMProvider):
    """The only provider in the chain: records any call, then fails it."""

    name = "fail-every-call"
    location = LLMLocation.CLOUD

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    async def complete(self, req: LLMRequest) -> str:
        self.calls.append(req)
        raise AssertionError("the seed must never call a model")

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        self.calls.append(req)
        raise AssertionError("the seed must never call a model")
        yield ("content", "")  # pragma: no cover - makes this an async generator


@pytest.fixture(autouse=True)
def failing_chain() -> _FailEveryCall:
    """Every test here runs with a chain that fails any model call."""
    model = _FailEveryCall()
    set_chain(FallbackChainProvider([model]))
    return model


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time(0), IST)


async def _seed(today: date = TODAY, repos=None):
    """Seed the demo user's history into ``repos`` (a fresh InMemory family by default)."""
    repos = repos if repos is not None else RepositoryFactory.in_memory()
    clock = seed.SimClock(_start_of(today) - timedelta(days=seed.DEFAULT_DAYS))
    services = seed.build_seed_services(repos, BagOfWordsEmbedder(), clock)
    report = await seed.seed_history(
        services, DEMO, days=seed.DEFAULT_DAYS, today=today, zone=IST,
        rng=random.Random(seed.SEED),
    )
    return repos, report


def _pearl(repos, today: date = TODAY) -> PatternService:
    """The real PEARL over the seeded history, looking at it on the morning of ``today``."""
    at = _start_of(today)
    return PatternService(
        repos.task_events, repos.memories, repos.profiles, EventBus(), clock=lambda: at
    )


def _energy(repos) -> HistoryEnergyEstimator:
    return HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator())


def _by_key(views) -> dict:
    return {view.pattern.pattern_key: view for view in views}


# --- what live inference finds in the seeded history -----------------------------


@pytest.mark.asyncio
async def test_seed__history__pearl_finds_morning_skips_and_evening_peak():
    repos, _ = await _seed()

    views = _by_key(await _pearl(repos).current(DEMO))

    assert "skip_time_window:9" in views
    peaks = [view.pattern for view in views.values() if view.pattern.detector == "peak_hour"]
    assert len(peaks) == 1
    assert peaks[0].hour in EVENING


@pytest.mark.asyncio
async def test_seed__history__focus_profile_learned_evening_above_morning():
    repos, _ = await _seed()

    profile = await _energy(repos).estimate(DEMO, IST, now=_start_of(TODAY))

    assert profile.source == "learned"
    scores = profile.scores()
    assert min(scores[19], scores[20]) > max(scores[h] for h in MORNING)


@pytest.mark.asyncio
async def test_seed__history__deadline_crunch_detected():
    repos, _ = await _seed()

    views = _by_key(await _pearl(repos).current(DEMO))

    assert "deadline_crunch" in views
    crunch = views["deadline_crunch"].pattern
    assert crunch.occurrences >= 3
    assert crunch.confidence >= 0.7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "today",
    # Saturday: the 7 PM hour peaks. Wednesday: this week's OS chunks are still set
    # aside, so 8 PM peaks and 7 PM scores lower.
    [TODAY, TODAY + timedelta(days=4)],
    ids=lambda d: f"{d:%a}",
)
async def test_seed__history__vision_end_to_end__pattern_profile_and_plan_lean_evening(
    today, deterministic_solver,
):
    """(a) PEARL holds a remembered morning-skip pattern, seen 3+ times at 70%+ and
    told to the user; (b) the focus profile is learned, evenings above mornings; (c) a
    fresh plan of mixed difficulty puts the hardest task in the evening peak (real CP-SAT)."""
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata, TaskChunk
    from app.api.v1.endpoints.schedule import run_schedule
    from app.schemas.context import Availability, TimeSlot

    repos, _ = await _seed(today)
    pearl = _pearl(repos, today)

    # (a) Seen 3+ times at 70%+, remembered by the seed's own PEARL run, and told.
    skip = _by_key(await pearl.current(DEMO))["skip_time_window:9"]
    assert skip.pattern.occurrences >= 3
    assert skip.pattern.confidence >= 0.7
    assert skip.memory_id is not None
    assert skip.memory_id in {insight["memory_id"] for insight in await pearl.insights(DEMO)}

    # (b) Learned, so it steers the solver; the evening beats the morning.
    profile = await _energy(repos).estimate(DEMO, IST, now=_start_of(today))
    assert profile.source == "learned"
    scores = profile.scores()
    assert min(scores[19], scores[20]) > max(scores[h] for h in MORNING)

    # (c) What the planner leans on, from the live gate, and a real solve with it.
    focus = await solver_focus(_energy(repos), DEMO, IST, now=_start_of(today))
    assert focus is not None
    difficulties = {"hardest": 0.9, "hard": 0.8, "medium_hard": 0.7, "medium": 0.6,
                    "middling": 0.5, "light": 0.3, "easy": 0.2, "easiest": 0.1}
    graph = ExecutionGraph(
        goal_metadata=GoalMetadata(),
        decomposition=[
            TaskChunk(task_id=tid, title=tid, duration_minutes=25, difficulty_weight=d,
                      dependencies=[], completion_criteria="done")
            for tid, d in difficulties.items()
        ],
        cognitive_load_estimate={"intrinsic_load": 0.5},
    )
    nights = [TimeSlot(name=f"night_{d}", start_min=d * 1440 + 960, end_min=(d + 1) * 1440,
                       availability=Availability.BLOCKED) for d in range(2)]
    horizon_start = datetime.combine(today, time(8), IST)

    plan = run_schedule(graph, nights, horizon_minutes=2880, horizon_start=horizon_start,
                        zone=IST, focus_by_hour=focus)

    hardest = horizon_start + timedelta(minutes=plan.schedule["hardest"].start_min)
    assert hardest.astimezone(IST).hour in EVENING, hardest


@pytest.mark.asyncio
@pytest.mark.parametrize("today", A_WEEK_OF_RUN_DAYS, ids=lambda d: f"{d:%a}")
async def test_seed__any_run_day__regularities_hold(today):
    """Whatever weekday it is run on, the seeded regularities are what the live
    detectors and the focus profile find, and every event type but a deadline miss
    is in the history."""
    repos, report = await _seed(today)

    views = _by_key(await _pearl(repos, today).current(DEMO))
    skip = views["skip_time_window:9"].pattern
    assert skip.occurrences >= 3 and skip.confidence >= 0.7
    assert [v.pattern.hour for v in views.values() if v.pattern.detector == "peak_hour"][0] in EVENING
    assert "deadline_crunch" in views
    assert [key for key in views if key.startswith("skip_time_window:")] == ["skip_time_window:9"]
    profile = await _energy(repos).estimate(DEMO, IST, now=_start_of(today))
    scores = profile.scores()
    assert profile.source == "learned"
    assert min(scores[19], scores[20]) > max(scores[h] for h in MORNING)
    assert set(report["events_by_type"]) == {t.value for t in TaskEventType} - {"deadline_missed"}


# --- only the shapes the app writes --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["in_memory", "supabase_over_fake"])
async def test_seed__every_row_shape_is_a_live_shape(family):
    if family == "in_memory":
        store = InMemoryStore()
        repos, report = await _seed(repos=RepositoryFactory.in_memory(store))
        events = [(e.event_type.value, e.metadata) for e in store.task_events]
        tasks = [(t.user_id, t.plan_id) for t in store.tasks]
    else:
        fake = FakeSupabase()
        repos, report = await _seed(repos=RepositoryFactory.supabase(fake))
        events = [(row["event_type"], row["metadata"]) for row in fake.rows["task_events"]]
        tasks = [(row["user_id"], row.get("plan_id")) for row in fake.rows["user_tasks"]]

    assert {kind for kind, _ in events} <= {t.value for t in TaskEventType}
    for kind, metadata in events:
        assert set(metadata) <= LIVE_METADATA_KEYS, (kind, metadata)
    assert len(tasks) == report["tasks"]
    assert all(user_id == DEMO and plan_id for user_id, plan_id in tasks)


@pytest.mark.asyncio
async def test_seed__user_facing_copy__no_blame_language():
    fake = FakeSupabase()
    await _seed(repos=RepositoryFactory.supabase(fake))

    copy = [row["content"] for row in fake.rows["user_memories"] if row["provenance"] == "said"]
    for row in fake.rows["user_tasks"]:
        copy += [row["title"], row["completion_criteria"],
                 *row["implementation_intention"].values()]

    assert len(copy) > 3
    flagged = {text: blame_phrases_in(text) for text in copy
               if blame_phrases_in(text) or "missed" in text.lower()}
    assert flagged == {}


@pytest.mark.asyncio
async def test_seed__report__about_three_weeks_and_ninety_tasks():
    _, report = await _seed()

    assert report["days"] == 21
    assert 80 <= report["tasks"] <= 100
    assert report["memories"] == 3
    assert report["events_by_type"]["scheduled"] == report["tasks"]
    assert "deadline_missed" not in report["events_by_type"]


# --- idempotent, repeatable, offline -------------------------------------------------


def test_seed__twice_without_reset__second_is_a_no_op(monkeypatch, capsys):
    fake = FakeSupabase()
    monkeypatch.setattr(seed, "_connect", lambda: fake)
    monkeypatch.setattr(seed, "MiniLMEmbedder", BagOfWordsEmbedder)

    assert seed.main(["--user", "demo"]) == 0
    seeded = {table: len(rows) for table, rows in fake.rows.items()}
    capsys.readouterr()
    assert seed.main(["--user", "demo"]) == 0

    out = capsys.readouterr().out
    assert f"demo already seeded ({seeded['task_events']} events) — pass --reset to rebuild" in out
    assert {table: len(rows) for table, rows in fake.rows.items()} == seeded


def test_seed__reset__rebuilds_the_same_history_not_a_second_one(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(seed, "_connect", lambda: fake)
    monkeypatch.setattr(seed, "MiniLMEmbedder", BagOfWordsEmbedder)
    assert seed.main(["--user", "demo"]) == 0
    seeded = {table: len(rows) for table, rows in fake.rows.items() if rows}

    assert seed.main(["--user", "demo", "--reset"]) == 0

    assert {table: len(rows) for table, rows in fake.rows.items() if rows} == seeded


@pytest.mark.asyncio
async def test_seed__same_seed__same_counts():
    _, first = await _seed()
    _, second = await _seed()

    assert first == second


@pytest.mark.asyncio
async def test_seed__no_llm_calls(failing_chain):
    store = InMemoryStore()

    await _seed(repos=RepositoryFactory.in_memory(store))

    assert failing_chain.calls == []
    assert store.usage == []


def test_seed__dry_run__prints_the_report_without_touching_a_database(monkeypatch, capsys):
    def no_database():
        raise AssertionError("a dry run never connects")

    monkeypatch.setattr(seed, "_connect", no_database)

    assert seed.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "21 days" in out
    assert "tasks" in out and "scheduled" in out


# --- the demo profile only -------------------------------------------------------------


def test_seed__other_user__refused_before_anything_is_touched(monkeypatch, capsys):
    connected = []
    monkeypatch.setattr(seed, "_connect", lambda: connected.append(True))

    assert seed.main(["--user", "someone-else"]) == 2
    assert seed.main(["--user", "someone-else", "--reset"]) == 2

    assert connected == []
    assert "someone-else" in capsys.readouterr().out
    with pytest.raises(ValueError):
        seed.reset_user(FakeSupabase(), "someone-else")
    services = seed.build_seed_services(
        RepositoryFactory.in_memory(), BagOfWordsEmbedder(), seed.SimClock(_start_of(TODAY))
    )
    with pytest.raises(ValueError):
        asyncio.run(seed.seed_history(services, "someone-else", days=1, today=TODAY,
                                      zone=IST, rng=random.Random(seed.SEED)))


class _TableLog(FakeSupabase):
    """FakeSupabase that remembers the order tables are opened in."""

    def __init__(self, rows: dict) -> None:
        super().__init__(rows)
        self.opened: list[str] = []

    def table(self, name: str):
        self.opened.append(name)
        return super().table(name)


def test_reset__deletes_only_the_demo_users_rows__usage_first_profile_last():
    tables = ("llm_usage_events", "task_events", "user_tasks", "draft_schedules",
              "user_memories", "user_profiles")
    fake = _TableLog({
        table: [{"user_id": DEMO, "n": 1}, {"user_id": "someone-else", "n": 2}]
        for table in tables
    })

    seed.reset_user(fake, DEMO)

    # Usage rows would only lose their user when the profile goes (ON DELETE SET NULL).
    assert fake.opened[0] == "llm_usage_events"
    # The profile goes last: on the live database its cascade takes every other table.
    first_open = {table: fake.opened.index(table) for table in tables}
    assert first_open["user_profiles"] == max(first_open.values())
    for table in tables:
        assert [row["user_id"] for row in fake.rows[table]] == ["someone-else"], table
