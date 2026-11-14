"""Psychology: the frameworks Jarvis claims, checked in what the user actually gets.

* TMT — a task due sooner is scheduled sooner.
* CLT — no day carries more than the adaptive cap; a hard goal lowers the cap by a fifth.
* WOOP — every task arrives with an if-then plan for its likely obstacle.
* Pomodoro — no task is longer than 25 minutes.
* Anti-guilt — every deterministic message on the missed-task, overdue, infeasible and
  skip paths is free of blame; model-written text is steered by an explicit instruction.
* Mastery orientation — a progress check is answered from the user's own mastery data,
  never against other people.
* Biology — without a sleep habit, nothing lands between midnight and 8 AM.

Model-written replies cannot be judged offline, so for those the tests check the
contract at the model boundary (what the model is told, and with which data) and
the deterministic fallbacks a user sees when the model is unavailable.
"""

import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.services.analytical.missed_deadlines as missed_deadlines
from app.domain.errors import LLMUnavailableError
from app.utils.pacing import compute_adaptive_daily_cap
from tests.behaviour.conftest import blame_phrases_in, brain_dump, decomposition

# The user's clock (India). Plans are made at 08:00 local, the start of their day;
# these were UTC times when the horizon was UTC-anchored.
IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
HORIZON_START = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
MINUTES_PER_DAY = 1440


def _day(offset: int) -> str:
    return (MONDAY_8AM + timedelta(days=offset)).date().isoformat()


def _script_plan(llm, chunker_reply: str, goal: str = "Revise DSA") -> None:
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal=goal) if "plan" in req.prompt.lower() else "{}",
    )
    llm.script("socratic_chunker", chunker_reply)


def _starts_by_title(turn) -> dict[str, int]:
    return {t["title"]: t["start_min"] for t in turn.payload["schedule"]["schedule"].values()}


def _minutes_per_day(turn) -> dict[int, int]:
    days: dict[int, int] = {}
    for task in turn.payload["schedule"]["schedule"].values():
        day = task["start_min"] // MINUTES_PER_DAY
        days[day] = days.get(day, 0) + task["end_min"] - task["start_min"]
    return days


@pytest.fixture
def monday_morning(clock):
    clock.freeze(MONDAY_8AM, schedule_endpoint, missed_deadlines)
    return clock


@pytest.fixture
def cap_spy(monkeypatch) -> list[dict]:
    """Every daily cap the scheduler is given, with the inputs that produced it."""
    seen: list[dict] = []

    def spy(**kwargs):
        cap = compute_adaptive_daily_cap(**kwargs)
        seen.append({**kwargs, "cap": cap})
        return cap

    monkeypatch.setattr(schedule_endpoint, "compute_adaptive_daily_cap", spy)
    return seen


@asynccontextmanager
async def _api(monkeypatch, jarvis=None):
    from app.main import app

    if jarvis is not None:
        import app.core.config as cfg
        from app.container import AppContainer
        from app.orchestrator.hooks import ActionHooks

        monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
        monkeypatch.setattr(app.state, "memory_store", jarvis.memory_store, raising=False)
        # The composition root the lifespan builds, over the same database.
        container = AppContainer.build(supabase_client=jarvis.db, cfg=cfg, hooks=ActionHooks())
        monkeypatch.setattr(app.state, "container", container, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --- TMT ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tmt__task_due_today__scheduled_before_one_due_in_three_days(
    jarvis, llm, monday_morning
):
    # The decomposer lists the later deadline first; the schedule must not follow it.
    _script_plan(llm, decomposition(["Essay outline", "Lab report"], deadline=[_day(3), _day(0)]))

    turn = await jarvis.say("Plan my essay outline and the lab report due today")

    starts = _starts_by_title(turn)
    assert starts["Lab report"] < starts["Essay outline"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "TMT ordering collapses for deadlines more than ~16 h away: run_schedule hands "
        "the solver priority max(1, int(motivation*100)), and motivation*100 < 1 for "
        "any delay over ~16 h, so tasks due in 2 and in 5 days both get priority 1 and "
        "the decomposer's listing order, not the deadline, decides which runs first. "
        "The raw TMT scores are right (0.016 vs 0.007); restoring the order needs a "
        "finer priority scale rebalanced against the solver's makespan (15) and "
        "spread (5-50) weights — a design decision, not a one-line fix."
    ),
)
@pytest.mark.asyncio
async def test_tmt__two_equal_tasks__earlier_deadline_scheduled_first(
    jarvis, llm, monday_morning
):
    _script_plan(llm, decomposition(["Essay outline", "Lab report"], deadline=[_day(5), _day(2)]))

    turn = await jarvis.say("Plan my essay outline and lab report")

    starts = _starts_by_title(turn)
    assert starts["Lab report"] < starts["Essay outline"]


# --- CLT ---------------------------------------------------------------------------

SEVEN_TOPICS = [f"Topic {i}" for i in range(1, 8)]  # 175 minutes of work


@pytest.mark.asyncio
async def test_clt__busiest_day__never_exceeds_the_adaptive_daily_cap(
    jarvis, llm, monday_morning, cap_spy
):
    _script_plan(llm, decomposition(SEVEN_TOPICS))

    turn = await jarvis.say("Plan my revision of seven topics")

    cap = cap_spy[-1]["cap"]  # the solve that produced this schedule
    per_day = _minutes_per_day(turn)
    assert sum(per_day.values()) == 175
    assert len(per_day) > 1  # spread, not crammed
    assert max(per_day.values()) <= cap


def test_clt__high_intrinsic_load__formula_takes_a_fifth_off_the_cap():
    ordinary = compute_adaptive_daily_cap(horizon_minutes=2880, total_task_minutes=175)
    hard = compute_adaptive_daily_cap(
        horizon_minutes=2880, total_task_minutes=175, intrinsic_load=0.9
    )

    assert hard == int(ordinary * 0.8)


@pytest.mark.asyncio
async def test_clt__high_intrinsic_load__the_plan_gets_the_lower_cap_and_a_lighter_day(
    jarvis, llm, monday_morning, cap_spy
):
    _script_plan(llm, decomposition(SEVEN_TOPICS, intrinsic_load=0.5))
    ordinary = await jarvis.say("Plan my revision of seven topics", user="alice")
    ordinary_cap = cap_spy[0]
    cap_spy.clear()
    _script_plan(llm, decomposition(SEVEN_TOPICS, intrinsic_load=0.9))

    hard = await jarvis.say("Plan my revision of seven topics", user="bob")

    # Same work, same 48 h first attempt: the model's load estimate reaches the cap.
    assert cap_spy[0]["horizon_minutes"] == ordinary_cap["horizon_minutes"] == 2880
    assert cap_spy[0]["intrinsic_load"] == 0.9
    assert cap_spy[0]["cap"] < ordinary_cap["cap"]
    # And the user feels it: the hard goal's busiest day is lighter.
    assert max(_minutes_per_day(hard).values()) < max(_minutes_per_day(ordinary).values())


@pytest.mark.slow
@pytest.mark.asyncio
async def test_planning__solve_runs_to_the_solver_time_cap__best_schedule_still_delivered(
    jarvis, llm, monday_morning
):
    """Slow on purpose (about SOLVER_MAX_TIME_SECONDS, 10 s by default), so it runs only
    under ``pytest -m slow``: ten tasks of graded difficulty keep CP-SAT busy for its
    whole time budget, after which it returns the best schedule it found. Ten tasks is
    an ordinary plan once other goals' pending tasks are fused in."""
    titles = [f"Topic {i}" for i in range(1, 11)]
    graded = decomposition(titles)
    for i in range(1, 11):
        graded = graded.replace('"difficulty_weight": 0.5', f'"difficulty_weight": {i / 10}', 1)
    _script_plan(llm, graded)

    turn = await jarvis.say("Plan my revision of ten topics")

    assert turn.draft_id is not None
    assert len(turn.payload["schedule"]["schedule"]) == 10


# --- WOOP and Pomodoro -------------------------------------------------------------


@pytest.mark.asyncio
async def test_woop__every_planned_task__keeps_its_if_then_plan_into_the_task_list(
    jarvis, llm, monday_morning
):
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision")

    await jarvis.say("looks good", session=planned.conversation_id)

    rows = jarvis.rows("user_tasks", user_id="alice")
    assert len(rows) == 5
    for row in rows:
        plan = row["implementation_intention"]
        assert plan["obstacle_trigger"].startswith("If ")
        assert plan["behavioral_response"].startswith("then ")


@pytest.mark.asyncio
async def test_woop__decomposition_prompt__asks_for_an_if_then_plan_on_every_task(
    jarvis, llm, monday_morning
):
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))

    await jarvis.say("Plan my DSA revision")

    (chunker,) = llm.calls_for("socratic_chunker")
    instruction = chunker.system_prompt
    assert "implementation_intention" in instruction
    assert "obstacle_trigger" in instruction and "behavioral_response" in instruction
    assert "every task" in instruction.lower()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "The decomposition prompt asks for an if-then plan on every task, but when the "
        "model leaves one out nothing fills it in: TaskChunk.implementation_intention is "
        "Optional and there is no default if-then. Needs a decision on the default "
        "plan's content."
    ),
)
@pytest.mark.asyncio
async def test_woop__model_omits_intentions__each_task_still_gets_an_if_then_plan(
    jarvis, llm, monday_morning
):
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"], with_intentions=False))

    turn = await jarvis.say("Plan my DSA revision")

    for task in jarvis.draft(turn.draft_id)["tasks"]:
        assert (task.get("implementation_intention") or {}).get("behavioral_response")


@pytest.mark.asyncio
async def test_pomodoro__every_proposed_task__is_25_minutes_or_less(jarvis, llm, monday_morning):
    _script_plan(llm, decomposition(SEVEN_TOPICS, duration=25))

    turn = await jarvis.say("Plan my revision of seven topics")

    tasks = turn.payload["schedule"]["schedule"].values()
    assert all(t["end_min"] - t["start_min"] <= 25 for t in tasks)
    assert all(t["duration_minutes"] <= 25 for t in jarvis.draft(turn.draft_id)["tasks"])


@pytest.mark.asyncio
async def test_pomodoro__model_proposes_a_40_minute_task__it_is_never_proposed(
    jarvis, llm, monday_morning
):
    _script_plan(llm, decomposition(["Read chapter 4"] * 5, duration=40))

    turn = await jarvis.say("Plan my reading")

    assert turn.draft_id is None
    assert jarvis.rows("draft_schedules") == []
    assert "try again" in turn.reply


# --- anti-guilt ---------------------------------------------------------------------


@pytest.mark.parametrize("overdue", [1, 2, 5])
def test_anti_guilt__missed_task_framing__has_no_blame(overdue):
    tasks = [{"task_id": f"t{i}", "title": f"Problem set {i}"} for i in range(overdue)]

    message = missed_deadlines.build_anti_guilt_message(tasks)

    assert message and blame_phrases_in(message) == []


REQUEUE_CLAIMS = ("recalibrated", "folded", "planning queue", "rescheduled", "back into")


@pytest.mark.parametrize("overdue", [1, 2, 5])
def test_anti_guilt__overdue_message__says_set_aside_and_claims_no_requeue(overdue):
    tasks = [{"task_id": f"t{i}", "title": f"Problem set {i}"} for i in range(overdue)]

    message = missed_deadlines.build_anti_guilt_message(tasks).lower()

    assert [claim for claim in REQUEUE_CLAIMS if claim in message] == []
    assert re.search(r"set (it|them) aside", message) and "nothing is lost" in message
    assert "say the word" in message and "this plan" in message


@pytest.mark.asyncio
async def test_anti_guilt__overdue_tasks__are_left_out_of_the_plan_as_the_message_says(
    jarvis, llm, monday_morning
):
    for i, title in enumerate(["Problem set 3", "Read chapter 2"]):
        jarvis.db.table("user_tasks").insert({
            "id": f"row-{i}", "user_id": "alice", "task_id": f"old_task_{i}", "goal_id": "old",
            "title": title, "status": "pending", "duration_minutes": 25,
            "deadline_hint": _day(-3),
        }).execute()
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    calls_before = len(llm.calls)

    turn = await jarvis.say("Plan my DSA revision")

    # The tasks really are set aside: neither the draft nor the schedule carries them.
    draft_titles = {t["title"] for t in jarvis.draft(turn.draft_id)["tasks"]}
    scheduled_titles = {t["title"] for t in turn.payload["schedule"]["schedule"].values()}
    assert not {"Problem set 3", "Read chapter 2"} & (draft_titles | scheduled_titles)
    # And the user is told exactly that, not that they were re-queued.
    (synthesis,) = [c for c in llm.calls[calls_before:] if c.task == "voice_of_jarvis_synthesis"]
    (framing,) = [line for line in synthesis.prompt.splitlines() if line.startswith("anti_guilt:")]
    assert re.search(r"set (it|them) aside", framing)
    assert [claim for claim in REQUEUE_CLAIMS if claim in framing.lower()] == []


def _seed_overdue(jarvis) -> None:
    """Two pending tasks three days past their deadline, as the table holds them."""
    for i, title in enumerate(["Problem set 3", "Read chapter 2"]):
        jarvis.db.table("user_tasks").insert({
            "id": f"row-{i}", "user_id": "alice", "task_id": f"old_task_{i}", "goal_id": "old",
            "title": title, "status": "pending", "duration_minutes": 25,
            "difficulty_weight": 0.5, "deadline_hint": _day(-3),
        }).execute()


@pytest.mark.asyncio
async def test_anti_guilt__user_asks_for_the_missed_tasks__they_come_back_into_the_next_draft(
    jarvis, llm, monday_morning
):
    """The overdue message offers to work set-aside tasks into the plan; asking does it."""
    _seed_overdue(jarvis)
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision")
    assert {r["status"] for r in jarvis.rows("user_tasks", goal_id="old")} == {"missed"}
    calls_before = len(llm.calls)

    turn = await jarvis.say("Yes please, add the missed tasks back in", session=planned.conversation_id)

    assert {r["status"] for r in jarvis.rows("user_tasks", goal_id="old")} == {"pending"}
    draft_titles = {t["title"] for t in jarvis.draft(turn.draft_id)["tasks"]}
    assert {"Problem set 3", "Read chapter 2"} <= draft_titles
    scheduled_titles = {t["title"] for t in turn.payload["schedule"]["schedule"].values()}
    assert {"Problem set 3", "Read chapter 2"} <= scheduled_titles
    (synthesis,) = [c for c in llm.calls[calls_before:] if c.task == "voice_of_jarvis_synthesis"]
    (framing,) = [line for line in synthesis.prompt.splitlines() if line.startswith("anti_guilt:")]
    assert "Problem set 3" in framing and "back" in framing
    assert blame_phrases_in(framing) == []


@pytest.mark.asyncio
async def test_anti_guilt__user_asks_for_the_missed_tasks_twice__they_stay_in_the_plan(
    jarvis, llm, monday_morning
):
    """The second ask finds nothing set aside; it must not set the restored tasks aside again."""
    _seed_overdue(jarvis)
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision")
    await jarvis.say("Yes please, add the missed tasks back in", session=planned.conversation_id)

    turn = await jarvis.say("And add the missed tasks back in, please", session=planned.conversation_id)

    assert {r["status"] for r in jarvis.rows("user_tasks", goal_id="old")} == {"pending"}
    scheduled_titles = {t["title"] for t in turn.payload["schedule"]["schedule"].values()}
    assert {"Problem set 3", "Read chapter 2"} <= scheduled_titles


@pytest.mark.parametrize("accept_first", [True, False], ids=["accepted", "not_yet_accepted"])
@pytest.mark.asyncio
async def test_anti_guilt__restored_tasks__next_plan_does_not_set_them_aside_again(
    jarvis, llm, monday_morning, accept_first
):
    """Bringing a task back is durable: its passed deadline no longer sets it aside."""
    _seed_overdue(jarvis)
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision")
    await jarvis.say("Yes please, add the missed tasks back in", session=planned.conversation_id)
    if accept_first:
        await jarvis.say("looks good", session=planned.conversation_id)

    turn = await jarvis.say("Plan my DSA revision")

    restored = [r for r in jarvis.rows("user_tasks") if r["title"] in ("Problem set 3", "Read chapter 2")]
    assert {r["status"] for r in restored} == {"pending"}
    scheduled_titles = {t["title"] for t in turn.payload["schedule"]["schedule"].values()}
    assert {"Problem set 3", "Read chapter 2"} <= scheduled_titles


@pytest.mark.parametrize(
    "message",
    ["Plan my DSA revision", "Don't add the missed tasks, just plan DSA"],
    ids=["no_ask", "declined"],
)
@pytest.mark.asyncio
async def test_anti_guilt__replan_without_asking__missed_tasks_stay_set_aside(
    jarvis, llm, monday_morning, message
):
    _seed_overdue(jarvis)
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision")

    turn = await jarvis.say(message, session=planned.conversation_id)

    assert {r["status"] for r in jarvis.rows("user_tasks", goal_id="old")} == {"missed"}
    titles = {t["title"] for t in turn.payload["schedule"]["schedule"].values()}
    assert not {"Problem set 3", "Read chapter 2"} & titles


@pytest.mark.parametrize(
    ("message", "asks"),
    [
        ("add the missed tasks back", True),
        ("Please include the missed ones", True),
        ("bring back what I missed", True),
        ("ok, work them in — the missed ones", True),
        ("bring the set-aside tasks back", True),
        ("reschedule my missed tasks", True),
        ("add the tasks I missed back", True),
        ("Yes please, add the missed tasks back in", True),
        ("I missed the bus", False),
        ("I missed the lecture, add a review session", False),
        ("add graphs to the plan", False),
        ("don't add the missed tasks", False),
        ("", False),
    ],
)
def test_asks_to_bring_back_missed__deterministic_phrases(message, asks):
    assert missed_deadlines.asks_to_bring_back_missed(message) is asks


@pytest.mark.asyncio
async def test_anti_guilt__planning_with_overdue_tasks__marks_them_missed_and_frames_it_kindly(
    jarvis, llm, monday_morning
):
    for i, title in enumerate(["Problem set 3", "Read chapter 2"]):
        jarvis.db.table("user_tasks").insert({
            "id": f"row-{i}", "user_id": "alice", "task_id": f"old_task_{i}", "goal_id": "old",
            "title": title, "status": "pending", "duration_minutes": 25,
            "deadline_hint": _day(-3),
        }).execute()
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    calls_before = len(llm.calls)

    await jarvis.say("Plan my DSA revision")

    assert {r["status"] for r in jarvis.rows("user_tasks", goal_id="old")} == {"missed"}
    # The user is told, through the reply's model, with the framing and its instruction.
    (synthesis,) = [c for c in llm.calls[calls_before:] if c.task == "voice_of_jarvis_synthesis"]
    framing = [line for line in synthesis.prompt.splitlines() if line.startswith("anti_guilt:")]
    assert len(framing) == 1, synthesis.prompt
    assert "Problem set 3" in framing[0] and "Read chapter 2" in framing[0]
    assert "never shaming" in framing[0]
    assert blame_phrases_in(framing[0]) == []


@pytest.mark.asyncio
async def test_anti_guilt__plan_no_horizon_can_hold__reply_is_the_scope_message_verbatim(
    jarvis, llm, monday_morning
):
    # Two tasks that each wait for the other: infeasible at every rung of the ladder.
    cyclic = decomposition(["Write intro", "Write conclusion"]).replace(
        '"dependencies": []', '"dependencies": ["task_2"]', 1
    ).replace('"dependencies": []', '"dependencies": ["task_1"]', 1)
    _script_plan(llm, cyclic)
    calls_before = len(llm.calls)

    turn = await jarvis.say("Plan my essay")

    assert "scope problem, not a you problem" in turn.reply
    assert blame_phrases_in(turn.reply) == []
    assert turn.draft_id is None
    # The honesty gate: no model gets to narrate this turn as a success.
    assert "voice_of_jarvis_synthesis" not in llm.tasks(since=calls_before)


@pytest.mark.asyncio
async def test_anti_guilt__coach_after_an_infeasible_plan__is_blame_free():
    from app.modules.coach import run_coaching_response

    out = await run_coaching_response({"error": "INFEASIBLE", "modules_invoked": []})

    assert "scope problem" in out["response_message"]
    assert blame_phrases_in(out["response_message"]) == []


@pytest.mark.asyncio
async def test_anti_guilt__solver_api_infeasible__422_says_scope_without_blame(monkeypatch):
    graph = {
        "goal_metadata": {},
        "decomposition": [
            {"task_id": f"t{i}", "title": f"Task {i}", "duration_minutes": 25,
             "difficulty_weight": 0.5, "completion_criteria": "done"}
            for i in range(3)
        ],
        "cognitive_load_estimate": {"intrinsic_load": 0.5},
    }
    body = {
        "graph": graph,
        "horizon_minutes": 60,
        "plan_start": HORIZON_START.isoformat(),
        "daily_context": [{"name": "Lectures", "start_min": 0, "end_min": 60, "availability": "blocked"}],
    }
    async with _api(monkeypatch) as api:
        response = await api.post("/api/v1/schedule/generate-schedule", json=body)

    assert response.status_code == 422
    assert "scope" in response.text
    assert blame_phrases_in(response.text) == []


@pytest.mark.asyncio
async def test_anti_guilt__skipping_tasks__every_reply_is_blame_free(jarvis, monkeypatch):
    for task_id, minutes in (("short", 25), ("long", 90)):
        jarvis.db.table("user_tasks").insert({
            "user_id": "alice", "task_id": task_id, "title": f"Revise {task_id} notes",
            "status": "pending", "duration_minutes": minutes, "difficulty_weight": 0.5,
        }).execute()

    replies = []
    async with _api(monkeypatch, jarvis) as api:
        for task_id in ("long", "short", "short", "short"):
            response = await api.post(f"/api/v1/tasks/{task_id}/skip", json={"user_id": "alice"})
            assert response.status_code == 200
            replies.append(response.json()["message"])
        jarvis.db.table("user_tasks").insert({
            "user_id": "alice", "task_id": "done", "title": "Revise done notes",
            "status": "completed", "duration_minutes": 25, "difficulty_weight": 0.5,
        }).execute()
        response = await api.post("/api/v1/tasks/done/skip", json={"user_id": "alice"})
        assert response.json()["status"] == "completed"
        replies.append(response.json()["message"])

    assert "tomorrow morning" in replies[0] and "buffer" in replies[1]  # both variants seen
    # Repeat skips record nothing: one outcome event per task.
    assert sorted(e["task_id"] for e in jarvis.rows("task_events", user_id="alice")) == [
        "long", "short"
    ]
    for text in replies:
        assert blame_phrases_in(text) == [], text


@pytest.mark.asyncio
async def test_anti_guilt__learned_skip_pattern__insight_and_note_are_blame_free(
    jarvis, embeddings, monkeypatch
):
    from app.services.memory.pearl import (
        detect_patterns,
        generate_proactive_insights,
        pearl_registry,
        register_default_patterns,
    )

    # The legacy detectors, for this test only: the app no longer registers them.
    monkeypatch.setattr(pearl_registry, "_entries", {})
    register_default_patterns()

    for i in range(3):
        jarvis.db.table("user_tasks").insert({
            "user_id": "alice", "task_id": f"t{i}", "status": "skipped",
            "scheduled_start": f"2026-11-1{i}T09:00:00+05:30",  # 9 AM on the user's clock
        }).execute()

    detect_patterns("alice", jarvis.db, jarvis.memory_store)

    (note,) = [m["content"] for m in jarvis.rows("user_memories", user_id="alice")]
    (insight,) = generate_proactive_insights("alice", jarvis.memory_store)
    assert "9 AM" in insight
    assert blame_phrases_in(note) == [] and blame_phrases_in(insight) == []


def test_anti_guilt__deprecated_v1_planner__every_user_facing_literal_is_blame_free():
    """The deprecated v1 planner (POST /chat) has its own INFEASIBLE, missed-deadline
    and replan replies. It cannot be driven offline without its whole pipeline, so its
    templates are checked where they are written: every string literal in the module
    except docstrings and the arguments of logging calls."""
    import ast
    import inspect

    from app.services.analytical import control_policy

    tree = ast.parse(inspect.getsource(control_policy))
    log_calls = {"debug", "info", "warning", "error", "exception", "critical", "log_step", "print"}
    skipped: set[int] = set()
    for node in ast.walk(tree):
        is_log = isinstance(node, ast.Call) and (
            getattr(node.func, "attr", None) in log_calls or getattr(node.func, "id", None) in log_calls
        )
        is_docstring = isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)
        if is_log or is_docstring:
            skipped.update(id(n) for n in ast.walk(node))
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skipped
    ]

    assert any("mathematically impossible" in text for text in literals)  # the sweep sees them
    assert [t for t in literals if blame_phrases_in(t)] == []


def test_anti_guilt__model_instructions__forbid_blame():
    from app.modules.coach import MASTERY_COACHING_SYSTEM_PROMPT
    from app.services.analytical.voice_of_jarvis import _build_voj_parts

    assert "Setbacks are data points, not failures" in MASTERY_COACHING_SYSTEM_PROMPT
    assert "\"Life happened\" not \"you missed\"" in MASTERY_COACHING_SYSTEM_PROMPT
    (line,) = _build_voj_parts({"anti_guilt_message": "Sir, 'X' slipped past its deadline."})
    assert "never apologetic, never shaming" in line


# --- mastery orientation ------------------------------------------------------------

PEER_TERMS = ("average", "other users", "others", "peers", "percentile", "ranking", "than most")


def _seed_progress(jarvis) -> None:
    for i, quality in enumerate([3, 4, 4, 5]):
        jarvis.db.table("user_tasks").insert({
            "user_id": "alice", "task_id": f"dsa_t{i}", "goal_id": "dsa", "title": f"DSA {i}",
            "status": "completed", "quality_score": quality, "reschedule_count": 0,
        }).execute()
    jarvis.db.table("user_tasks").insert({
        "user_id": "alice", "task_id": "dsa_t9", "goal_id": "dsa", "title": "DSA 9",
        "status": "pending", "reschedule_count": 1,
    }).execute()


@pytest.mark.asyncio
async def test_coach__progress_check__answered_from_own_mastery_data_never_peers(jarvis, llm):
    from app.modules.coach import MASTERY_COACHING_SYSTEM_PROMPT

    _seed_progress(jarvis)
    llm.script("voice_of_jarvis", "Your DSA mastery has climbed steadily, sir.")

    turn = await jarvis.say("how am I doing with my progress?")

    assert turn.intent == "CHECK_PROGRESS"
    assert turn.reply == "Your DSA mastery has climbed steadily, sir."  # not re-synthesised
    (coach_call,) = llm.calls_for("voice_of_jarvis")
    assert coach_call.system_prompt == MASTERY_COACHING_SYSTEM_PROMPT
    assert "NEVER compare to other users" in coach_call.system_prompt
    assert '"dsa": 0.' in coach_call.prompt  # the user's own mastery score for the goal
    assert "Tasks completed: 4" in coach_call.prompt
    assert not [term for term in PEER_TERMS if term in coach_call.prompt.lower()]


@pytest.mark.asyncio
async def test_coach__model_unavailable__fallback_speaks_of_progress_not_peers(jarvis, llm):
    _seed_progress(jarvis)

    def model_down(_req):
        raise LLMUnavailableError("down")

    llm.script("voice_of_jarvis", model_down)

    turn = await jarvis.say("how am I doing with my progress?")

    assert "progress" in turn.reply.lower()
    assert not [term for term in PEER_TERMS if term in turn.reply.lower()]
    assert blame_phrases_in(turn.reply) == []


# --- biology and the clock -------------------------------------------------------


def _wall_clock_hours(response_json: dict) -> list[tuple[datetime, datetime]]:
    start = datetime.fromisoformat(response_json["horizon_start"])
    return [
        (start + timedelta(minutes=t["start_min"]), start + timedelta(minutes=t["end_min"]))
        for t in response_json["schedule"].values()
    ]


def _overlaps_night(begin: datetime, end: datetime) -> bool:
    """Whether [begin, end) touches 00:00-08:00 on any calendar day it spans."""
    day = begin.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        if begin < day + timedelta(hours=8) and end > day:
            return True
        day += timedelta(days=1)
    return False


def _evening_only_request(daily_context_extra: list[dict]) -> dict:
    # Days are booked 08:00-23:00 and the user allows up to 10 h of work a day, so the
    # only thing keeping work out of the small hours is the sleep rule itself.
    booked = [
        {"name": f"Classes and work d{d}", "start_min": d * 1440, "end_min": d * 1440 + 900,
         "availability": "blocked"}
        for d in range(2)
    ]
    return {
        "graph": {
            "goal_metadata": {},
            "decomposition": [
                {"task_id": f"t{i}", "title": f"Task {i}", "duration_minutes": 25,
                 "difficulty_weight": 0.5, "completion_criteria": "done"}
                for i in range(4)
            ],
            "cognitive_load_estimate": {"intrinsic_load": 0.5},
        },
        "horizon_minutes": 2880,
        "plan_start": HORIZON_START.isoformat(),
        "max_daily_deep_work_minutes": 600,
        "daily_context": booked + daily_context_extra,
    }


@pytest.mark.asyncio
async def test_biology__no_sleep_habit__nothing_between_midnight_and_8am(monkeypatch):
    async with _api(monkeypatch) as api:
        response = await api.post(
            "/api/v1/schedule/generate-schedule", json=_evening_only_request([])
        )

    assert response.status_code == 200
    slots = _wall_clock_hours(response.json())
    assert len(slots) == 4
    assert not [s for s in slots if _overlaps_night(*s)], slots


@pytest.mark.asyncio
async def test_biology__own_sleep_habit__replaces_the_default_night(monkeypatch):
    # Sleeps 01:00-09:00: the hour after midnight is the user's to use.
    sleep = [
        {"name": f"Sleep d{d}", "start_min": d * 1440 + 1020, "end_min": d * 1440 + 1500,
         "availability": "blocked"}
        for d in range(2)
    ]
    async with _api(monkeypatch) as api:
        response = await api.post(
            "/api/v1/schedule/generate-schedule", json=_evening_only_request(sleep)
        )

    slots = _wall_clock_hours(response.json())
    assert any(begin.hour == 0 for begin, _end in slots)
    assert not [s for s in slots if 1 <= s[0].hour < 9]


@pytest.mark.asyncio
async def test_biology__sleep_habit__holds_on_every_day_of_a_seven_day_retry(
    jarvis, llm, clock, monkeypatch
):
    """Habit slots used to be expanded for the first attempt's 48 h only. On a retry
    out to 7 days, days 3+ had no sleep block, and because a sleep habit existed the
    default night was skipped too."""
    from fastapi import HTTPException

    clock.freeze(MONDAY_8AM, schedule_endpoint)
    jarvis.db.table("behavioral_constraints").insert({
        "user_id": "alice", "constraint_type": "habit", "raw_text": "I sleep from 11 PM to 7 AM",
    }).execute()
    llm.script("habit_translation", {"semantic_slots": [
        {"name": "Sleep", "start_min": 900, "end_min": 1380,  # 23:00-07:00 local
         "availability": "blocked", "recurrence": "daily"},
    ]})
    # Days are taken 08:00-23:00, so only 07:00-08:00 is free on each day before
    # the sleep habit is honoured.
    jarvis.memory_store.store_memory(
        "alice", {"type": "constraint", "content": "No study between 8 AM and 11 PM", "confidence": 0.9}
    )
    real_run_schedule = schedule_endpoint.run_schedule
    horizons: list[int] = []

    def only_a_week_fits(graph, daily_context, horizon_minutes, **kwargs):
        horizons.append(horizon_minutes)
        if horizon_minutes < 7 * MINUTES_PER_DAY:
            raise HTTPException(status_code=422, detail="Schedule infeasible")
        return real_run_schedule(graph, daily_context, horizon_minutes=horizon_minutes, **kwargs)

    monkeypatch.setattr(schedule_endpoint, "run_schedule", only_a_week_fits)
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))

    turn = await jarvis.say("Plan my DSA revision")

    assert horizons[-1] == 7 * MINUTES_PER_DAY  # the 7-day retry is the one that solved
    days = {t["start_min"] // MINUTES_PER_DAY for t in turn.payload["schedule"]["schedule"].values()}
    assert max(days) >= 2, days  # the plan reaches the days the first horizon did not cover
    in_sleep = [
        t for t in turn.payload["schedule"]["schedule"].values()
        if 900 <= t["start_min"] % MINUTES_PER_DAY < 1380
        or 900 < t["end_min"] % MINUTES_PER_DAY <= 1380
    ]
    assert in_sleep == [], in_sleep


@pytest.mark.asyncio
async def test_late_night__plan_made_at_3am__nothing_before_8am(jarvis, llm, clock):
    clock.freeze(datetime(2026, 11, 16, 3, 0, tzinfo=IST), schedule_endpoint)
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision")

    await jarvis.say("looks good", session=planned.conversation_id)

    starts = [datetime.fromisoformat(r["scheduled_start"]) for r in jarvis.rows("user_tasks")]
    assert len(starts) == 5
    assert min(starts) >= datetime(2026, 11, 16, 8, 0, tzinfo=IST)
    assert not [s for s in starts if s.astimezone(IST).hour < 8]


@pytest.mark.parametrize("hour", [14, 22])
@pytest.mark.asyncio
async def test_late_day__plan_made_after_8am__nothing_scheduled_in_the_past(
    jarvis, llm, clock, hour
):
    made_at = datetime(2026, 11, 16, hour, 0, tzinfo=IST)
    clock.freeze(made_at, schedule_endpoint)
    _script_plan(llm, decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision")

    await jarvis.say("looks good", session=planned.conversation_id)

    starts = [datetime.fromisoformat(r["scheduled_start"]) for r in jarvis.rows("user_tasks")]
    assert len(starts) == 5
    assert min(starts) >= made_at
