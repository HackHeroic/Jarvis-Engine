"""Plans are anchored to the user's local day, not to UTC.

The solver counts minutes from ``horizon_start`` and the frontend shows
``horizon_start + minutes`` in the browser's local time, so the instant minute 0
stands for decides every wall-clock time the user sees. It is 08:00 in the user's
zone (their profile's ``timezone``, else ``DEFAULT_TIMEZONE``) on their logical
day. Every test freezes the clock at a local time in India (UTC+5:30), where a
UTC-anchored day starts at 13:30 and its "midnight-08:00" sleep block is
05:30-13:30.
"""

import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
from app.domain.events import NullEventBus
from app.infrastructure.persistence.supabase_repos import SupabaseDraftRepository
from app.services.draft_service import DraftService
from tests.behaviour.conftest import brain_dump, decomposition

IST = ZoneInfo("Asia/Kolkata")
MONDAY = date(2026, 11, 16)
TOPICS = ["Graphs", "DP", "Trees", "Heaps", "Mocks", "Tries", "Sorting", "Hashing"]


def _at(clock, hour: int, minute: int = 0, day: date = MONDAY) -> datetime:
    when = datetime.combine(day, time(hour, minute), tzinfo=IST)
    clock.freeze(when, schedule_endpoint)
    return when


def _script_plan(llm, topics: list[str] = TOPICS[:5]) -> None:
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA") if "plan" in req.prompt.lower() else "{}",
    )
    llm.script("socratic_chunker", decomposition(topics))


def _local_slots(turn, zone: ZoneInfo = IST) -> list[tuple[datetime, datetime]]:
    """Every task's (start, end) as the user's clock shows it, earliest first."""
    schedule = turn.payload["schedule"]
    start = datetime.fromisoformat(schedule["horizon_start"])
    return sorted(
        (
            (start + timedelta(minutes=t["start_min"])).astimezone(zone),
            (start + timedelta(minutes=t["end_min"])).astimezone(zone),
        )
        for t in schedule["schedule"].values()
    )


def _in_the_night(slot: tuple[datetime, datetime]) -> bool:
    """Does the task touch 00:00-08:00 local on the day it starts or ends?"""
    begin, end = slot
    return begin.hour < 8 or (end.hour < 8 and end.time() != time(0, 0))


@pytest.mark.asyncio
async def test_local_day__plan_at_nine_ist__first_task_at_or_after_nine_the_same_day(
    jarvis, llm, clock
):
    now = _at(clock, 9, 0)
    _script_plan(llm)

    turn = await jarvis.say("Plan my DSA revision")

    horizon_start = datetime.fromisoformat(turn.payload["schedule"]["horizon_start"])
    assert horizon_start.utcoffset() == timedelta(hours=5, minutes=30)  # offset-aware, local
    assert horizon_start == datetime.combine(MONDAY, time(8, 0), tzinfo=IST)
    first_start, _ = _local_slots(turn)[0]
    assert first_start.date() == MONDAY
    assert first_start >= now
    assert not [s for s in _local_slots(turn) if _in_the_night(s)]


@pytest.mark.asyncio
async def test_local_day__never_before_11_am__holds_in_local_time_on_every_day(jarvis, llm, clock):
    _at(clock, 7, 30)
    _script_plan(llm, TOPICS)

    def extract(req) -> str:
        user_line = re.search(r"^User: (.*)$", req.prompt, flags=re.MULTILINE).group(1)
        if "before 11" not in user_line:
            return '{"memories": []}'
        rule = {"type": "constraint", "content": "No study before 11 AM", "confidence": 0.9}
        return json.dumps({"memories": [rule]})

    llm.script("memory_extraction", extract)
    await jarvis.say("Please never put study before 11 AM, I'm useless then")

    turn = await jarvis.say("Plan my DSA revision")

    slots = _local_slots(turn)
    assert len({begin.date() for begin, _ in slots}) > 1  # the rule is tested on several days
    assert all(begin.time() >= time(11, 0) for begin, _ in slots), slots
    # And it is 11 AM on the user's clock: read as 11:00 UTC it blocked until 16:30 IST.
    assert min(begin.time() for begin, _ in slots) == time(11, 0)


@pytest.mark.asyncio
async def test_local_day__plan_at_half_past_one_ist__yesterdays_day_and_nothing_before_eight(
    jarvis, llm, clock
):
    now = _at(clock, 1, 30)
    _script_plan(llm)

    turn = await jarvis.say("Plan my DSA revision")

    horizon_start = datetime.fromisoformat(turn.payload["schedule"]["horizon_start"])
    # Before 08:00 the logical day is still yesterday's.
    assert horizon_start == datetime.combine(MONDAY - timedelta(days=1), time(8, 0), tzinfo=IST)
    slots = _local_slots(turn)
    assert all(begin >= datetime.combine(MONDAY, time(8, 0), tzinfo=IST) for begin, _ in slots)
    assert all(begin >= now for begin, _ in slots)


@pytest.mark.parametrize(("hour", "minute"), [(22, 30), (23, 0)], ids=["22:30", "23:00"])
@pytest.mark.asyncio
async def test_local_day__plan_late_in_the_evening__nothing_that_night__starts_at_eight_next_day(
    jarvis, llm, clock, hour, minute
):
    _at(clock, hour, minute)
    _script_plan(llm)

    turn = await jarvis.say("Plan my DSA revision")

    # From the evening cutoff (22:00) a plan starts on the next local day: a
    # UTC-anchored day put these at 23:00-05:30 IST, and a local one still put the
    # first task at 23:00.
    tuesday_8am = datetime.combine(MONDAY + timedelta(days=1), time(8, 0), tzinfo=IST)
    horizon_start = datetime.fromisoformat(turn.payload["schedule"]["horizon_start"])
    assert horizon_start == tuesday_8am
    slots = _local_slots(turn)
    assert all(begin >= tuesday_8am for begin, _ in slots), slots
    assert not [s for s in slots if _in_the_night(s)], slots


@pytest.mark.asyncio
async def test_local_day__accepted_plan__scheduled_start_round_trips_to_the_local_time(
    jarvis, llm, clock
):
    _at(clock, 9, 0)
    _script_plan(llm)
    turn = await jarvis.say("Plan my DSA revision")
    shown = {
        t["title"]: (datetime.fromisoformat(turn.payload["schedule"]["horizon_start"])
                     + timedelta(minutes=t["start_min"]))
        for t in turn.payload["schedule"]["schedule"].values()
    }

    await DraftService(SupabaseDraftRepository(jarvis.db), NullEventBus()).accept(
        "alice", turn.draft_id
    )

    rows = jarvis.rows("user_tasks", user_id="alice")
    assert len(rows) == len(shown)
    stored = {row["title"]: datetime.fromisoformat(row["scheduled_start"]) for row in rows}
    assert all(s.utcoffset() is not None for s in stored.values())  # instants, not wall times
    assert stored == shown
    # Planned at 09:00 IST, the first task is at 09:00 on the user's clock.
    assert min(s.astimezone(IST) for s in stored.values()) == datetime.combine(
        MONDAY, time(9, 0), tzinfo=IST
    )


@pytest.mark.asyncio
async def test_local_day__profile_timezone__the_plan_runs_on_that_zones_clock(jarvis, llm, clock):
    new_york = ZoneInfo("America/New_York")
    jarvis.db.table("user_profiles").insert(
        {"user_id": "alice", "timezone": "America/New_York"}
    ).execute()
    now = datetime.combine(MONDAY, time(9, 0), tzinfo=new_york)
    clock.freeze(now, schedule_endpoint)
    _script_plan(llm)

    turn = await jarvis.say("Plan my DSA revision")

    horizon_start = datetime.fromisoformat(turn.payload["schedule"]["horizon_start"])
    assert horizon_start == datetime.combine(MONDAY, time(8, 0), tzinfo=new_york)
    slots = _local_slots(turn, new_york)
    assert slots[0][0] == now  # the first task at 09:00 New York time
    assert not [s for s in slots if _in_the_night(s)], slots
