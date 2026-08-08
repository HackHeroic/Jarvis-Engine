"""Tests for the planning module graph topology and its scheduling node."""

import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.modules.planning_graph import planning_module
from app.core.module_framework import build_module_graph


def _chunk(task_id: str = "t1", **overrides) -> dict:
    """A task_chunks entry shaped like decompose_goal's output."""
    base = {
        "task_id": task_id,
        "title": "read chapter 1",
        "duration_minutes": 25,
        "difficulty_weight": 0.5,
        "dependencies": [],
        "completion_criteria": "summarise it from memory",
        "deadline_hint": None,
    }
    base.update(overrides)
    return base


def test_planning_graph_compiles():
    """Planning module compiles via ModuleStep framework."""
    graph = build_module_graph(planning_module)
    assert graph is not None


def test_planning_graph_has_expected_steps():
    """Planning module has all 10 expected steps."""
    expected = {
        "fetch_constraints", "translate_habits", "expand_slots",
        "memory_to_constraints", "validate_goal", "decompose_goal",
        "fuse_tasks", "solve_schedule", "handle_infeasible", "create_draft",
    }
    actual = {s.name for s in planning_module.steps}
    assert actual == expected


# --- solve_schedule delegates to the reusable run_schedule -------------------
# House rule (.claude/rules/code-style.md): `run_schedule` is THE scheduling
# function — import and call it, never reimplement TMT / adaptive cap / blocks.
# These tests patch the run_schedule boundary to assert *delegation*; the real
# OR-Tools solver is never mocked (see the end-to-end test below).


@pytest.mark.asyncio
async def test_solve_schedule__delegates_to_run_schedule_with_tmt():
    """The node calls run_schedule rather than driving JarvisScheduler itself."""
    from app.modules.planning_graph import solve_schedule

    fake_resp = MagicMock()
    fake_resp.model_dump.return_value = {
        "status": "OPTIMAL",
        "schedule": {"t1": {"start_min": 0, "end_min": 25, "tmt_score": 1.0}},
    }
    with patch(
        "app.api.v1.endpoints.schedule.run_schedule", return_value=fake_resp
    ) as m:
        state = {
            "task_chunks": [_chunk()],
            "time_slots": [],
            "horizon_minutes": 2880,
            "user_id": "u1",
        }
        result = await solve_schedule(state)

    m.assert_called_once()
    assert result["error"] is None
    assert result["schedule"] is not None
    assert result["_tool_detail"]["tmt_applied"] is True


@pytest.mark.asyncio
async def test_solve_schedule__builds_valid_execution_graph_for_run_schedule():
    """The ExecutionGraph handed to run_schedule must satisfy its real schema.

    Regression guard: `goal_metadata` is a required GoalMetadata (not None) and
    `cognitive_load_estimate` is a Dict[str, float] (not a bare float) — passing
    the wrong shape raises ValidationError before the solver ever runs.
    """
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata
    from app.modules.planning_graph import solve_schedule

    with patch(
        "app.api.v1.endpoints.schedule.run_schedule", return_value=MagicMock()
    ) as m:
        await solve_schedule(
            {
                "task_chunks": [_chunk()],
                "time_slots": [],
                "horizon_minutes": 2880,
                "user_id": "u1",
            }
        )

    graph = m.call_args.args[0]
    assert isinstance(graph, ExecutionGraph)
    assert isinstance(graph.goal_metadata, GoalMetadata)
    assert isinstance(graph.cognitive_load_estimate, dict)
    assert graph.decomposition[0].task_id == "t1"


@pytest.mark.asyncio
async def test_solve_schedule__horizon_start_comes_from_run_schedule():
    """horizon_start is run_schedule's to resolve; the node echoes it as ISO.

    run_schedule types it Optional[datetime] and does date math on it, so the
    node must never hand it a string — it hands it nothing and reads the
    resolved value back off the response (UI contract: wall time =
    horizon_start + timedelta(minutes=start_min)).
    """
    from app.modules.planning_graph import solve_schedule

    fake_resp = MagicMock()
    fake_resp.model_dump.return_value = {
        "status": "OPTIMAL",
        "schedule": {},
        "horizon_start": "2026-08-08T08:00:00+00:00",
    }
    with patch(
        "app.api.v1.endpoints.schedule.run_schedule", return_value=fake_resp
    ) as m:
        result = await solve_schedule(
            {
                "task_chunks": [_chunk()],
                "time_slots": [],
                "horizon_minutes": 2880,
                "user_id": "u1",
            }
        )

    passed = m.call_args.kwargs.get("horizon_start")
    assert passed is None or isinstance(passed, datetime)
    assert result["horizon_start"] == "2026-08-08T08:00:00+00:00"


@pytest.mark.asyncio
async def test_solve_schedule__real_solver__horizon_start_is_parseable_iso():
    """End-to-end: the echoed horizon_start round-trips through fromisoformat."""
    from app.modules.planning_graph import solve_schedule

    result = await solve_schedule(
        {
            "task_chunks": [_chunk(f"t{i}") for i in range(4)],
            "time_slots": [],
            "horizon_minutes": 2880,
            "user_id": "u1",
        }
    )

    assert datetime.fromisoformat(result["horizon_start"]).hour == 8


@pytest.mark.asyncio
async def test_solve_schedule__maps_time_slots_to_timeslot_objects():
    """PlanningState slot dicts (incl. minimal_work extras) become TimeSlots."""
    from app.schemas.context import Availability, TimeSlot
    from app.modules.planning_graph import solve_schedule

    with patch(
        "app.api.v1.endpoints.schedule.run_schedule", return_value=MagicMock()
    ) as m:
        await solve_schedule(
            {
                "task_chunks": [_chunk()],
                "time_slots": [
                    {
                        "name": "Sleep",
                        "start_min": 960,
                        "end_min": 1440,
                        "availability": "blocked",
                    },
                    {
                        "name": "Lecture",
                        "start_min": 60,
                        "end_min": 180,
                        "availability": "minimal_work",
                        "max_task_duration": 10,
                        "max_difficulty": 0.3,
                    },
                ],
                "horizon_minutes": 2880,
                "user_id": "u1",
            }
        )

    daily_context = m.call_args.args[1]
    assert all(isinstance(s, TimeSlot) for s in daily_context)
    soft = next(s for s in daily_context if s.name == "Lecture")
    assert soft.availability == Availability.MINIMAL_WORK
    assert soft.max_task_duration == 10
    assert soft.max_difficulty == 0.3


@pytest.mark.asyncio
async def test_solve_schedule__fused_pending_task_missing_fields__still_schedules():
    """fuse_tasks emits rows without completion_criteria — must not blow up.

    TaskChunk.completion_criteria is required and duration_minutes is capped at
    25, so raw pending-task dicts fail strict validation. The node coerces.
    """
    from app.modules.planning_graph import solve_schedule

    pending_row = {
        "task_id": "pending_1",
        "title": "finish lab report",
        "duration_minutes": 90,  # over TaskChunk's 25-minute ceiling
        "difficulty_weight": 0.5,
        "dependencies": [],
        "deadline_hint": None,
        # no completion_criteria — fuse_tasks never sets one
    }
    with patch(
        "app.api.v1.endpoints.schedule.run_schedule", return_value=MagicMock()
    ) as m:
        result = await solve_schedule(
            {
                "task_chunks": [pending_row],
                "time_slots": [],
                "horizon_minutes": 2880,
                "user_id": "u1",
            }
        )

    assert result["error"] is None
    graph = m.call_args.args[0]
    assert graph.decomposition[0].task_id == "pending_1"
    assert graph.decomposition[0].duration_minutes <= 25


@pytest.mark.asyncio
async def test_solve_schedule__422_maps_to_infeasible():
    """run_schedule raises HTTPException(422) on INFEASIBLE — map, don't leak."""
    from fastapi import HTTPException
    from app.modules.planning_graph import solve_schedule

    with patch(
        "app.api.v1.endpoints.schedule.run_schedule",
        side_effect=HTTPException(status_code=422, detail="INFEASIBLE"),
    ):
        result = await solve_schedule(
            {
                "task_chunks": [_chunk()],
                "time_slots": [],
                "horizon_minutes": 2880,
                "user_id": "u1",
            }
        )

    assert result["error"] == "INFEASIBLE"
    assert result["schedule"] is None


@pytest.mark.asyncio
async def test_solve_schedule__non_422_http_error__propagates():
    """Only 422 means 'scope problem'. Anything else is a real failure."""
    from fastapi import HTTPException
    from app.modules.planning_graph import solve_schedule

    with patch(
        "app.api.v1.endpoints.schedule.run_schedule",
        side_effect=HTTPException(status_code=500, detail="boom"),
    ):
        with pytest.raises(HTTPException):
            await solve_schedule(
                {
                    "task_chunks": [_chunk()],
                    "time_slots": [],
                    "horizon_minutes": 2880,
                    "user_id": "u1",
                }
            )


@pytest.mark.asyncio
async def test_solve_schedule__no_tasks__returns_error_without_calling_solver():
    """ExecutionGraph.decomposition has min_length=1 — short-circuit first."""
    from app.modules.planning_graph import solve_schedule

    with patch("app.api.v1.endpoints.schedule.run_schedule") as m:
        result = await solve_schedule(
            {"task_chunks": [], "time_slots": [], "horizon_minutes": 2880}
        )

    m.assert_not_called()
    assert result["schedule"] is None
    assert result["error"]


@pytest.mark.asyncio
async def test_solve_schedule__real_solver__applies_biological_sleep_fallback():
    """End-to-end through the REAL run_schedule + real CP-SAT (never mocked).

    With no sleep habit in time_slots, run_schedule injects the Midnight-8 AM
    block, so nothing may land in the 960-1440 intra-day window.
    """
    from app.modules.planning_graph import solve_schedule

    result = await solve_schedule(
        {
            "task_chunks": [_chunk(f"t{i}") for i in range(4)],
            "time_slots": [],
            "horizon_minutes": 2880,
            "user_id": "u1",
        }
    )

    assert result["error"] is None
    scheduled = result["schedule"]["schedule"]
    assert len(scheduled) == 4
    for slot in scheduled.values():
        for day in range(3):
            sleep_start = day * 1440 + 960
            sleep_end = day * 1440 + 1440
            overlaps = slot["start_min"] < sleep_end and slot["end_min"] > sleep_start
            assert not overlaps, (
                f"task scheduled inside the injected sleep block: {slot}"
            )
        # TMT priority came from run_schedule, not a positional heuristic.
        assert slot["tmt_score"] is not None


# --- horizon ladder ---------------------------------------------------------


def test_handle_infeasible__ladder_matches_v1():
    from app.modules.planning_graph import HORIZON_RETRY_SEQUENCE

    assert HORIZON_RETRY_SEQUENCE == [4320, 7200, 10080, 20160, 43200]


@pytest.mark.asyncio
async def test_handle_infeasible__walks_the_full_ladder():
    from app.modules.planning_graph import HORIZON_RETRY_SEQUENCE, handle_infeasible

    for i, expected in enumerate(HORIZON_RETRY_SEQUENCE):
        out = await handle_infeasible({"retry_count": i})
        assert out["horizon_minutes"] == expected
        assert out["retry_count"] == i + 1
        assert out["error"] is None


@pytest.mark.asyncio
async def test_handle_infeasible__exhausted__anti_guilt_message_says_30_days():
    from app.modules.planning_graph import HORIZON_RETRY_SEQUENCE, handle_infeasible

    out = await handle_infeasible({"retry_count": len(HORIZON_RETRY_SEQUENCE)})

    assert out["error"] == "INFEASIBLE_EXHAUSTED"
    assert "30-day" in out["clarification_request"]
    assert "not a you problem" in out["clarification_request"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing bug inherited from v1 via run_schedule, not introduced by "
        "the delegation: compute_adaptive_daily_cap forces target_days >= 2, so a "
        "one-task plan gets cap = ceil(25/2) = 13 min/day — below the task's own "
        "25 minutes — and CP-SAT can place it nowhere. Every rung of the horizon "
        "ladder lowers the cap further, so the user is told 'I couldn't fit "
        "everything in even with a 30-day window' for a single 25-minute task. "
        "Fix belongs in app/utils/pacing.py (floor the cap at the longest task)."
    ),
)
@pytest.mark.asyncio
async def test_solve_schedule__real_solver__single_small_task__is_feasible():
    from app.modules.planning_graph import solve_schedule

    result = await solve_schedule(
        {
            "task_chunks": [_chunk()],
            "time_slots": [],
            "horizon_minutes": 2880,
            "user_id": "u1",
        }
    )

    assert result["error"] is None


# --- create_draft: a feasible solve becomes a reviewable draft ---------------
# v1 parity: the schedule is proposed, never silently committed. Persistence
# (_persist_fused_tasks) happens on accept only — never in this node.


class _FakeDraftStore:
    """Records create_draft calls and returns whatever `row` is set to."""

    def __init__(self, row=None):
        self.row = row if row is not None else {"draft_id": "d-123"}
        self.calls = []
        self.other_calls = []
        self.thread_idents = []

    def create_draft(self, user_id, tasks, horizon_start, goal_id=None):
        self.calls.append((user_id, tasks, horizon_start, goal_id))
        self.thread_idents.append(threading.get_ident())
        return self.row

    def accept_draft(self, *a, **k):
        self.other_calls.append("accept_draft")
        return True

    def delete_draft(self, *a, **k):
        self.other_calls.append("delete_draft")
        return True


def _solved_state(**overrides) -> dict:
    base = {
        "user_id": "u1",
        "schedule": {"schedule": {"t1": {"start_min": 0}}},
        "horizon_start": "2026-08-08T08:00:00+00:00",
        "task_chunks": [_chunk()],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_create_draft__stores_tasks_and_sets_draft_id():
    from app.modules.planning_graph import create_draft

    store = _FakeDraftStore()
    result = await create_draft(_solved_state(draft_store=store))

    assert result["draft_id"] == "d-123"
    user_id, tasks, horizon_start, _goal = store.calls[0]
    assert user_id == "u1"
    assert tasks == [_chunk()]
    assert horizon_start == "2026-08-08T08:00:00+00:00"


@pytest.mark.asyncio
async def test_create_draft__real_store_row_shape__reads_the_id_column():
    """DraftStore.create_draft returns the Supabase row — its PK column is `id`.

    draft_store.py:58-64 generates `draft_id` locally but inserts it as `"id"`
    and returns `result.data[0]`, so a node that only reads `row["draft_id"]`
    would hand the user a null id against a real (non-fake) store — exactly the
    bug this task exists to remove.
    """
    from app.modules.planning_graph import create_draft

    store = _FakeDraftStore(row={"id": "row-uuid", "user_id": "u1", "status": "pending"})
    result = await create_draft(_solved_state(draft_store=store))

    assert result["draft_id"] == "row-uuid"


@pytest.mark.asyncio
async def test_create_draft__never_persists__only_proposes():
    """Accept (Task 10) commits; this node must not touch the accept path."""
    from app.modules.planning_graph import create_draft

    store = _FakeDraftStore()
    await create_draft(_solved_state(draft_store=store))

    assert store.other_calls == []


@pytest.mark.asyncio
async def test_create_draft__no_store__returns_none_draft_id():
    from app.modules.planning_graph import create_draft

    result = await create_draft(
        {"user_id": "u1", "draft_store": None, "schedule": {"schedule": {}}, "task_chunks": []}
    )

    assert result["draft_id"] is None


@pytest.mark.asyncio
async def test_create_draft__degraded_store__returns_none_draft_id():
    """A clientless DraftStore returns None from every method — never crash."""
    from app.services.draft_store import DraftStore
    from app.modules.planning_graph import create_draft

    result = await create_draft(_solved_state(draft_store=DraftStore(supabase_client=None)))

    assert result["draft_id"] is None


@pytest.mark.asyncio
async def test_create_draft__no_schedule__does_not_call_the_store():
    """An infeasible/errored solve has nothing to propose."""
    from app.modules.planning_graph import create_draft

    store = _FakeDraftStore()
    result = await create_draft(_solved_state(draft_store=store, schedule=None))

    assert result["draft_id"] is None
    assert store.calls == []


@pytest.mark.asyncio
async def test_create_draft__horizon_start_falls_back_to_the_schedule_payload():
    """solve_schedule sources it from the payload; read it there if the key is lost."""
    from app.modules.planning_graph import create_draft

    store = _FakeDraftStore()
    state = _solved_state(draft_store=store)
    state.pop("horizon_start")
    state["schedule"] = {"schedule": {}, "horizon_start": "2026-08-08T08:00:00+00:00"}

    result = await create_draft(state)

    assert result["draft_id"] == "d-123"
    assert store.calls[0][2] == "2026-08-08T08:00:00+00:00"


@pytest.mark.asyncio
async def test_create_draft__no_horizon_start_anywhere__skips_the_insert():
    """draft_schedules.horizon_start is TEXT NOT NULL — inserting None can only fail."""
    from app.modules.planning_graph import create_draft

    store = _FakeDraftStore()
    state = _solved_state(draft_store=store)
    state.pop("horizon_start")

    result = await create_draft(state)

    assert result["draft_id"] is None
    assert store.calls == []


@pytest.mark.asyncio
async def test_create_draft__does_not_block_the_event_loop():
    """The Supabase insert is a sync HTTP round-trip — it must run off-thread.

    Bare, it stalls every other coroutine for the length of the insert, and
    `_wrap_step`'s `asyncio.wait_for` cannot interrupt a blocking call, so the
    step timeout would be advisory only.
    """
    from app.modules.planning_graph import create_draft

    store = _FakeDraftStore()
    await create_draft(_solved_state(draft_store=store))

    assert store.thread_idents[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_create_draft__store_raises__is_non_fatal():
    """A Supabase outage must not lose the schedule the solver just produced."""
    from app.modules.planning_graph import create_draft

    class _Exploding:
        def create_draft(self, **kwargs):
            raise RuntimeError("supabase down")

    result = await create_draft(_solved_state(draft_store=_Exploding()))

    assert result["draft_id"] is None


def test_planning_graph__optimal_solve_routes_through_create_draft():
    """The OPTIMAL branch no longer ends at __END__ — it drafts first."""
    solve = next(s for s in planning_module.steps if s.name == "solve_schedule")
    destinations = next(iter(solve.routes_to.values()))

    assert destinations["OPTIMAL"] == "create_draft"
    assert destinations["INFEASIBLE"] == "handle_infeasible"


def test_planning_state_in__carries_the_draft_store_into_the_subgraph():
    from app.modules.planning_graph import planning_state_in

    store = _FakeDraftStore()
    module_state = planning_state_in({"user_model": None, "draft_store": store})

    assert module_state["draft_store"] is store
    assert module_state["draft_id"] is None


def test_planning_state_out__exposes_draft_id_and_opens_review():
    """A proposed draft puts the conversation into REVIEWING for the next turn."""
    from app.orchestrator.state import NegotiationPhase
    from app.modules.planning_graph import planning_state_out

    out = planning_state_out(
        {"schedule": {"schedule": {}}, "task_chunks": [_chunk()], "draft_id": "d-9"},
        "planning_module",
    )

    assert out["draft_id"] == "d-9"
    assert out["negotiation_state"] == NegotiationPhase.REVIEWING


def test_planning_state_out__no_draft__leaves_negotiation_state_alone():
    """Omitting the key matters: LangGraph merges by key, so returning NONE
    would stomp a REVIEWING thread every time a draft could not be created."""
    from app.modules.planning_graph import planning_state_out

    out = planning_state_out(
        {"schedule": None, "task_chunks": [], "clarification_request": "more?"},
        "planning_module",
    )

    assert out["draft_id"] is None
    assert "negotiation_state" not in out


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing module-framework wiring bug (flagged in the Task 8 report, "
        "not introduced here): build_module_graph never gives a step its "
        "`depends_on` edges in two cases, and the planning module hits both. "
        "(a) module_framework.py:197-204 uses `if step.routes_to: ... elif "
        "step.depends_on`, so `validate_goal` — which has both — gets no "
        "incoming edge and is orphaned. (b) the same branch skips any step "
        "listed in `routed_targets`, so `solve_schedule` gets no edge from "
        "`fuse_tasks` and is reachable only via handle_infeasible's retry "
        "back-edge, which is itself unreachable. Net effect: the compiled "
        "planning graph is __start__ -> fetch_constraints -> "
        "{translate_habits -> expand_slots, memory_to_constraints} -> __end__, "
        "so decompose_goal/fuse_tasks/solve_schedule/create_draft have never "
        "run in v2. `create_draft` is correct and unit-tested above; it just "
        "cannot be reached until the framework wires depends_on independently "
        "of routes_to. Fix belongs in app/core/module_framework.py "
        "(build_module_graph edge wiring), not in this module."
    ),
)
@pytest.mark.asyncio
async def test_planning_subgraph__feasible_solve__reaches_create_draft(no_llm):
    """End-to-end through the compiled graph: the OPTIMAL edge really runs.

    Asserting on `planning_module.steps` alone would pass even if the edge were
    never wired, so this drives the compiled sub-graph with the real OR-Tools
    solver and only the LLM boundary faked.
    """
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata, TaskChunk
    from app.modules.planning_graph import planning_state_in

    async def fake_route_llm_call(*args, **kwargs):
        return ExecutionGraph(
            goal_metadata=GoalMetadata(),
            decomposition=[TaskChunk.model_validate(_chunk(f"t{i}")) for i in range(6)],
            cognitive_load_estimate={"intrinsic_load": 0.5},
        )

    store = _FakeDraftStore(row={"id": "row-uuid"})
    module_state = planning_state_in(
        {"user_model": None, "user_message": "revise for the exam", "draft_store": store}
    )

    with patch("app.core.model_router.route_llm_call", fake_route_llm_call):
        result = await build_module_graph(planning_module).ainvoke(module_state)

    assert result["schedule"] is not None
    assert result["draft_id"] == "row-uuid"
    # The draft carries the fused chunks and the solver's own horizon_start.
    _user_id, tasks, horizon_start, _goal = store.calls[0]
    assert len(tasks) == 6
    assert datetime.fromisoformat(horizon_start).hour == 8
