"""Rule check-ins: a rule taught in chat is checked on the next day, spaced out, and goes
with the rule.

Infer → confirm → adapt, applied to the rules themselves: the Habits page asks "Still
how you work?" about each rule Jarvis learned, first the day after it was learned,
then less and less often while the answer is yes; a low answer brings it back the next
day. The whole loop runs through the real chat turn (the rule is stored by the
``store_constraint`` node) and the real Habits routes over the same fake database.
"""

from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.domain.clock as utc_clock
from tests.behaviour.conftest import brain_dump, said

IST = ZoneInfo("Asia/Kolkata")
MONDAY_9PM = datetime(2026, 11, 16, 21, 0, tzinfo=IST)
RULE = "Never work after 10 PM"


@asynccontextmanager
async def _habits_api(jarvis, monkeypatch):
    """The app's REST routes over the harness's composition root and database."""
    from app.main import app

    monkeypatch.setattr(app.state, "container", jarvis.state.container, raising=False)
    monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _check_ins(api) -> dict:
    resp = await api.get("/api/v1/habits/tracker/due", params={"user_id": "alice"})
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_rule_taught_in_chat__checked_on_the_next_day_spaced_out_and_gone_with_the_rule(
    jarvis, llm, clock, monkeypatch
):
    clock.freeze(MONDAY_9PM, utc_clock)
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(inline_habits=[RULE]) if "10 PM" in said(req) else "{}",
    )

    turn = await jarvis.say("Never work after 10 PM, please")
    (rule,) = jarvis.rows("behavioral_constraints", user_id="alice")
    async with _habits_api(jarvis, monkeypatch) as api:
        tonight = await _check_ins(api)
        clock.advance(days=1)  # Tuesday 21:00
        tuesday = await _check_ins(api)
        (due,) = tuesday["due_trackers"]
        answered = await api.post(
            f"/api/v1/habits/tracker/{due['id']}/complete",
            params={"user_id": "alice"}, json={"quality": 5},
        )
        after = await _check_ins(api)
        clock.advance(days=1)  # Wednesday: due again, and still holding
        await api.post(
            f"/api/v1/habits/tracker/{due['id']}/complete",
            params={"user_id": "alice"}, json={"quality": 4},
        )
        spaced = await _check_ins(api)
        deleted = await api.delete(
            f"/api/v1/habits/constraints/{rule['id']}", params={"user_id": "alice"}
        )
        gone = await _check_ins(api)

    assert turn.payload["saved_constraints"] == [RULE]
    # Taught on Monday night: not asked about that night, first asked on Tuesday.
    assert tonight["due_trackers"] == []
    (later,) = tonight["upcoming_trackers"]
    assert (later["constraint_id"], later["rule_text"], later["source"]) == (
        rule["id"], RULE, "said"
    )
    assert (later["next_due_on"], later["last_reviewed_at"]) == ("2026-11-17", None)
    assert due["id"] == later["id"]
    assert tuesday["upcoming_trackers"] == []
    # Answered: back the next day (the first SM-2 interval), then six days on.
    assert answered.status_code == 200
    assert answered.json()["next_due_on"] == "2026-11-18"
    assert after["due_trackers"] == []
    assert after["upcoming_trackers"][0]["next_due_on"] == "2026-11-18"
    assert spaced["upcoming_trackers"][0]["next_due_on"] == "2026-11-24"
    assert spaced["upcoming_trackers"][0]["interval_days"] == 6
    # Deleting the rule takes its check-ins: nothing listed, no tracker left.
    assert deleted.status_code == 200
    assert gone["due_trackers"] == gone["upcoming_trackers"] == []
    assert jarvis.rows("habit_trackers", user_id="alice") == []
