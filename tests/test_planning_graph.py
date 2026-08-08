"""Tests for the planning module graph topology and its scheduling node."""

import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.modules.planning_graph import planning_module
from app.core.module_framework import build_module_graph

# Captured at import, before any fixture can swap it out: the decomposition
# tests below want the REAL router in the loop and fake one layer lower.
from app.core.model_router import route_llm_call as _real_route_llm_call


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


@pytest.mark.asyncio
async def test_solve_schedule__does_not_block_the_event_loop(monkeypatch):
    """CP-SAT is a synchronous C++ solve — it must run off-thread.

    Bare, it freezes the whole event loop for the length of the solve. On the
    streaming path that means every queued SSE frame stops mid-plan: the user
    watches a dead spinner instead of the progress events already produced. It
    also makes `_wrap_step`'s `asyncio.wait_for` advisory only, since a blocking
    call cannot be interrupted by the scheduler.
    """
    from app.api.v1.endpoints import schedule as schedule_ep
    from app.modules.planning_graph import solve_schedule

    idents: list[int] = []

    class _Resp:
        def model_dump(self, mode="json"):
            return {"schedule": {}, "horizon_start": "2026-08-08T08:00:00+00:00"}

    def _recording_run_schedule(*a, **k):
        idents.append(threading.get_ident())
        return _Resp()

    monkeypatch.setattr(schedule_ep, "run_schedule", _recording_run_schedule)

    result = await solve_schedule(
        {
            "task_chunks": [_chunk()],
            "time_slots": [],
            "horizon_minutes": 2880,
            "user_id": "u1",
        }
    )

    assert result["error"] is None
    assert idents and idents[0] != threading.get_ident()


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


# --- the decomposition LLM boundary -----------------------------------------
# These tests fake `hybrid_route_query` (the transport), NOT `route_llm_call`.
# Faking the router is what let F1 ship: the old fake handed `decompose_goal` an
# `ExecutionGraph` instance, a value production never produced, so the whole
# decomposition path was tested against fiction. `litellm_conf.py:259` returns
# `parsed.model_dump()` — a plain dict — for every structured call, local and
# cloud alike. Patching one layer lower keeps the real router in the test.


async def _fake_decompose_transport(*args, **kwargs):
    """Return exactly what `hybrid_route_query` returns: a `model_dump()` dict."""
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata, TaskChunk

    return ExecutionGraph(
        goal_metadata=GoalMetadata(),
        decomposition=[TaskChunk.model_validate(_chunk(f"t{i}")) for i in range(6)],
        cognitive_load_estimate={"intrinsic_load": 0.5},
    ).model_dump()


def _decompose_transport(chunks: list[dict], goal_metadata=None):
    """A transport returning exactly the chunks (and goal metadata) a test needs.

    Same contract as `_fake_decompose_transport` — a `model_dump()` dict — just
    parameterised, so the namespacing tests can pin ids and dependencies.
    """

    async def _transport(*args, **kwargs):
        from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata, TaskChunk

        return ExecutionGraph(
            goal_metadata=goal_metadata if goal_metadata is not None else GoalMetadata(),
            decomposition=[TaskChunk.model_validate(c) for c in chunks],
            cognitive_load_estimate={"intrinsic_load": 0.5},
        ).model_dump()

    return _transport


def _patch_decompose_transport(monkeypatch, transport=None) -> None:
    """Wire the real router to an in-memory transport — no network, no .env.

    The `no_llm` fixture stubs `route_llm_call` itself; restoring the genuine
    function here keeps the normalisation under test even when both are active.
    """
    import app.core.config as _cfg
    import app.models.brain.litellm_conf as _llm

    monkeypatch.setattr(
        "app.core.model_router.route_llm_call", _real_route_llm_call
    )
    monkeypatch.setattr(
        _llm, "hybrid_route_query", transport or _fake_decompose_transport
    )
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.utils.chroma_client.query_knowledge", lambda *a, **k: []
    )


@pytest.mark.asyncio
async def test_decompose_goal__transport_returns_dict__still_produces_chunks(monkeypatch):
    """F1 regression: a dict from the transport must not empty the decomposition.

    Live run 2026-08-08: `route_llm_call` normalised only `str`, so the dict fell
    through, `str(dict)` produced a Python repr, `json.loads` died at char 1 and
    the `except` returned `{"task_chunks": []}`. `fuse_tasks` then emitted only
    stale pending rows — the user's goal vanished from a turn that reported
    success.
    """
    from app.modules.planning_graph import decompose_goal

    _patch_decompose_transport(monkeypatch)

    out = await decompose_goal(
        {"planning_goal": "prepare a 10-minute talk on graph algorithms", "user_id": "u1"}
    )

    assert out.get("error") is None, out.get("error")
    assert len(out["task_chunks"]) == 6
    assert out["_tool_detail"] == {"task_count": 6}


@pytest.mark.asyncio
async def test_planning_subgraph__feasible_solve__reaches_create_draft(no_llm, monkeypatch):
    """End-to-end through the compiled graph: the OPTIMAL edge really runs.

    Asserting on `planning_module.steps` alone would pass even if the edge were
    never wired, so this drives the compiled sub-graph with the real OR-Tools
    solver and only the LLM transport faked.
    """
    from app.modules.planning_graph import planning_state_in

    _patch_decompose_transport(monkeypatch)

    store = _FakeDraftStore(row={"id": "row-uuid"})
    module_state = planning_state_in(
        {"user_model": None, "user_message": "revise for the exam", "draft_store": store}
    )

    result = await build_module_graph(planning_module).ainvoke(module_state)

    assert result["schedule"] is not None
    assert result["draft_id"] == "row-uuid"
    # The draft carries the fused chunks and the solver's own horizon_start.
    _user_id, tasks, horizon_start, _goal = store.calls[0]
    assert len(tasks) == 6
    assert datetime.fromisoformat(horizon_start).hour == 8


# --- invocation counts through the compiled sub-graph ------------------------
# Edge lists are not the acceptance criterion — *how many times each node runs*
# is. Two 27B decompositions in one turn is an OOM risk on a 24 GB machine
# (.claude/CLAUDE.md), and the naive wiring fix fired decompose_goal twice.


def _count_invocations(monkeypatch, module, *names) -> dict:
    """Wrap the named steps' handlers with a counter.

    Counts handler ENTRY, not LLM side effects: entry is what costs a model
    slot, and a node can be entered without the mocked model recording a call.
    `_wrap_step` reads `step.handler` at call time, so patching the dataclass
    instance is enough — and monkeypatch restores the shared module singleton.
    """
    counts = {n: 0 for n in names}

    def _wrap(name, inner):
        async def counting(state):
            counts[name] += 1
            return await inner(state)
        return counting

    for step in module.steps:
        if step.name in counts:
            monkeypatch.setattr(step, "handler", _wrap(step.name, step.handler))
    return counts


@pytest.mark.asyncio
async def test_planning_subgraph__clear_goal__decompose_goal_runs_exactly_once(monkeypatch):
    """One goal, one decomposition. The fan-in is a barrier, not two triggers.

    Wired as independent edges, `expand_slots` and `memory_to_constraints`
    complete at different depths and fire `decompose_goal` once each — two
    concurrent 27B calls and an `InvalidUpdateError` on `task_chunks`.
    """
    from app.modules.planning_graph import planning_module, planning_state_in

    counts = _count_invocations(
        monkeypatch, planning_module,
        "validate_goal", "fetch_constraints", "decompose_goal", "fuse_tasks",
        "solve_schedule", "create_draft",
    )
    _patch_decompose_transport(monkeypatch)

    module_state = planning_state_in(
        {"user_model": None, "user_message": "revise for the compilers exam",
         "draft_store": None}
    )
    result = await build_module_graph(planning_module).ainvoke(module_state)

    assert counts == {
        "validate_goal": 1, "fetch_constraints": 1, "decompose_goal": 1,
        "fuse_tasks": 1, "solve_schedule": 1, "create_draft": 1,
    }
    assert result["schedule"] is not None
    assert result["draft_id"] is None  # degrades gracefully with no draft_store


@pytest.mark.asyncio
async def test_planning_subgraph__unclear_goal__decompose_goal_never_runs(monkeypatch):
    """`validate_goal` is a gate, and a gate that leaks costs a 27B call.

    'hi' is under the 5-character floor, so the module must stop at the
    clarification — no decomposition, and no habit translation either (that is
    a 27B call too, and there is nothing to plan).
    """
    from app.modules.planning_graph import planning_module, planning_state_in

    counts = _count_invocations(
        monkeypatch, planning_module,
        "validate_goal", "decompose_goal", "translate_habits", "solve_schedule",
        "create_draft",
    )
    _patch_decompose_transport(monkeypatch)

    module_state = planning_state_in(
        {"user_model": None, "user_message": "hi", "draft_store": None}
    )
    result = await build_module_graph(planning_module).ainvoke(module_state)

    assert counts == {
        "validate_goal": 1, "decompose_goal": 0, "translate_habits": 0,
        "solve_schedule": 0, "create_draft": 0,
    }
    assert result["clarification_request"]
    assert result["schedule"] is None


@pytest.mark.asyncio
async def test_planning_subgraph__infeasible_ladder__reruns_solve_only(monkeypatch):
    """The retry loop re-enters the solver, never the decomposition.

    `handle_infeasible -> solve_schedule` is a back-edge into the middle of the
    pipeline; if it re-triggered the fan-in the user would pay for a fresh 27B
    decomposition on every rung of the horizon ladder.
    """
    from fastapi import HTTPException
    from app.modules.planning_graph import planning_module, planning_state_in

    counts = _count_invocations(
        monkeypatch, planning_module,
        "decompose_goal", "fuse_tasks", "solve_schedule", "handle_infeasible",
        "create_draft",
    )
    _patch_decompose_transport(monkeypatch)

    ok = MagicMock()
    ok.model_dump.return_value = {
        "status": "OPTIMAL", "schedule": {}, "horizon_start": "2026-08-08T08:00:00+00:00",
    }
    attempts = []

    def flaky_run_schedule(*args, **kwargs):
        attempts.append(kwargs.get("horizon_minutes"))
        if len(attempts) <= 2:
            raise HTTPException(status_code=422, detail="INFEASIBLE")
        return ok

    monkeypatch.setattr(
        "app.api.v1.endpoints.schedule.run_schedule", flaky_run_schedule
    )

    module_state = planning_state_in(
        {"user_model": None, "user_message": "revise for the compilers exam",
         "draft_store": None}
    )
    result = await build_module_graph(planning_module).ainvoke(module_state)

    assert counts == {
        "decompose_goal": 1, "fuse_tasks": 1, "solve_schedule": 3,
        "handle_infeasible": 2, "create_draft": 1,
    }
    # Each retry widened the horizon along the v1 ladder.
    assert attempts == [2880, 4320, 7200]
    assert result["schedule"] is not None


# --- F5: goal namespacing keeps a new plan from eating the old one ----------
# Live run 2026-08-08: the decomposer emits POSITIONAL ids (`task_1…task_N`), so
# every plan produces the same ids as the last one. `fuse_tasks` deduped by
# task_id and dropped the pending rows that collided, and accept's
# delete-then-replace then deleted them from user_tasks for good — six of the
# demo user's pending tasks vanished on a plain "plan X" → "accept".
#
# v1 never had this hole: it namespaces every fresh chunk as `{goal_id}_{task_id}`
# before fusion (control_policy._run_plan_day_flow), which is the only thing that
# makes "tasks belonging to THIS goal" decidable. These tests port that rule.


class _PendingUserModel:
    """A UserModel with pending rows and nothing else — no DB, no memory store."""

    def __init__(self, rows: list[dict]):
        self.user_id = "u1"
        self._rows = rows

    async def get_pending_tasks(self) -> list[dict]:
        return list(self._rows)

    async def get_behavioral_constraints(self) -> list[dict]:
        return []

    async def get_memory_store(self):
        return None


@pytest.mark.asyncio
async def test_decompose_goal__namespaces_task_ids_and_dependencies(monkeypatch):
    """Fresh chunks leave decomposition already namespaced, deps included.

    Dependencies must be re-prefixed in the same pass or the solver's precedence
    edges point at ids that no longer exist (`_namespace_chunk`'s docstring).
    """
    from app.modules.planning_graph import decompose_goal

    _patch_decompose_transport(
        monkeypatch,
        _decompose_transport([_chunk("task_1"), _chunk("task_2", dependencies=["task_1"])]),
    )

    out = await decompose_goal(
        {"planning_goal": "revise graph algorithms", "user_id": "u1"}
    )

    assert out.get("error") is None, out.get("error")
    assert out["goal_id"] == "revise_graph_algorithms"
    assert [c["task_id"] for c in out["task_chunks"]] == [
        "revise_graph_algorithms_task_1",
        "revise_graph_algorithms_task_2",
    ]
    assert out["task_chunks"][1]["dependencies"] == ["revise_graph_algorithms_task_1"]


@pytest.mark.asyncio
async def test_decompose_goal__goal_metadata_carries_an_id__it_wins_over_the_slug(monkeypatch):
    """v1 derivation order: goal_metadata.goal_id → slugified objective → uuid."""
    from app.api.v1.endpoints.reasoning import GoalMetadata
    from app.modules.planning_graph import decompose_goal

    _patch_decompose_transport(
        monkeypatch,
        _decompose_transport(
            [_chunk("task_1")],
            goal_metadata=GoalMetadata(goal_id="thesis", objective="finish the thesis"),
        ),
    )

    out = await decompose_goal({"planning_goal": "finish the thesis", "user_id": "u1"})

    assert out["goal_id"] == "thesis"
    assert [c["task_id"] for c in out["task_chunks"]] == ["thesis_task_1"]


@pytest.mark.asyncio
async def test_fuse_tasks__prior_plans_positional_ids__survive_the_new_decomposition():
    """v1 parity: keep tasks from OTHER goals, drop tasks from THIS goal.

    The legacy `task_1` row is the F5 collision itself — pre-fix the new chunks
    carried that same id, so the row was silently dropped from the fusion.
    """
    from app.modules.planning_graph import fuse_tasks

    pending = [
        # An un-namespaced row written by an earlier plan — the actual collision.
        {"task_id": "task_1", "title": "read chapter 1", "duration_minutes": 25},
        # Another goal's row: must survive, it is not what this plan replaces.
        {"task_id": "calculus_task_1", "title": "integrals", "duration_minutes": 20},
        # THIS goal's leftover from a previous run: replaced by the new decomposition.
        {"task_id": "revise_graph_algorithms_task_9", "title": "stale", "duration_minutes": 25},
    ]
    state = {
        "user_model": _PendingUserModel(pending),
        "goal_id": "revise_graph_algorithms",
        "task_chunks": [
            _chunk("revise_graph_algorithms_task_1"),
            _chunk("revise_graph_algorithms_task_2"),
        ],
    }

    out = await fuse_tasks(state)
    ids = [c["task_id"] for c in out["task_chunks"]]

    assert "task_1" in ids
    assert "calculus_task_1" in ids
    assert "revise_graph_algorithms_task_9" not in ids
    assert len(ids) == len(set(ids))  # duplicate ids break the solver's uniqueness
    assert out["pending_tasks"] == pending


@pytest.mark.asyncio
async def test_fuse_tasks__no_goal_id__falls_back_to_id_dedupe():
    """Decomposition failed (no goal_id on state): still never emit a duplicate id."""
    from app.modules.planning_graph import fuse_tasks

    pending = [{"task_id": "task_1", "title": "old", "duration_minutes": 25}]
    out = await fuse_tasks(
        {"user_model": _PendingUserModel(pending), "task_chunks": [_chunk("task_1")]}
    )

    ids = [c["task_id"] for c in out["task_chunks"]]
    assert ids == ["task_1"]
    assert out["task_chunks"][0]["title"] == "read chapter 1"  # the new one won


@pytest.mark.asyncio
async def test_accept_after_replan__pre_existing_pending_rows_survive_in_user_tasks(monkeypatch):
    """The whole F5 chain: decompose → fuse → accept must not delete prior work.

    `_persist_fused_tasks` wipes every pending row and re-inserts the fused list,
    so anything fusion drops is gone from the database permanently. The draft is
    built here exactly as `create_draft` builds it (`tasks=state["task_chunks"]`);
    the solver is skipped because scheduling is not what F5 is about.
    """
    from app.core.user_model import UserModel
    from app.modules.planning_graph import decompose_goal, fuse_tasks
    from app.services.draft_actions import accept_draft_and_persist
    from tests.fakes import FakeDBClient, FakeSupabase

    supabase = FakeSupabase({"user_tasks": [
        {"user_id": "u1", "task_id": "task_1", "title": "read chapter 1",
         "status": "pending", "plan_id": "plan-last-week", "duration_minutes": 25},
        {"user_id": "u1", "task_id": "calculus_task_1", "title": "integrals",
         "status": "pending", "plan_id": "plan-last-week", "duration_minutes": 20},
    ]})
    user_model = UserModel(user_id="u1", db=FakeDBClient(supabase))

    _patch_decompose_transport(
        monkeypatch, _decompose_transport([_chunk("task_1"), _chunk("task_2")])
    )

    decomposed = await decompose_goal(
        {"planning_goal": "revise graph algorithms", "user_id": "u1"}
    )
    fused = await fuse_tasks({**decomposed, "user_model": user_model})

    draft = {
        "id": "d1",
        "tasks": fused["task_chunks"],
        "horizon_start": "2026-08-08T08:00:00+00:00",
    }
    store = _FakeDraftStore()
    landed = await accept_draft_and_persist(store, "u1", draft, supabase)

    assert sorted(r["task_id"] for r in supabase.rows["user_tasks"]) == [
        "calculus_task_1",
        "revise_graph_algorithms_task_1",
        "revise_graph_algorithms_task_2",
        "task_1",
    ]
    assert landed == 4
    assert store.other_calls == ["accept_draft"]


@pytest.mark.asyncio
async def test_planning_subgraph__namespaced_ids__reach_the_solver_and_the_draft(no_llm, monkeypatch):
    """The longer ids must travel the whole v2 chain unchanged.

    `_to_task_chunk` → `run_schedule` → `create_draft` is newer than v1's path,
    and the solver keys its output map by task_id — `_persist_fused_tasks` then
    looks `scheduled_start` up by that key. An id regenerated or truncated
    anywhere in between would silently drop every wall-clock time, so this drives
    the compiled sub-graph with the real OR-Tools solver and pins both ends.
    """
    from app.modules.planning_graph import planning_state_in

    _patch_decompose_transport(
        monkeypatch,
        _decompose_transport([_chunk("task_1"), _chunk("task_2", dependencies=["task_1"])]),
    )

    store = _FakeDraftStore(row={"id": "row-uuid"})
    module_state = planning_state_in({
        "user_model": _PendingUserModel(
            [{"task_id": "calculus_task_1", "title": "integrals", "duration_minutes": 20}]
        ),
        "user_message": "revise graph algorithms",
        "draft_store": store,
    })

    result = await build_module_graph(planning_module).ainvoke(module_state)

    _user_id, tasks, _horizon_start, goal = store.calls[0]
    assert goal == "revise_graph_algorithms"  # the draft row names its own namespace
    draft_ids = [t["task_id"] for t in tasks]
    assert draft_ids == [
        "revise_graph_algorithms_task_1",
        "revise_graph_algorithms_task_2",
        "calculus_task_1",  # the other goal's pending row, fused in untouched
    ]
    assert set(result["schedule"]["schedule"]) == set(draft_ids)
