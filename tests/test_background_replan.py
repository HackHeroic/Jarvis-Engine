"""The background replan (`trigger_replan`) must not undo goal ids.

Task endpoints (complete, skip, edit, delete) fire `trigger_replan`, which
re-solves the user's pending tasks and rewrites them through
`_persist_fused_tasks` → `build_task_rows`. The pending tasks come back from
`user_tasks` as TaskChunks, which have no goal_id field. If the goal is not
carried alongside, `build_task_rows` re-reads it off the id prefix, and every
pending task of goal `learn_dsa` is rewritten under goal `learn`.

`trigger_replan` cannot run to the write today: it builds
`ExecutionGraph(goal_metadata=None)`, which pydantic rejects (it has done so since
the function was written). The xfail below pins that. The goal-id test removes
only that one crash, so it can check what the replan writes once it runs.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from tests.fakes import FakeDBClient, FakeSupabase

U1 = "u1"
GOAL = "learn_dsa"
IDS = [f"{GOAL}_ab12cd_task_{n}" for n in (1, 2, 3)]


async def _accepted_learn_dsa_plan_with_one_task_done(supabase: FakeSupabase) -> None:
    """Accept a three-task `learn_dsa` plan the real way, then complete its first task."""
    from app.domain.entities import TaskStatus
    from app.domain.events import NullEventBus
    from app.infrastructure.persistence.factory import RepositoryFactory
    from app.services.draft_service import DraftService

    repos = RepositoryFactory.supabase(supabase)
    draft = await repos.drafts.create(
        U1,
        tasks=[
            {"task_id": tid, "goal_id": GOAL, "title": f"step {n}", "duration_minutes": 25,
             "difficulty_weight": 0.5, "completion_criteria": "explain it back"}
            for n, tid in enumerate(IDS, start=1)
        ],
        horizon_start="2026-11-14T08:00:00+00:00",
        goal_id=GOAL,
        schedule=None,
    )
    await DraftService(repos.drafts, NullEventBus()).accept(U1, draft.id)
    await repos.tasks.update_status(U1, IDS[0], TaskStatus.COMPLETED)


def _pending(supabase: FakeSupabase) -> list[dict]:
    return [r for r in supabase.rows["user_tasks"] if r["status"] == "pending"]


def test_get_all_pending_tasks_with_goals__pairs_each_chunk_with_its_goal_id():
    from app.services.analytical.task_retrieval import (
        get_all_pending_tasks,
        get_all_pending_tasks_with_goals,
    )

    supabase = FakeSupabase({"user_tasks": [
        {"user_id": U1, "task_id": IDS[1], "goal_id": GOAL, "title": "a", "status": "pending",
         "created_at": "2026-11-14T08:00:00+00:00"},
        {"user_id": U1, "task_id": "task_1", "title": "legacy", "status": "pending",
         "created_at": "2026-11-14T07:00:00+00:00"},
        {"user_id": U1, "task_id": IDS[0], "goal_id": GOAL, "title": "done", "status": "completed",
         "created_at": "2026-11-14T06:00:00+00:00"},
    ]})

    pairs = get_all_pending_tasks_with_goals(U1, supabase)

    assert [(chunk.task_id, goal) for chunk, goal in pairs] == [(IDS[1], GOAL), ("task_1", None)]
    # The plain function keeps its return type for its callers.
    assert [c.task_id for c in get_all_pending_tasks(U1, supabase)] == [IDS[1], "task_1"]


@pytest.mark.asyncio
async def test_trigger_replan__learn_dsa_plan__keeps_goal_ids_and_task_ids(monkeypatch):
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata
    from app.services.analytical import control_policy

    def _graph_without_the_known_crash(**kwargs):
        if kwargs.get("goal_metadata") is None:
            kwargs["goal_metadata"] = GoalMetadata()
        return ExecutionGraph(**kwargs)

    monkeypatch.setattr(control_policy, "ExecutionGraph", _graph_without_the_known_crash)
    supabase = FakeSupabase()
    await _accepted_learn_dsa_plan_with_one_task_done(supabase)
    accepted_plan = {r["plan_id"] for r in _pending(supabase)}

    await control_policy.trigger_replan(U1, FakeDBClient(supabase), f"task_completed:{IDS[0]}")

    pending = _pending(supabase)
    assert {r["plan_id"] for r in pending}.isdisjoint(accepted_plan)  # the replan did rewrite them
    assert sorted(r["task_id"] for r in pending) == IDS[1:]
    assert {r["goal_id"] for r in pending} == {GOAL}


@pytest.mark.xfail(
    strict=True,
    raises=PydanticValidationError,
    reason=(
        "Pre-existing: trigger_replan builds ExecutionGraph(goal_metadata=None), which "
        "pydantic rejects, so the background replan never reaches its write. When this "
        "starts passing, remove the marker."
    ),
)
@pytest.mark.asyncio
async def test_trigger_replan__pending_tasks__rewrites_them():
    from app.services.analytical.control_policy import trigger_replan

    supabase = FakeSupabase()
    await _accepted_learn_dsa_plan_with_one_task_done(supabase)
    accepted_plan = {r["plan_id"] for r in _pending(supabase)}

    await trigger_replan(U1, FakeDBClient(supabase), f"task_completed:{IDS[0]}")

    assert {r["plan_id"] for r in _pending(supabase)}.isdisjoint(accepted_plan)
