"""Anticipation: Jarvis makes the first move.

The user sets aside the 09:00 task three mornings running. PEARL notices, and Jarvis
offers, without being asked, to stop putting demanding work there. The user accepts:
the rule is stored with its hour already worked out, and every plan after it keeps
09:00 free, with no model spent translating the rule back into time.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.services.task_service as task_service
from tests.behaviour.conftest import brain_dump, decomposition, settle_background_tasks

IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
# Nine light 15-minute tasks: pacing spreads them over two days, five then four,
# 08:00-09:15 on the first if nothing intervenes. Each is short and easy enough for a
# minimal-work hour, so only a hard rule keeps 09:00 free of them.
LIGHT_TOPICS = ["Arrays", "Hashing", "Stacks", "Queues", "Graphs", "DP", "Trees", "Heaps", "Tries"]


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    clock.freeze(MONDAY_8AM, schedule_endpoint, task_service)


@asynccontextmanager
async def _api(jarvis, monkeypatch):
    from app.main import app

    monkeypatch.setattr(app.state, "container", jarvis.state.container, raising=False)
    monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _three_mornings_at_nine(jarvis) -> list[str]:
    """A plan the user accepted last week: a task at 09:00 on each of three mornings."""
    container = jarvis.state.container
    first_day = MONDAY_8AM - timedelta(days=3)
    task_ids = [f"lastweek_{day}" for day in range(3)]
    draft = await container.repos.drafts.create(
        "alice",
        tasks=[
            {"task_id": task_id, "title": f"Morning review {day}", "duration_minutes": 25,
             "difficulty_weight": 0.6, "goal_id": "lastweek"}
            for day, task_id in enumerate(task_ids)
        ],
        horizon_start=first_day.isoformat(),
        goal_id="lastweek",
        schedule={
            task_id: {"start_min": 60 + day * 1440, "end_min": 85 + day * 1440}
            for day, task_id in enumerate(task_ids)
        },
    )
    await container.draft_service.accept("alice", draft.id)
    return task_ids


def _script_plan(llm) -> None:
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA") if "plan" in req.prompt.lower() else "{}",
    )
    llm.script("socratic_chunker", decomposition(LIGHT_TOPICS, duration=15, difficulty=0.3))


def _wall_clock(turn) -> list[tuple[datetime, datetime]]:
    schedule = turn.payload["schedule"]
    start = datetime.fromisoformat(schedule["horizon_start"])
    return sorted(
        (start + timedelta(minutes=t["start_min"]), start + timedelta(minutes=t["end_min"]))
        for t in schedule["schedule"].values()
    )


def _touches_nine(slot: tuple[datetime, datetime]) -> bool:
    begin, end = (moment.astimezone(IST) for moment in slot)
    nine = begin.replace(hour=9, minute=0, second=0, microsecond=0)
    return begin < nine + timedelta(hours=1) and end > nine


@pytest.mark.asyncio
async def test_anticipation__three_skips_at_nine__rule_suggested_accepted_and_next_plan_keeps_nine_free(
    jarvis, llm, monkeypatch
):
    import app.core.config as config

    # Nine interchangeable tasks leave CP-SAT many equally good orders to rule out: it
    # finds the best packing at once and would spend its whole 10 s cap proving it.
    # The best found in 2 s is that packing, and a hard rule holds in any schedule.
    monkeypatch.setattr(config, "SOLVER_MAX_TIME_SECONDS", 2.0)
    _script_plan(llm)
    task_ids = await _three_mornings_at_nine(jarvis)

    async with _api(jarvis, monkeypatch) as api:
        for task_id in task_ids:
            skipped = await api.post(f"/api/v1/tasks/{task_id}/skip", json={"user_id": "alice"})
            assert skipped.status_code == 200
        await settle_background_tasks()  # PEARL looks after each outcome; Jarvis speaks up
        listed = await api.get("/api/v1/anticipation/suggestions", params={"user_id": "alice"})

        (proposal,) = listed.json()["suggestions"]
        assert proposal["title"] == "A pattern around 9 AM"
        assert proposal["body"] == (
            "You've set aside 3 of the last 3 tasks around 9 AM."
            " Want me to stop putting demanding work there?"
        )
        # Learned, not yet a rule: the 09:00 hour is only a minimal-work hour, which
        # light tasks may still use.
        before = await jarvis.say("Plan my DSA revision")
        assert any(_touches_nine(slot) for slot in _wall_clock(before))

        accepted = await api.post(
            f"/api/v1/anticipation/suggestions/{proposal['id']}/accept",
            params={"user_id": "alice"},
        )

    assert accepted.status_code == 200
    assert accepted.json()["result"]["message"] == (
        "Saved. Every plan from now on keeps 9 AM free."
    )
    (rule,) = jarvis.rows("behavioral_constraints", user_id="alice")
    assert rule["source"] == "inferred"
    assert rule["structured_semantics"]["semantic_slots"][0]["availability"] == "blocked"

    calls_before = len(llm.calls)
    after = await jarvis.say("Plan my DSA revision")

    slots = _wall_clock(after)
    assert len(slots) == len(LIGHT_TOPICS)
    assert not [slot for slot in slots if _touches_nine(slot)], slots
    assert "habit_translation" not in llm.tasks(calls_before)  # the rule's hour was known
