"""The user's clock: which zone their day runs in, and what "today" means in it."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.core.config as config
from app.domain.entities import UserProfile
from app.services.planning.local_time import logical_day_start, user_zone

IST = ZoneInfo("Asia/Kolkata")


# --- user_zone -----------------------------------------------------------------------------


def test_user_zone__no_profile__the_configured_default():
    assert user_zone() == IST


def test_user_zone__profile__its_timezone():
    assert user_zone(UserProfile(user_id="u1", timezone="Europe/Berlin")) == ZoneInfo("Europe/Berlin")


def test_user_zone__profile_names_no_real_zone__the_default():
    assert user_zone(UserProfile(user_id="u1", timezone="Mars/Olympus_Mons")) == IST


def test_user_zone__default_misconfigured__utc_rather_than_a_crash(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_TIMEZONE", "Not/AZone")

    assert user_zone() == ZoneInfo("UTC")


# --- logical_day_start ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_now", "expected_local_day"),
    [
        # 09:00 IST on the 16th: today's day, started at 08:00 IST.
        (datetime(2026, 11, 16, 3, 30, tzinfo=timezone.utc), 16),
        # 01:30 IST on the 16th (still the 15th in UTC): yesterday's day, the 15th.
        (datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc), 15),
        # 07:59 IST: still yesterday's day.
        (datetime(2026, 11, 16, 2, 29, tzinfo=timezone.utc), 15),
        # 21:59 IST on the 16th: still today's day.
        (datetime(2026, 11, 16, 16, 29, tzinfo=timezone.utc), 16),
        # 22:00 IST on the 16th: the evening cutoff, so tomorrow's day from 08:00.
        (datetime(2026, 11, 16, 16, 30, tzinfo=timezone.utc), 17),
        # 23:00 IST on the 16th: tomorrow's day; nothing is planned that night.
        (datetime(2026, 11, 16, 17, 30, tzinfo=timezone.utc), 17),
        # 23:30 IST on the 16th (18:00 UTC): tomorrow's day.
        (datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc), 17),
    ],
)
def test_logical_day_start__india__eight_am_local_on_the_logical_day(utc_now, expected_local_day):
    start = logical_day_start(utc_now, IST)

    assert start == datetime(2026, 11, expected_local_day, 8, 0, tzinfo=IST)
    assert start.utcoffset() == timedelta(hours=5, minutes=30)


def test_logical_day_start__naive_now__read_as_local_wall_time():
    assert logical_day_start(datetime(2026, 11, 16, 9, 0), IST) == datetime(2026, 11, 16, 8, 0, tzinfo=IST)


# --- run_schedule: the late-night rule and the night block, in local time ------------------


def _graph(n: int = 5):
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata, TaskChunk

    chunks = [TaskChunk(task_id=f"t{i}", title=f"part {i}", duration_minutes=25,
                        difficulty_weight=0.5, dependencies=[], completion_criteria="done")
              for i in range(n)]
    return ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                          cognitive_load_estimate={"intrinsic_load": 0.5})


def test_run_schedule__planned_at_nine_ist__horizon_and_tasks_on_the_local_clock(clock):
    from app.api.v1.endpoints import schedule as sep

    clock.freeze(datetime(2026, 11, 16, 9, 0, tzinfo=IST), sep)

    resp = sep.run_schedule(_graph(), [], horizon_minutes=2880)

    assert resp.horizon_start == datetime(2026, 11, 16, 8, 0, tzinfo=IST)
    # 09:00 is minute 60: nothing before it, and nothing in 00:00-08:00 local.
    assert min(t.start_min for t in resp.schedule.values()) >= 60
    assert all(not 960 <= t.start_min % 1440 < 1440 for t in resp.schedule.values())


def test_generate_schedule__naive_plan_start__eight_am_local_on_that_date():
    from app.api.v1.endpoints import schedule as sep

    assert sep._compute_horizon_start(datetime(2026, 11, 16, 15, 0), IST) == datetime(
        2026, 11, 16, 8, 0, tzinfo=IST
    )


# --- what "today" and "this hour" mean elsewhere -------------------------------------------


@pytest.mark.asyncio
async def test_missed_deadlines__date_only_deadline__missed_after_local_midnight(clock):
    """A deadline of the 15th ends at 23:59 IST on the 15th, not at 05:29 IST on the 16th."""
    import app.services.analytical.missed_deadlines as md
    from tests.fakes import FakeSupabase

    db = FakeSupabase({"user_tasks": [
        {"id": 1, "user_id": "u1", "task_id": "t1", "title": "Essay",
         "status": "pending", "deadline_hint": "2026-11-15"},
    ]})

    clock.freeze(datetime(2026, 11, 15, 23, 30, tzinfo=IST), md)
    assert await md.detect_and_mark_missed("u1", db) == []

    clock.freeze(datetime(2026, 11, 16, 0, 30, tzinfo=IST), md)  # 19:00 UTC on the 15th
    (missed,) = await md.detect_and_mark_missed("u1", db)
    assert missed["task_id"] == "t1"


def test_pearl__skips_at_nine_local__counted_as_hour_nine():
    from app.services.memory.pearl import detect_skip_time_window

    class _Store:
        def __init__(self):
            self.stored = []

        def find_similar_memory(self, *_a, **_k):
            return None

        def store_memory(self, user_id, memory):
            self.stored.append(memory)

    tasks = [
        {"task_id": f"t{i}", "status": "skipped",
         "scheduled_start": f"2026-11-{5 + i:02d}T03:30:00+00:00"}  # 09:00 IST
        for i in range(3)
    ]
    store = _Store()

    detect_skip_time_window("u1", tasks, store)

    (memory,) = store.stored
    assert "hour 9" in memory["content"]


def test_energy__read_on_the_users_clock(monkeypatch):
    import asyncio

    import app.core.user_model as um

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(um, "datetime", _Frozen)

    energy = asyncio.run(um.UserModel(user_id="u1", db=None).get_estimated_energy())

    assert energy == 0.9  # 10:00 local is the morning peak; 04:30 would read as 0.3


def test_ingestion__deadline_today_in_local_time__is_not_a_future_deadline(monkeypatch):
    """Ingestion keeps deadlines after "today"; today is the user's date, not UTC's."""
    from app.services.planning.local_time import local_today

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)  # 01:30 IST on the 16th
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    import app.services.planning.local_time as lt

    monkeypatch.setattr(lt, "datetime", _Frozen)

    assert local_today().isoformat() == "2026-11-16"


# --- accepted tasks carry instants ---------------------------------------------------------


@pytest.mark.parametrize(
    ("horizon_start", "expected_start"),
    [
        ("2026-11-16T08:00:00+05:30", "2026-11-16T09:00:00+05:30"),
        # Written before the horizon was local: no offset meant UTC.
        ("2026-11-16T08:00:00", "2026-11-16T09:00:00+00:00"),
    ],
)
def test_build_task_rows__horizon_start__scheduled_start_is_an_offset_aware_instant(
    horizon_start, expected_start
):
    from app.services.planning.task_rows import build_task_rows

    _, (row,) = build_task_rows(
        "u1",
        [{"task_id": "g_t1", "title": "Graphs", "duration_minutes": 25, "goal_id": "g"}],
        schedule={"g_t1": {"start_min": 60, "end_min": 85}},
        horizon_start=horizon_start,
    )

    assert row["scheduled_start"] == expected_start
    assert datetime.fromisoformat(row["scheduled_end"]) - datetime.fromisoformat(
        row["scheduled_start"]
    ) == timedelta(minutes=25)
