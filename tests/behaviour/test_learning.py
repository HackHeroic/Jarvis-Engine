"""Learning: Jarvis notices what the user does, and the next schedule changes because of it.

PEARL reads the outcome history (``task_events``, the rows TaskService writes for each
Complete, Skip and passed slot). It turns repeated set-asides at one local hour into a
learned avoidance pattern, from at least three outcomes at a rate of at least 70%,
never fewer, and tells the user in the turn it happens (a ``pattern_detected`` event).
The memory -> constraint bridge then turns a learned pattern, or a rule the user
stated, into solver constraints, so the next plan moves out of that window on every
day it covers. Dismissing a pattern hands the hour back for good: PEARL recognises the
dismissed pattern by its key and never learns it again.
"""

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.services.task_service as task_service
from app.domain.entities import TaskEvent, TaskEventType
from tests.behaviour.conftest import brain_dump, decomposition

# The user's clock (India): plans start at 08:00 local, the start of their day, and
# the skips PEARL learns from were at 09:00 local. (These were UTC times when the
# horizon was UTC-anchored.) PEARL reads the history of the 28 days before its clock,
# which is frozen with the planner's.
IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
FIVE_TOPICS = ["Graphs", "DP", "Trees", "Heaps", "Mocks"]


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    clock.freeze(MONDAY_8AM, schedule_endpoint, task_service)


# What TaskService adds to an outcome's metadata, besides the task's title, goal and deadline.
_OUTCOME_DETAILS = {
    TaskEventType.COMPLETED: {"actual_duration_minutes": None, "quality": None},
    TaskEventType.SKIPPED: {"reason": None},
    TaskEventType.MISSED: {},
}


async def _seed_history_at_nine(jarvis, outcomes: list[str], *, user: str = "alice") -> None:
    """What became of the slots the user was given at 09:00 on successive days: one
    outcome event per slot, as TaskService records it."""
    events = []
    for i, outcome in enumerate(outcomes):
        kind = TaskEventType(outcome)
        slot = datetime(2026, 11, 5 + i, 9, 0, tzinfo=IST)
        events.append(TaskEvent(
            user_id=user, task_id=f"past_{i}", event_type=kind,
            occurred_at=slot + timedelta(minutes=25),
            scheduled_start=slot, scheduled_end=slot + timedelta(minutes=25),
            metadata={
                "title": f"Morning task {i}", "goal_id": "past", "deadline_hint": None,
                **_OUTCOME_DETAILS[kind],
            },
        ))
    await jarvis.state.container.repos.task_events.append_many(user, events)


def _script_plan(llm, topics: list[str] = FIVE_TOPICS) -> None:
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA") if "plan" in req.prompt.lower() else "{}",
    )
    llm.script("socratic_chunker", decomposition(topics))


def _wall_clock(turn) -> list[tuple[datetime, datetime]]:
    schedule = turn.payload["schedule"]
    start = datetime.fromisoformat(schedule["horizon_start"])
    return sorted(
        (start + timedelta(minutes=t["start_min"]), start + timedelta(minutes=t["end_min"]))
        for t in schedule["schedule"].values()
    )


def _touches(slot: tuple[datetime, datetime], from_hour: int, to_hour: int) -> bool:
    begin, end = slot
    window_start = begin.replace(hour=from_hour, minute=0, second=0, microsecond=0)
    return begin < window_start + timedelta(hours=to_hour - from_hour) and end > window_start


def _patterns(turn) -> list[dict]:
    return [data for event, data in turn.events if event == "pattern_detected"]


@asynccontextmanager
async def _memory_api(jarvis, monkeypatch):
    from app.main import app

    monkeypatch.setattr(app.state, "container", jarvis.state.container, raising=False)
    monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --- PEARL: when is a habit a pattern? ---------------------------------------------


@pytest.mark.parametrize(
    "history, learned",
    [
        (["skipped"] * 3, True),                                  # 3 of 3
        (["skipped", "skipped", "skipped", "completed"], True),   # 3 of 4 = 75%
        (["skipped"] * 2, False),                                 # too few to call it
        (["skipped", "skipped", "completed"], False),             # 2 of 3 = 67%
    ],
    ids=["three_of_three", "three_of_four", "only_two", "two_of_three"],
)
@pytest.mark.asyncio
async def test_pearl__skips_at_nine__learned_only_from_enough_consistent_evidence(
    jarvis, llm, history, learned
):
    await _seed_history_at_nine(jarvis, history)

    turn = await jarvis.say("Anything I should know about my week?")

    patterns = jarvis.rows("user_memories", user_id="alice", memory_type="behavioral_pattern")
    if learned:
        (pattern,) = patterns
        assert "hour 9" in pattern["content"]
        assert pattern["confidence"] >= 0.7
        assert [p["content"] for p in _patterns(turn)] == [pattern["content"]]
    else:
        assert patterns == []
        assert _patterns(turn) == []


@pytest.mark.asyncio
async def test_pearl__learning_moment__carries_confidence_and_how_often_it_was_seen(jarvis, llm):
    await _seed_history_at_nine(jarvis, ["skipped"] * 3)

    turn = await jarvis.say("Anything I should know about my week?")

    (moment,) = _patterns(turn)
    assert moment["type"] == "skip_time_window"
    assert moment["confidence"] == 1.0
    assert moment["occurrence_count"] == 3


@pytest.mark.asyncio
async def test_pearl__another_users_skips__teach_me_nothing(jarvis, llm):
    await _seed_history_at_nine(jarvis, ["skipped"] * 3, user="bob")

    turn = await jarvis.say("Anything I should know about my week?", user="alice")

    assert _patterns(turn) == []
    assert jarvis.rows("user_memories", user_id="alice") == []


# --- the bridge: what was learned changes the next plan -----------------------------


def _script_rule(llm) -> None:
    def extract(req) -> str:
        user_line = re.search(r"^User: (.*)$", req.prompt, flags=re.MULTILINE).group(1)
        if "before 11" not in user_line:
            return '{"memories": []}'
        rule = {"type": "constraint", "content": "No study before 11 AM", "confidence": 0.9}
        return json.dumps({"memories": [rule]})

    llm.script("memory_extraction", extract)


@pytest.mark.asyncio
async def test_bridge__rule_the_user_stated__moves_every_day_of_the_next_plan_out_of_it(
    jarvis, llm
):
    _script_plan(llm)
    _script_rule(llm)
    before = await jarvis.say("Plan my DSA revision")
    assert any(_touches(slot, 8, 11) for slot in _wall_clock(before))  # the mornings were used

    await jarvis.say("Please never put study before 11 AM, I'm useless then")
    after = await jarvis.say("Plan my DSA revision")

    slots = _wall_clock(after)
    assert len({begin.date() for begin, _ in slots}) > 1  # the plan spans several days
    assert not [slot for slot in slots if _touches(slot, 8, 11)], slots


@pytest.mark.asyncio
async def test_bridge__learned_avoidance__next_plan_keeps_demanding_work_out_of_that_hour(
    jarvis, llm
):
    # Six 25-minute tasks over two days: three a day, 08:00-09:15 if nothing intervenes.
    _script_plan(llm, FIVE_TOPICS + ["Tries"])
    before = await jarvis.say("Plan my DSA revision")
    assert any(_touches(slot, 9, 10) for slot in _wall_clock(before))
    await _seed_history_at_nine(jarvis, ["skipped"] * 3)
    await jarvis.say("Anything I should know about my week?")  # PEARL learns here

    after = await jarvis.say("Plan my DSA revision")

    # Every task is 25 min at difficulty 0.5: too demanding for a minimal-work hour.
    slots = _wall_clock(after)
    assert len({begin.date() for begin, _ in slots}) > 1
    assert not [slot for slot in slots if _touches(slot, 9, 10)], slots


@pytest.mark.asyncio
async def test_bridge__dismissed_pattern__hands_the_hour_back(jarvis, llm, monkeypatch):
    _script_plan(llm)
    await _seed_history_at_nine(jarvis, ["skipped"] * 3)
    await jarvis.say("Anything I should know about my week?")
    (pattern,) = jarvis.rows("user_memories", user_id="alice", memory_type="behavioral_pattern")

    async with _memory_api(jarvis, monkeypatch) as api:
        dismissed = await api.post(
            f"/api/v1/memories/{pattern['id']}/dismiss", params={"user_id": "alice"}
        )
    after = await jarvis.say("Plan my DSA revision")
    again = await jarvis.say("Plan my DSA revision")

    assert dismissed.status_code == 200
    assert any(_touches(slot, 9, 10) for slot in _wall_clock(after))
    assert any(_touches(slot, 9, 10) for slot in _wall_clock(again))


# --- the focus profile: learned hours lean the plan, stated rules still win ---------


async def _register_container_with_evening_completions(jarvis, llm, count: int) -> None:
    """The composition root over the same database, its history holding ``count``
    completed 19:00 slots on the days before Monday."""
    import app.core.config as cfg
    from app.container import AppContainer
    from app.core import runtime
    from app.domain.entities import TaskEvent, TaskEventType
    from app.infrastructure.llm.chain import FallbackChainProvider
    from app.infrastructure.llm.factory import set_chain
    from app.orchestrator.hooks import ActionHooks

    container = AppContainer.build(supabase_client=jarvis.db, cfg=cfg, hooks=ActionHooks())
    runtime.set_container(container)  # conftest resets the registry afterwards
    set_chain(FallbackChainProvider([llm]))  # build installed the real chain; the script answers
    for day in range(1, count + 1):
        slot = (MONDAY_8AM - timedelta(days=day)).replace(hour=19)
        await container.repos.task_events.append("alice", TaskEvent(
            user_id="alice", task_id=f"evening_{day}", event_type=TaskEventType.COMPLETED,
            occurred_at=slot + timedelta(minutes=25), scheduled_start=slot,
            scheduled_end=slot + timedelta(minutes=25),
        ))


@pytest.mark.asyncio
async def test_energy__learned_evening_profile__teaching_a_rule_still_moves_the_schedule(
    jarvis, llm, clock, monkeypatch
):
    import app.modules.planning_graph as planning_graph
    from app.services.energy import HeuristicEnergyEstimator

    clock.freeze(MONDAY_8AM, schedule_endpoint, planning_graph)
    await _register_container_with_evening_completions(jarvis, llm, 12)
    focus_seen: list = []
    real_run_schedule = schedule_endpoint.run_schedule

    def spy(*args, **kwargs):
        focus_seen.append(kwargs.get("focus_by_hour"))
        return real_run_schedule(*args, **kwargs)

    monkeypatch.setattr(schedule_endpoint, "run_schedule", spy)
    _script_plan(llm)
    _script_rule(llm)

    await jarvis.say("Please never put study before 11 AM, I'm useless then")
    after = await jarvis.say("Plan my DSA revision")

    # The learned evening profile reached the solver on this turn...
    assert focus_seen and focus_seen[-1] is not None
    assert focus_seen[-1][19] > HeuristicEnergyEstimator().score(19)
    # ...and the rule the user stated still holds on every day of the plan.
    slots = _wall_clock(after)
    assert len({begin.date() for begin, _ in slots}) > 1
    assert [begin for begin, _ in slots if begin.astimezone(IST).hour < 11] == [], slots
