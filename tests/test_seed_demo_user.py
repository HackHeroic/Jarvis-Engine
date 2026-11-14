"""The demo seed: three weeks of history, written only through the live services.

Offline throughout: the InMemory family (and the Supabase family over FakeSupabase),
a bag-of-words embedder, and an LLM chain that fails any call. "Seeded history, live
inference": PEARL, the focus profile and a fresh plan are read off the seeded history
by the real services and the real CP-SAT solver, never asserted from the seed itself.
"""

import asyncio
import http.server
import random
import re
import threading
from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import scripts.seed_demo_user as seed
from app.domain.entities import LLMLocation, TaskEvent, TaskEventType
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


# The real probe, kept before the fixture below replaces it.
REAL_BACKEND_PROBE = getattr(seed, "_backend_answers", None)


@pytest.fixture(autouse=True)
def no_backend(monkeypatch) -> list[str]:
    """No backend answers, whatever runs on this machine's port 8000; returns the URLs probed."""
    probed: list[str] = []

    def nothing_answers(base_url: str) -> bool:
        probed.append(base_url)
        return False

    monkeypatch.setattr(seed, "_backend_answers", nothing_answers, raising=False)
    return probed


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
# Whatever weekday the seed runs on. Run Tue-Fri, this week's OS chunks are still set
# aside, so 8 PM peaks and 7 PM scores lower than when it runs Sat-Mon.
@pytest.mark.parametrize("today", A_WEEK_OF_RUN_DAYS, ids=lambda d: f"{d:%a}")
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
    """Whatever weekday it is run on, the live detectors find the evening peak, the
    Friday crunch and only one hour set aside, and every event type but a deadline miss
    is in the history. (The end-to-end test covers the morning pattern and the profile.)"""
    repos, report = await _seed(today)

    views = _by_key(await _pearl(repos, today).current(DEMO))
    assert [v.pattern.hour for v in views.values() if v.pattern.detector == "peak_hour"][0] in EVENING
    assert "deadline_crunch" in views
    assert [key for key in views if key.startswith("skip_time_window:")] == ["skip_time_window:9"]
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


def test_seed__dry_run__prints_the_report_without_touching_a_database(
    monkeypatch, capsys, no_backend
):
    def no_database():
        raise AssertionError("a dry run never connects")

    monkeypatch.setattr(seed, "_connect", no_database)

    assert seed.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "21 days" in out
    assert "tasks" in out and "scheduled" in out
    assert no_backend == []  # an in-memory run cannot be corrupted by a backend


def test_seed__success__ends_with_the_operator_note(capsys):
    yesterday = datetime.now(IST).date() - timedelta(days=1)

    assert seed.main(["--dry-run"]) == 0

    note = capsys.readouterr().out.split("Seeded through yesterday", 1)[1]
    assert f"{yesterday:%a %d %b}" in note
    assert "skip_time_window:9" in note and "deadline_crunch" in note
    assert re.search(r"learned peak hour: (7|8) PM", note, flags=re.IGNORECASE)
    assert "seed on the demo day; rerun with --reset to refresh" in note.lower()


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


def test_reset__a_row_the_cascade_should_have_taken_is_still_there__reported():
    # FakeSupabase has no cascade: these stand in for rows a missing FK would leave.
    fake = FakeSupabase({
        "user_profiles": [{"user_id": DEMO}],
        "chat_sessions": [{"user_id": DEMO}],
        "goals": [{"user_id": DEMO}],
    })

    with pytest.raises(seed.SeedProblem) as raised:
        seed.reset_user(fake, DEMO)

    assert "chat_sessions" in str(raised.value) and "goals" in str(raised.value)
    assert raised.value.exit_code == 1


# --- a running backend, a broken database, a half-written seed --------------------------


def _live(monkeypatch, fake: FakeSupabase) -> None:
    """main's live path, over ``fake`` and a bag-of-words embedder."""
    monkeypatch.setattr(seed, "_connect", lambda: fake)
    monkeypatch.setattr(seed, "MiniLMEmbedder", BagOfWordsEmbedder)


def _demo_rows(fake: FakeSupabase) -> dict:
    """The demo user's rows, by table (tables a probe merely opened are left out)."""
    rows = {table: [row for row in rows if row.get("user_id") == DEMO]
            for table, rows in fake.rows.items()}
    return {table: found for table, found in rows.items() if found}


def _row_counts(fake: FakeSupabase) -> dict:
    return {table: len(rows) for table, rows in fake.rows.items() if rows}


def test_main__a_backend_answers__refused_with_exit_3_before_anything_is_touched(
    monkeypatch, capsys, no_backend
):
    connected = []
    monkeypatch.setattr(seed, "_connect", lambda: connected.append(True))
    monkeypatch.setattr(seed, "_backend_answers", lambda base_url: True)

    assert seed.main(["--user", "demo", "--reset"]) == 3

    out = capsys.readouterr().out
    assert connected == []
    assert "JARVIS_ANTICIPATION=false" in out and "--allow-running-backend" in out


def test_main__a_backend_answers_but_allowed__seeds(monkeypatch):
    fake = FakeSupabase()
    _live(monkeypatch, fake)
    monkeypatch.setattr(seed, "_backend_answers", lambda base_url: True)

    assert seed.main(["--user", "demo", "--allow-running-backend"]) == 0

    assert fake.rows["task_events"]


def test_main__probes_the_backend_health_url_it_is_given(monkeypatch, no_backend):
    _live(monkeypatch, FakeSupabase())

    assert seed.main(["--user", "demo", "--base-url", "http://127.0.0.1:8123"]) == 0

    assert no_backend == ["http://127.0.0.1:8123"]


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        self.send_response(503)  # an unhealthy backend still answers, and still sweeps
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def test_backend_probe__answers_only_while_something_listens():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Health)
    base_url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert REAL_BACKEND_PROBE(base_url) is True
    finally:
        server.shutdown()
        server.server_close()

    assert REAL_BACKEND_PROBE(base_url) is False


def test_run__a_backend_sweep_lands_mid_seed__exit_1_and_says_what_happened(
    monkeypatch, capsys
):
    """The backend's minute tick, on the real clock, between an accept and its outcomes.
    Two days ago is inside its 7-day lookback and the day's slots are long past, so it
    sets every pending task aside at real now."""
    accept = seed._accept_plan
    accepted = []

    async def racing_accept(s, user_id, slots, horizon, *, at):
        await accept(s, user_id, slots, horizon, at=at)
        accepted.append(at)
        if len(accepted) == seed.DEFAULT_DAYS - 1:
            await s.tasks.sweep_passed_slots(user_id, datetime.now(timezone.utc))

    monkeypatch.setattr(seed, "_accept_plan", racing_accept)

    assert seed.main(["--dry-run"]) == 1

    out = capsys.readouterr().out
    assert "--reset" in out and "backend" in out
    assert "Seeded through yesterday" not in out


def _real_now_outcome(today):
    return TaskEvent(user_id=DEMO, task_id="rogue", event_type=TaskEventType.COMPLETED,
                     occurred_at=datetime.now(timezone.utc))


def _outcome_dated_today(today):
    return TaskEvent(user_id=DEMO, task_id="rogue", event_type=TaskEventType.MISSED,
                     occurred_at=datetime.combine(today, time(0, 30), IST))


def _extra_scheduled(today):
    slot = datetime.combine(today - timedelta(days=1), time(9), IST)
    return TaskEvent(user_id=DEMO, task_id="rogue", event_type=TaskEventType.SCHEDULED,
                     occurred_at=slot - timedelta(hours=2), scheduled_start=slot,
                     scheduled_end=slot + timedelta(minutes=25))


@pytest.mark.parametrize(
    ("stray_event", "summary"),
    [
        (_real_now_outcome, "real clock"),
        (_outcome_dated_today, "dated today"),
        (_extra_scheduled, "scheduled events for"),
    ],
    ids=["outcome_at_real_now", "event_dated_today", "extra_scheduled_event"],
)
def test_run__self_check__stray_event__exit_1_with_a_plain_summary(
    monkeypatch, capsys, stray_event, summary
):
    real_seed = seed.seed_history

    async def seed_then_stray(s, user_id, **kwargs):
        report = await real_seed(s, user_id, **kwargs)
        await s.task_events.append(user_id, stray_event(kwargs["today"]))
        return report

    monkeypatch.setattr(seed, "seed_history", seed_then_stray)

    assert seed.main(["--dry-run"]) == 1

    out = capsys.readouterr().out
    assert summary in out and "--reset" in out


def test_run__self_check__no_closing_pattern_memory__exit_1(monkeypatch, capsys):
    async def looks_away(self, user_id):
        return []

    monkeypatch.setattr(PatternService, "observe", looks_away)

    assert seed.main(["--dry-run"]) == 1

    assert "closing pattern memory" in capsys.readouterr().out


class _SchemaGap(FakeSupabase):
    """FakeSupabase over a database that lacks one column, table or function."""

    def __init__(self, rows: dict, *, column: tuple[str, str] | None = None,
                 function: str | None = None) -> None:
        super().__init__(rows)
        self._column = column
        self._function = function

    def table(self, name: str):
        query = super().table(name)
        if self._column is not None and name == self._column[0]:
            select = query.select

            def select_or_fail(*columns: str):
                if any(self._column[1] in c for c in columns):
                    raise Exception(f"column {name}.{self._column[1]} does not exist")
                return select(*columns)

            query.select = select_or_fail
        return query

    def rpc(self, name: str, params: dict):
        if name == self._function:
            raise Exception(f"PGRST202 Could not find the function public.{name}")
        return super().rpc(name, params)


DEMO_LEFTOVERS = {
    "user_tasks": [{"user_id": DEMO, "task_id": "t1", "title": "t", "status": "pending"}],
    "task_events": [{"user_id": DEMO, "task_id": "t1", "event_type": "scheduled"}],
    "user_profiles": [{"user_id": DEMO}],
}


@pytest.mark.parametrize(
    "gap",
    [
        {"column": ("task_events", "event_type")},
        {"column": ("user_memories", "pattern_key")},
        {"column": ("user_memories", "provenance")},
        {"column": ("proactive_suggestions", "consent_kind")},
        {"function": "accept_draft"},
        {"function": "match_memories"},
    ],
    ids=lambda gap: ".".join(gap.get("column") or (gap.get("function"),)),
)
def test_main__database_not_migrated__exit_2_before_the_reset_deletes_anything(
    monkeypatch, capsys, gap
):
    fake = _SchemaGap({t: [dict(r) for r in rows] for t, rows in DEMO_LEFTOVERS.items()}, **gap)
    _live(monkeypatch, fake)
    before = _demo_rows(fake)

    assert seed.main(["--user", "demo", "--reset"]) == 2

    assert "apply the migrations first" in capsys.readouterr().out
    assert _demo_rows(fake) == before


def test_main__embedder_not_working__exit_2_before_the_reset_deletes_anything(
    monkeypatch, capsys
):
    from app.domain.errors import EmbeddingUnavailableError

    class _NoModel(BagOfWordsEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingUnavailableError("Text embeddings are unavailable.")

    fake = FakeSupabase({t: [dict(r) for r in rows] for t, rows in DEMO_LEFTOVERS.items()})
    monkeypatch.setattr(seed, "_connect", lambda: fake)
    monkeypatch.setattr(seed, "MiniLMEmbedder", _NoModel)
    before = _demo_rows(fake)

    assert seed.main(["--user", "demo", "--reset"]) == 2

    assert "embedder" in capsys.readouterr().out
    assert _demo_rows(fake) == before


def test_guard__events_but_no_closing_memory__partial_seed_exit_1(monkeypatch, capsys):
    """A run that died before PEARL looked: its history is there, its patterns are not."""
    fake = FakeSupabase()
    _live(monkeypatch, fake)
    assert seed.main(["--user", "demo"]) == 0
    fake.rows["user_memories"] = [r for r in fake.rows["user_memories"]
                                  if r.get("source") != "behavior"]
    seeded = _row_counts(fake)
    capsys.readouterr()

    assert seed.main(["--user", "demo"]) == 1

    assert "partial seed found — rerun with --reset" in capsys.readouterr().out
    assert _row_counts(fake) == seeded


async def _leave_task(repos) -> None:
    """A demo task with no history: a plan accepted when no event store was wired."""
    from app.services.draft_service import DraftService

    draft = await repos.drafts.create(
        DEMO,
        tasks=[{"task_id": "reading_a1b2c3_task_1", "title": "Read the OS paper",
                "duration_minutes": 25, "difficulty_weight": 0.5,
                "completion_criteria": "Write a three-line summary", "goal_id": "reading"}],
        horizon_start=datetime.combine(TODAY, time(8), IST).isoformat(),
        goal_id=None, schedule=None,
    )
    await DraftService(repos.drafts, EventBus()).accept(DEMO, draft.id)


async def _leave_memory(repos) -> None:
    from app.services.memory.memory_service import MemoryCandidate, MemoryService

    await MemoryService(repos.memories, BagOfWordsEmbedder()).remember(
        DEMO, MemoryCandidate(memory_type="fact", content="I play the violin on Sundays.")
    )


async def _leave_suggestion(repos) -> None:
    from app.domain.entities import Suggestion, SuggestionStatus

    await repos.suggestions.add(DEMO, Suggestion(
        user_id=DEMO, trigger="morning_brief", kind="reslot", title="A new slot",
        body="I found a new slot for this.", status=SuggestionStatus.PENDING,
        consent_behavior="ask", consent_reason="You asked to be asked first.",
        created_at=datetime.now(timezone.utc),
    ))


@pytest.mark.parametrize("leave", [_leave_task, _leave_memory, _leave_suggestion],
                         ids=["user_tasks", "user_memories", "proactive_suggestions"])
def test_guard__demo_rows_but_no_history__partial_seed_exit_1_never_a_merge(
    monkeypatch, capsys, leave
):
    fake = FakeSupabase()
    asyncio.run(leave(RepositoryFactory.supabase(fake)))
    _live(monkeypatch, fake)
    before = _row_counts(fake)

    assert seed.main(["--user", "demo"]) == 1

    assert "partial seed found — rerun with --reset" in capsys.readouterr().out
    assert _row_counts(fake) == before
