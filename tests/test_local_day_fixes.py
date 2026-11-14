"""Every "today" is the user's: the streak, spaced-repetition due dates, reschedule counts.

The streak and the reschedule count read the outcome history (``task_events``), not
task statuses, keeping only the latest outcome of each slot; a day is a date on the
user's clock. India is UTC+5:30, so 00:00-05:30 IST is still the previous UTC day.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain.entities import TaskEvent, TaskEventType
from app.domain.errors import PersistenceError
from app.domain.ports import TaskEventRepository
import app.core.config as config
from app.services.analytical.mastery_tracker import (
    NEUTRAL_SUCCESS_RATIO,
    OUTCOME_TYPES,
    calculate_success_ratio,
    compute_streak_from_events,
    reschedule_count_of,
    streak_as_of,
)
from app.services.analytical.sm2_engine import get_due_trackers
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

IST = ZoneInfo("Asia/Kolkata")


def _event(
    kind: str,
    occurred_at: datetime,
    *,
    task_id: str = "t1",
    user_id: str = "alice",
    goal_id: str | None = "g1",
    scheduled_start: datetime | None = None,
) -> TaskEvent:
    return TaskEvent(
        user_id=user_id,
        task_id=task_id,
        event_type=TaskEventType(kind),
        occurred_at=occurred_at,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_start + timedelta(minutes=25) if scheduled_start else None,
        metadata={"title": "Task", "goal_id": goal_id},
    )


def _container(supabase):
    import app.core.config as cfg
    from app.container import AppContainer
    from app.orchestrator.hooks import ActionHooks

    return AppContainer.build(
        supabase_client=supabase,
        cfg=cfg,
        hooks=ActionHooks(),
        embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )


def test_streak__completion_at_00_30_ist__counts_for_that_local_day():
    events = [
        # 00:30 IST on Mon 16 Nov is 19:00 UTC on Sun 15 Nov.
        _event("completed", datetime(2026, 11, 16, 0, 30, tzinfo=IST), task_id="late"),
        _event("completed", datetime(2026, 11, 15, 20, 0, tzinfo=IST), task_id="evening"),
        _event("completed", datetime(2026, 11, 14, 10, 0, tzinfo=IST), task_id="morning"),
        _event("skipped", datetime(2026, 11, 13, 10, 0, tzinfo=IST), task_id="set_aside"),
    ]

    assert compute_streak_from_events(events, IST, datetime(2026, 11, 16).date()) == 3
    # On UTC dates the 00:30 completion would sit on the 15th: the 16th would have none,
    # and the streak would count only the 15th and the 14th.
    assert compute_streak_from_events(events, ZoneInfo("UTC"), datetime(2026, 11, 16).date()) == 2
    assert compute_streak_from_events([], IST, datetime(2026, 11, 16).date()) == 0


# One streak, the coach's and GET /you's: consecutive local days with something
# completed. It is not broken until the local day ends: with nothing completed yet
# today, it counts back from yesterday.


def _completed_on(*days: int, hour: int = 10) -> list:
    return [
        _event("completed", datetime(2026, 11, day, hour, 0, tzinfo=IST), task_id=f"t{day}")
        for day in days
    ]


def test_streak__done_today__today_counts():
    assert compute_streak_from_events(_completed_on(16, 15), IST, date(2026, 11, 16)) == 2


def test_streak__nothing_yet_today__counts_back_from_yesterday():
    assert compute_streak_from_events(_completed_on(15, 14), IST, date(2026, 11, 16)) == 2


def test_streak__nothing_yesterday_or_today__zero():
    assert compute_streak_from_events(_completed_on(14, 13), IST, date(2026, 11, 16)) == 0


def test_streak__ist_00_30__yesterdays_evening_keeps_it_and_the_new_day_starts_on_ist():
    # 00:30 IST on the 17th is 19:00 UTC on the 16th: on the user's clock the 17th has
    # begun with nothing done, and the 16th and 15th still count.
    events = _completed_on(16, 15, hour=21)
    now = datetime(2026, 11, 17, 0, 30, tzinfo=IST)

    assert streak_as_of(events, IST, now) == 2
    # Done at 00:20 IST on the 17th: the 17th counts as a new day of the streak.
    early = [_event("completed", datetime(2026, 11, 17, 0, 20, tzinfo=IST), task_id="early")]
    assert streak_as_of(events + early, IST, now) == 3


@pytest.mark.asyncio
async def test_streak__reads_events_not_statuses(no_llm):
    from app.core.user_model import UserModel
    from app.modules.coach import run_coaching_response

    supabase = FakeSupabase()
    container = _container(supabase)
    today = datetime.now(IST).date()
    # Statuses say five days in a row ...
    for days_ago in range(5):
        day = today - timedelta(days=days_ago)
        supabase.table("user_tasks").insert({
            "user_id": "alice", "task_id": f"s{days_ago}", "goal_id": "g1",
            "title": "Task", "status": "completed", "duration_minutes": 25,
            "difficulty_weight": 0.5,  # the column's default
            "completed_at": datetime.combine(day, time(12), IST).isoformat(),
        }).execute()
    # ... the outcome history says today and yesterday.
    for days_ago in (0, 1):
        noon = datetime.combine(today - timedelta(days=days_ago), time(12), IST)
        await container.repos.task_events.append(
            "alice", _event("completed", noon, task_id=f"e{days_ago}", scheduled_start=noon)
        )
    await container.repos.task_events.append(  # another user's completion is not Alice's
        "bob", _event("completed", datetime.combine(today - timedelta(days=2), time(12), IST),
                      user_id="bob")
    )

    result = await run_coaching_response({
        "user_model": UserModel.for_planning("alice", container.planning),
        "container": container,  # the turn's composition root
        "user_message": "how am I doing?",
        "modules_invoked": [],
        "error": None,
    })

    assert result["coaching_data"]["streak"] == 2
    assert ("route_llm_call", "voice_of_jarvis") in no_llm


@pytest.mark.asyncio
async def test_reschedule_count__counts_skipped_events_for_goal():
    supabase = FakeSupabase()
    events = _container(supabase).repos.task_events
    now = datetime.now(timezone.utc)

    def slot(days_ago: int) -> datetime:
        return (now - timedelta(days=days_ago)).replace(microsecond=0)

    await events.append_many("alice", [
        _event("skipped", slot(3), task_id="a", scheduled_start=slot(3)),
        _event("skipped", slot(2), task_id="b", scheduled_start=slot(2)),
        _event("skipped", slot(2), task_id="c", goal_id="g2", scheduled_start=slot(2)),
        _event("completed", slot(1), task_id="d", scheduled_start=slot(1)),
        _event("scheduled", slot(1), task_id="e", scheduled_start=slot(1)),
        _event("rescheduled", slot(1), task_id="e", scheduled_start=slot(0)),
        # Set aside, then done after all in the same slot: what happened last counts.
        _event("skipped", slot(4), task_id="f", scheduled_start=slot(4)),
        _event("completed", slot(4) + timedelta(hours=3), task_id="f", scheduled_start=slot(4)),
        # Outside the history window: too old to count.
        _event("skipped", slot(60), task_id="g", scheduled_start=slot(60)),
    ])
    await events.append("bob", _event("skipped", slot(1), task_id="x", user_id="bob"))
    supabase.table("user_tasks").insert({  # the old column is never read
        "user_id": "alice", "task_id": "a", "goal_id": "g1", "title": "Task",
        "status": "pending", "duration_minutes": 25, "reschedule_count": 9,
    }).execute()

    reads_before = len(supabase.selects)

    async def recent(user: str) -> list[TaskEvent]:
        """The outcomes the mastery summary counts pushes over: the focus window's."""
        return await events.list_for_user(
            user, since=now - timedelta(days=config.FOCUS_WINDOW_DAYS), types=set(OUTCOME_TYPES)
        )

    alice = await recent("alice")
    assert reschedule_count_of(alice, "g1") == 2
    assert reschedule_count_of(alice, "g2") == 1
    assert reschedule_count_of(alice) == 3
    assert reschedule_count_of(await recent("carol"), "g1") == 0
    # Through the repository: task_events only, never the old user_tasks column.
    assert {table for table, _ in supabase.selects[reads_before:]} == {"task_events"}


class _EventsUnreachable(TaskEventRepository):
    async def append(self, user_id, event):
        raise NotImplementedError

    async def append_many(self, user_id, events):
        raise NotImplementedError

    async def list_for_user(self, user_id, **filters):
        raise PersistenceError("Could not reach the database.")


@pytest.mark.asyncio
async def test_success_ratio__history_unreadable__logged_and_neutral(caplog):
    """An outcome history that cannot be read is logged and read as none: the signal
    takes its neutral value (the coach's success ratio, as every mastery signal)."""
    assert await calculate_success_ratio("alice", "g1", _EventsUnreachable()) == (
        NEUTRAL_SUCCESS_RATIO
    )
    assert "Success ratio" in caplog.text


@pytest.mark.asyncio
async def test_due_trackers__ist_morning__uses_local_date():
    supabase = FakeSupabase({"habit_trackers": [
        # Done 10:00 IST Sun 15 Nov, every day: due Mon 16 Nov on the user's clock.
        {"id": "daytime", "user_id": "alice", "next_interval_days": 1,
         "last_done_at": "2026-11-15T04:30:00+00:00"},
        # Done 01:30 IST Mon 16 Nov (still the 15th in UTC): not due again until the 17th.
        {"id": "small_hours", "user_id": "alice", "next_interval_days": 1,
         "last_done_at": "2026-11-15T20:00:00+00:00"},
        {"id": "bobs", "user_id": "bob", "next_interval_days": 1,
         "last_done_at": "2026-11-15T04:30:00+00:00"},
    ]})
    # 03:00 IST on Mon 16 Nov is 21:30 UTC on Sun 15 Nov.
    morning = datetime(2026, 11, 16, 3, 0, tzinfo=IST)

    trackers = _container(supabase).repos.habit_trackers

    due = await get_due_trackers(trackers, "alice", IST, as_of_date=morning)
    on_utc = await get_due_trackers(trackers, "alice", ZoneInfo("UTC"), as_of_date=morning)

    assert [t["id"] for t in due] == ["daytime"]
    assert on_utc == []  # on UTC dates it is still the 15th: nothing would be due yet


@pytest.mark.asyncio
async def test_due_trackers_api__zone_from_the_profile(monkeypatch):
    import httpx

    from app.main import app

    supabase = FakeSupabase()
    container = _container(supabase)
    await container.repos.profiles.update("alice", timezone="America/New_York")
    seen: dict = {}

    async def fake_due(trackers, user_id, zone, *, as_of_date=None):
        seen.update(trackers=trackers, user_id=user_id, zone=zone)
        return []

    monkeypatch.setattr("app.api.v1.endpoints.habits.get_due_trackers", fake_due)
    monkeypatch.setattr(app.state, "container", container, raising=False)
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(supabase), raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        resp = await api.get("/api/v1/habits/tracker/due", params={"user_id": "alice"})

    assert resp.status_code == 200
    assert seen == {
        "trackers": container.repos.habit_trackers,
        "user_id": "alice",
        "zone": ZoneInfo("America/New_York"),
    }
