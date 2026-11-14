"""The replanner: today's plan redrafted from the tasks the user already has.

``PlanningGraphReplanner`` runs the real planning sub-graph in replan mode over
``FakeSupabase``, with the real CP-SAT solver: no goal, so no decomposition; the
user's pending tasks, plus the set-aside ones it is asked to re-slot. The only model
call it can make is PRIMARY habit translation, and only for habits written as free
text; a learned rule arrives with its slots already worked out.
"""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
from app.domain.entities import (
    BehavioralConstraint,
    DraftStatus,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from app.domain.events import NullEventBus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.modules.planning_replan import PlanningGraphReplanner
from app.services.draft_service import DraftService
from tests.fakes import FakeDBClient, FakeSupabase

IST = ZoneInfo("Asia/Kolkata")
U = "alice"
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    clock.freeze(MONDAY_8AM, schedule_endpoint)


@pytest.fixture
def fake() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def repos(fake):
    return RepositoryFactory.supabase(fake)


@pytest.fixture
def replanner(fake, repos) -> PlanningGraphReplanner:
    return PlanningGraphReplanner(
        repos.drafts, db_provider=lambda: FakeDBClient(fake), memory_provider=lambda: None
    )


def _row(task_id: str, *, status: str = "pending", start: datetime | None = None, **extra) -> dict:
    row = {
        "task_id": task_id,
        "title": f"Revise {task_id}",
        "goal_id": "dsa",
        "status": status,
        "duration_minutes": 25,
        "difficulty_weight": 0.5,
        "dependencies": [],
        "deadline_hint": None,
        **extra,
    }
    if start is not None:
        row["scheduled_start"] = start.isoformat()
        row["scheduled_end"] = (start + timedelta(minutes=25)).isoformat()
    return row


async def _seed_tasks(repos, rows: list[dict]) -> None:
    """Write tasks the only way they are ever written: by accepting a draft."""
    draft = await repos.drafts.create(
        U, tasks=[{"task_id": "seed", "title": "seed"}], horizon_start=None, goal_id=None,
        schedule=None,
    )
    await repos.drafts.accept_atomic(U, draft.id, rows)


def _learned_rule(hour: int) -> BehavioralConstraint:
    """A learned rule as accepting a pattern proposal stores it: its slot worked out."""
    from app.services.memory.constraint_bridge import _to_horizon

    start = _to_horizon(hour * 60)
    return BehavioralConstraint(
        id=uuid4().hex,
        user_id=U,
        raw_text=f"No demanding work between {hour:02d}:00 and {hour + 1:02d}:00",
        constraint_type="habit",
        recurrence="daily",
        source="inferred",
        structured_semantics={
            "pattern_key": f"skip_time_window:{hour}",
            "semantic_slots": [{
                "name": f"learned_rule_{hour:02d}",
                "start_min": start,
                "end_min": start + 60,
                "availability": "blocked",
                "recurrence": "daily",
            }],
        },
    )


def _free_text_habit() -> BehavioralConstraint:
    return BehavioralConstraint(
        id=uuid4().hex, user_id=U, raw_text="I sleep from 11 PM to 7 AM",
        constraint_type="habit", recurrence="daily",
    )


def _wall_clock(draft) -> dict[str, tuple[datetime, datetime]]:
    start = datetime.fromisoformat(draft.horizon_start)
    return {
        task_id: (start + timedelta(minutes=slot["start_min"]),
                  start + timedelta(minutes=slot["end_min"]))
        for task_id, slot in draft.schedule.items()
    }


def _touches(slot: tuple[datetime, datetime], hour: int) -> bool:
    begin, end = (moment.astimezone(IST) for moment in slot)
    window = begin.replace(hour=hour, minute=0, second=0, microsecond=0)
    return begin < window + timedelta(hours=1) and end > window


# --- what there is to plan ----------------------------------------------------------


@pytest.mark.asyncio
async def test_replan__no_tasks__none_and_no_llm_call(replanner, repos, fake, recording_llm):
    await repos.constraints.add(U, _free_text_habit())  # translating it would be a model call

    assert await replanner.replan(U, reslot_task_ids=[]) is None
    assert recording_llm.calls == []
    assert fake.rows.get("draft_schedules", []) == []


@pytest.mark.asyncio
async def test_replan__pending_tasks__draft_without_decomposition(replanner, repos, recording_llm):
    await _seed_tasks(repos, [_row("graphs"), _row("trees"), _row("heaps")])

    draft = await replanner.replan(U, reslot_task_ids=[])

    assert draft.status is DraftStatus.PENDING
    assert {t["task_id"] for t in draft.tasks} == {"graphs", "trees", "heaps"}
    assert set(draft.schedule) == {"graphs", "trees", "heaps"}
    assert datetime.fromisoformat(draft.horizon_start) == MONDAY_8AM
    assert recording_llm.calls == []  # no goal to break down, no habit to translate


@pytest.mark.asyncio
async def test_replan__reslot_missed__included_and_accept_reslots(replanner, repos, recording_llm):
    old_slot = MONDAY_8AM - timedelta(days=1) + timedelta(hours=2)  # Sunday 10:00
    await _seed_tasks(repos, [
        _row("graphs"),
        _row("dp", status="missed", start=old_slot),
        _row("tries", status="missed", start=old_slot - timedelta(days=3)),
    ])
    await repos.task_events.append(U, TaskEvent(
        user_id=U, task_id="dp", event_type=TaskEventType.SCHEDULED,
        occurred_at=old_slot - timedelta(days=1), scheduled_start=old_slot,
        scheduled_end=old_slot + timedelta(minutes=25),
    ))

    draft = await replanner.replan(U, reslot_task_ids=["dp"])
    written = await DraftService(repos.drafts, NullEventBus(), task_events=repos.task_events).accept(
        U, draft.id
    )

    assert {t["task_id"] for t in draft.tasks} == {"graphs", "dp"}  # "tries" stays set aside
    assert written == 2
    dp = await repos.tasks.get(U, "dp")
    assert dp.status is TaskStatus.PENDING and dp.scheduled_start > old_slot
    assert (await repos.tasks.get(U, "tries")).status is TaskStatus.MISSED
    (moved,) = await repos.task_events.list_for_user(
        U, types={TaskEventType.RESCHEDULED}, task_ids={"dp"}
    )
    assert datetime.fromisoformat(moved.metadata["previous_start"]) == old_slot
    assert recording_llm.calls == []


# --- habits whose slots are already known ------------------------------------------


@pytest.mark.asyncio
async def test_fetch_constraints__precomputed_slots__no_habit_translation_call(
    fake, repos, recording_llm
):
    from app.core.user_model import UserModel
    from app.modules.planning_graph import fetch_constraints, translate_habits

    rule = _learned_rule(9)
    await repos.constraints.add(U, rule)
    await repos.constraints.add(U, _free_text_habit())

    mixed = await fetch_constraints({"user_id": U, "user_model": UserModel(U, FakeDBClient(fake))})

    assert mixed["habits_text"] == "I sleep from 11 PM to 7 AM"  # only free text is translated
    assert mixed["precomputed_slots"] == [
        {**rule.structured_semantics["semantic_slots"][0], "weekday": None, "valid_from": None,
         "valid_until": None, "max_task_duration": None, "max_difficulty": None}
    ]

    only_rule = FakeSupabase()
    await RepositoryFactory.supabase(only_rule).constraints.add(U, rule)
    state = await fetch_constraints({"user_id": U, "user_model": UserModel(U, FakeDBClient(only_rule))})
    translated = await translate_habits(state)

    assert state["habits_text"] == ""
    assert translated["semantic_slots"] == state["precomputed_slots"]
    assert recording_llm.calls == []


@pytest.mark.asyncio
async def test_replan__learned_rule__no_task_in_that_hour(replanner, repos, recording_llm):
    # Six 25-minute tasks, three a day at most: 08:00-09:15 on the first day if nothing
    # stands in the way.
    await _seed_tasks(repos, [_row(name) for name in ("a", "b", "c", "d", "e", "f")])
    before = await replanner.replan(U, reslot_task_ids=[])
    assert any(_touches(slot, 9) for slot in _wall_clock(before).values())

    await repos.constraints.add(U, _learned_rule(9))
    after = await replanner.replan(U, reslot_task_ids=[])

    slots = _wall_clock(after)
    assert len(slots) == 6
    assert not [slot for slot in slots.values() if _touches(slot, 9)], slots
    assert recording_llm.calls == []  # the rule's slot was known: nothing to translate
