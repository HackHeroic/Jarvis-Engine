"""Learning: Jarvis notices what the user does, and the next schedule changes because of it.

PEARL turns repeated skips at one hour into a learned avoidance pattern — from at
least three observations at a skip rate of at least 70%, never fewer — and tells the
user in the turn it happens (a ``pattern_detected`` event). The memory -> constraint
bridge then turns a learned pattern, or a rule the user stated, into solver
constraints, so the next plan moves out of that window on every day it covers.
Dismissing a pattern hands the hour back.

The skip observations are ``user_tasks`` rows with status ``skipped``: what PEARL
counts today. (The Skip button writes ``pacing_pushed``, not ``skipped``; wiring it
into PEARL is the next plan's work, so these tests seed the status PEARL reads.)
"""

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
from tests.behaviour.conftest import brain_dump, decomposition

# The user's clock (India): plans start at 08:00 local, the start of their day, and
# the skips PEARL learns from were at 09:00 local. (These were UTC times when the
# horizon was UTC-anchored.)
IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
FIVE_TOPICS = ["Graphs", "DP", "Trees", "Heaps", "Mocks"]


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    clock.freeze(MONDAY_8AM, schedule_endpoint)


def _seed_history_at_nine(jarvis, statuses: list[str]) -> None:
    """Past tasks the user was given at 09:00 on successive days, and what became of them."""
    for i, status in enumerate(statuses):
        jarvis.db.table("user_tasks").insert({
            "user_id": "alice", "task_id": f"past_{i}", "goal_id": "past",
            "title": f"Morning task {i}", "status": status, "duration_minutes": 25,
            "scheduled_start": f"2026-11-{5 + i:02d}T09:00:00+05:30",
        }).execute()


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

    monkeypatch.setattr(app.state, "memory_store", jarvis.memory_store, raising=False)
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
    _seed_history_at_nine(jarvis, history)

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
    _seed_history_at_nine(jarvis, ["skipped"] * 3)

    turn = await jarvis.say("Anything I should know about my week?")

    (moment,) = _patterns(turn)
    assert moment["type"] == "skip_time_window"
    assert moment["confidence"] == 1.0
    assert moment["occurrence_count"] == 3


@pytest.mark.asyncio
async def test_pearl__another_users_skips__teach_me_nothing(jarvis, llm):
    for i in range(3):
        jarvis.db.table("user_tasks").insert({
            "user_id": "bob", "task_id": f"bob_{i}", "status": "skipped",
            "scheduled_start": f"2026-11-{5 + i:02d}T09:00:00+05:30",
        }).execute()

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
    _seed_history_at_nine(jarvis, ["skipped"] * 3)
    await jarvis.say("Anything I should know about my week?")  # PEARL learns here

    after = await jarvis.say("Plan my DSA revision")

    # Every task is 25 min at difficulty 0.5: too demanding for a minimal-work hour.
    slots = _wall_clock(after)
    assert len({begin.date() for begin, _ in slots}) > 1
    assert not [slot for slot in slots if _touches(slot, 9, 10)], slots


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Dismissing a learned pattern does not hand the hour back. PEARL stores a skip "
        "pattern at confidence 0.9; dismiss subtracts 0.3, leaving 0.6, which still meets "
        "the bridge's 0.6 pattern gate (inclusive by design; 0.9-0.3 is "
        "0.6000000000000001 > 0.6 in floats too). And with no record of the dismissal, "
        "PEARL re-detects the same skips on the next turn and reinforces the memory "
        "(+0.1). Making a dismissal stick needs a dismissed state PEARL respects."
    ),
)
@pytest.mark.asyncio
async def test_bridge__dismissed_pattern__hands_the_hour_back(jarvis, llm, monkeypatch):
    _script_plan(llm)
    _seed_history_at_nine(jarvis, ["skipped"] * 3)
    await jarvis.say("Anything I should know about my week?")
    (pattern,) = jarvis.rows("user_memories", user_id="alice", memory_type="behavioral_pattern")

    async with _memory_api(jarvis, monkeypatch) as api:
        dismissed = await api.post(
            f"/api/v1/memories/{pattern['id']}/dismiss", params={"user_id": "alice"}
        )
    after = await jarvis.say("Plan my DSA revision")

    assert dismissed.status_code == 200
    assert any(_touches(slot, 9, 10) for slot in _wall_clock(after))
