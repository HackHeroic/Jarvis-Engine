"""Editing a draft re-solves it, and the re-solve keeps the user's learned focus profile."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")
EIGHT_AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)


def _blocked_except(free: list[tuple[int, int]], horizon: int = 2880) -> list:
    """Hard blocks over the whole horizon except the ``free`` windows."""
    from app.schemas.context import Availability, TimeSlot

    slots, cursor = [], 0
    for i, (start, end) in enumerate(sorted(free)):
        if start > cursor:
            slots.append(TimeSlot(name=f"busy_{i}", start_min=cursor, end_min=start,
                                  availability=Availability.BLOCKED))
        cursor = end
    slots.append(TimeSlot(name="busy_rest", start_min=cursor, end_min=horizon,
                          availability=Availability.BLOCKED))
    return slots


def _draft_graph() -> dict:
    task = {"dependencies": [], "completion_criteria": "done", "deadline_hint": "2026-11-19"}
    return {
        "goal_metadata": {},
        "decomposition": [
            {**task, "task_id": "hard", "title": "Proofs", "duration_minutes": 25,
             "difficulty_weight": 0.9},
            {**task, "task_id": "easy", "title": "Flashcards", "duration_minutes": 25,
             "difficulty_weight": 0.1},
        ],
    }


def test_apply_modification__learned_profile__resolve_keeps_the_lean(clock):
    import app.api.v1.endpoints.schedule as schedule_endpoint
    from app.services.analytical.schedule_modifier import (
        ScheduleModification,
        apply_modification,
    )

    clock.freeze(EIGHT_AM, schedule_endpoint)
    evening_peak = [1.0 if h == 19 else 0.2 for h in range(24)]

    def hard_start(focus):
        # Shorten the easy task: the draft is re-solved. Free: 09:00-09:30 and 19:00-19:30.
        edit = ScheduleModification(action="change_duration", target_task_title="Flashcards",
                                    new_duration_minutes=20)
        _, schedule = apply_modification(
            edit, _draft_graph(), {"schedule": {}}, _blocked_except([(60, 90), (660, 690)]),
            EIGHT_AM, focus_by_hour=focus,
        )
        return schedule["schedule"]["hard"]["start_min"]

    assert hard_start(None) == 60  # on TMT alone the longer task takes the morning
    assert hard_start(evening_peak) == 660  # with the learned peak it keeps its evening


@pytest.mark.asyncio
async def test_modify_schedule__learned_profile__handed_to_the_resolve(monkeypatch):
    import app.core.config as cfg
    from app.api.v1.endpoints.chat import ModifyScheduleRequest, modify_schedule
    from app.container import AppContainer
    from app.core import runtime
    from app.domain.entities import TaskEvent, TaskEventType
    from app.orchestrator.hooks import ActionHooks
    from app.services.analytical import schedule_modifier

    container = AppContainer.build(supabase_client=None, cfg=cfg, hooks=ActionHooks())
    runtime.set_container(container)  # conftest resets the registry afterwards
    now = datetime.now(timezone.utc)
    for day in range(1, 13):
        slot = (now - timedelta(days=day)).astimezone(IST).replace(hour=19, minute=0)
        await container.repos.task_events.append("alice", TaskEvent(
            user_id="alice", task_id=f"past_{day}", event_type=TaskEventType.COMPLETED,
            occurred_at=slot + timedelta(minutes=25), scheduled_start=slot,
            scheduled_end=slot + timedelta(minutes=25),
        ))
    learned = await container.energy.estimate("alice", IST, now=now)

    async def parse(user_prompt, task_list):
        return schedule_modifier.ScheduleModification(
            action="change_duration", target_task_title="Flashcards", new_duration_minutes=20
        )

    seen: list = []

    def spy(*args, **kwargs):
        seen.append(kwargs.get("focus_by_hour"))
        return kwargs["execution_graph"], kwargs["schedule_data"]

    async def no_habits(user_id, supabase):
        return ""

    monkeypatch.setattr(schedule_modifier, "parse_modification_request", parse)
    monkeypatch.setattr(schedule_modifier, "apply_modification", spy)
    monkeypatch.setattr(
        "app.services.extraction.behavioral_store.get_behavioral_context_for_calendar", no_habits
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_client=None)))

    await modify_schedule(
        ModifyScheduleRequest(user_id="alice", user_prompt="shorter flashcards",
                              current_tasks=_draft_graph()["decomposition"]),
        request,
    )

    assert learned.source == "learned"
    assert seen == [learned.scores()]
