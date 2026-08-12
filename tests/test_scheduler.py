"""Unit tests for OR-Tools JarvisScheduler and INFEASIBLE states."""

from types import SimpleNamespace

import pytest

from app.core.or_tools.solver import JarvisScheduler


def test_hard_block_and_single_task():
    """Task should be scheduled after the hard block (no overlap)."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(0, 60, "early_block")
    scheduler.add_task("task_1", 30, 5, [])
    result, status = scheduler.solve()
    assert result != "INFEASIBLE"
    assert result["task_1"]["start"] >= 60


def test_dependency_a_before_b():
    """Task B must start after Task A ends."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_task("A", 30, 1, [])
    scheduler.add_task("B", 20, 2, ["A"])
    result, _ = scheduler.solve()
    assert result != "INFEASIBLE"
    assert result["B"]["start"] >= result["A"]["end"]


def test_sleep_block_tasks_avoid_night():
    """Tasks should avoid the sleep block (1380-1860)."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(1380, 1860, "sleep")
    scheduler.add_task("t1", 25, 3, [])
    scheduler.add_task("t2", 25, 2, ["t1"])
    result, _ = scheduler.solve()
    assert result != "INFEASIBLE"
    for tid, slot in result.items():
        assert slot["end"] <= 1380 or slot["start"] >= 1860


class _RecordingSolver:
    """Stands in for ``cp_model.CpSolver`` so the wall-clock cap can be read
    back without building a model slow enough to actually hit it."""

    instances: list["_RecordingSolver"] = []

    def __init__(self, status=None):
        from ortools.sat.python import cp_model

        self.parameters = SimpleNamespace(max_time_in_seconds=None)
        self._status = cp_model.UNKNOWN if status is None else status
        _RecordingSolver.instances.append(self)

    def solve(self, _model):
        return self._status

    def value(self, _var):  # pragma: no cover - only reached on FEASIBLE
        return 0


@pytest.fixture
def recording_solver(monkeypatch):
    """Swap CpSolver for the recorder; returns the list of instances built."""
    from app.core.or_tools import solver as solver_mod

    _RecordingSolver.instances = []
    monkeypatch.setattr(solver_mod.cp_model, "CpSolver", _RecordingSolver)
    return _RecordingSolver.instances


def test_solve_caps_wall_clock_time(recording_solver):
    """An uncapped CP-SAT solve has no upper bound on how long it runs.

    On the streaming path the solve sits between the user and their plan, so an
    unbounded search is an unbounded stall. The cap turns "hangs forever" into
    "returns the best schedule found, or INFEASIBLE" — the case the horizon
    ladder and the Socratic recalibration path already handle.
    """
    from app.core.config import SOLVER_MAX_TIME_SECONDS

    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_task("t1", 25, 1, [])
    scheduler.solve()

    assert len(recording_solver) == 1
    assert recording_solver[0].parameters.max_time_in_seconds == SOLVER_MAX_TIME_SECONDS
    assert SOLVER_MAX_TIME_SECONDS > 0


def test_solve_timeout_without_a_solution_reads_as_infeasible(recording_solver):
    """CP-SAT answers UNKNOWN when the cap expires before any solution exists.

    'I ran out of time' and 'there is no such schedule' are different facts, but
    the caller's options are identical — widen the horizon, cut scope — so
    UNKNOWN takes the INFEASIBLE path rather than crashing on a status the
    unpacking never expected.
    """
    from ortools.sat.python import cp_model

    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_task("t1", 25, 1, [])

    result, status = scheduler.solve()

    # The stand-in returns UNKNOWN, exactly what an expired cap looks like.
    assert recording_solver[-1]._status == cp_model.UNKNOWN
    assert result == "INFEASIBLE"
    assert status == ""


def test_infeasible_too_much_work():
    """Impossible constraint (50 hours of tasks in 24) should return INFEASIBLE."""
    scheduler = JarvisScheduler(horizon_minutes=1440)  # 24 hours
    scheduler.add_hard_block(0, 480, "sleep")  # 8h sleep
    # 50 hours of tasks in 24h horizon with 8h blocked
    for i in range(60):  # 60 tasks x 50 min = 3000 min > 1440
        scheduler.add_task(f"task_{i}", 50, 1, [])
    result, status = scheduler.solve()
    assert result == "INFEASIBLE"


def test_adaptive_cap__never_below_longest_task():
    """A cap smaller than the longest single task guarantees INFEASIBLE at
    every horizon — the ladder then *lowers* it further. Floor it."""
    from app.utils.pacing import compute_adaptive_daily_cap

    cap = compute_adaptive_daily_cap(
        horizon_minutes=43200,          # 30 days — worst slack ratio
        total_task_minutes=120,         # two hours of work
        longest_task_minutes=25,
    )
    assert cap >= 25


def test_adaptive_cap__composable_from_task_atoms():
    """cap=63 with 25-min tasks means a day fits 2 tasks (50) but never 63 —
    5x25 over 2 days (50+75) is unschedulable. Round the cap up to whole tasks."""
    from app.utils.pacing import compute_adaptive_daily_cap

    cap = compute_adaptive_daily_cap(
        horizon_minutes=2880, total_task_minutes=125, longest_task_minutes=25,
    )
    assert cap % 25 == 0 and cap >= 75, f"cap {cap} not composable from 25-min atoms"


def test_run_schedule__five_atoms_two_days__feasible():
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata, TaskChunk
    from app.api.v1.endpoints import schedule as sep

    chunks = [TaskChunk(task_id=f"t{i}", title=f"part {i}", duration_minutes=25,
                        difficulty_weight=0.5, dependencies=[], completion_criteria="done")
              for i in range(5)]
    graph = ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                           cognitive_load_estimate={"intrinsic_load": 0.5})
    resp = sep.run_schedule(graph, [], horizon_minutes=2880)
    assert len(resp.schedule) == 5


def test_hard_blocks__overlapping_windows__do_not_self_conflict():
    """Two overlapping *fixed* hard blocks must not make the model INFEASIBLE.

    ``add_hard_block`` builds a constant interval and ``solve`` drops every block
    into one ``add_no_overlap`` set alongside the tasks. Two constant intervals
    that overlap each other are unsatisfiable on their own — before a single task
    exists. Real inputs overlap constantly: a habit block ("no study before 10am"
    -> 0-120) and a memory constraint (0-600) both start at day zero. Blocked time
    is a *union*, not a renewable resource, so overlapping blocks are coalesced
    before the NoOverlap rather than being asked to avoid one another.
    """
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(0, 120, "habit_morning")
    scheduler.add_hard_block(0, 600, "memory_constraint")
    scheduler.add_task("t1", 25, 5, [])

    result, _ = scheduler.solve()

    assert result != "INFEASIBLE"
    # Merging must not weaken the block: the union 0-600 is still off-limits.
    assert result["t1"]["start"] >= 600


def test_hard_blocks__adjacent_and_nested__union_still_enforced():
    """Coalescing keeps every blocked minute blocked, including nested blocks."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(0, 600, "outer")
    scheduler.add_hard_block(200, 300, "nested")
    scheduler.add_hard_block(600, 900, "adjacent")
    scheduler.add_task("t1", 25, 5, [])

    result, _ = scheduler.solve()

    assert result != "INFEASIBLE"
    assert result["t1"]["start"] >= 900


def test_free_minutes_per_day__overlapping_blocks__counted_once():
    """Overlapping blocks are summed today, so blocked time is double counted.

    A day blocked 0-120 and 0-600 has 600 blocked minutes, not 720; the summing
    version reports 840 free instead of 960 and narrows the daily cap on a number
    that was never real.
    """
    from app.schemas.context import Availability, TimeSlot
    from app.utils.pacing import compute_free_minutes_per_day

    ctx = [
        TimeSlot(name="a", start_min=0, end_min=120, availability=Availability.BLOCKED),
        TimeSlot(name="b", start_min=0, end_min=600, availability=Availability.BLOCKED),
    ]

    assert compute_free_minutes_per_day(ctx, 2880)[0] == 1440 - 600


def test_adaptive_cap__one_heavily_blocked_day__never_below_one_task_atom():
    """The free-per-day narrowing must not undo the task-atom floor.

    The cap is *global* — ``_build_daily_load_constraints`` applies it to every
    day — but ``min(free_per_day)`` comes from the single *worst* day. One almost
    fully blocked day therefore drags the cap under one task atom, and a cap below
    the longest task makes every day unschedulable, at every rung of the horizon
    ladder, even when later days are wide open.

    The floor is re-applied *after* the narrowing rather than the narrowing being
    dropped: narrowing is still the right pacing signal on a busy horizon, but the
    cap is a pacing heuristic, not a feasibility mechanism. Per-day blocked time is
    already enforced exactly by the hard-block NoOverlap, so the cap must never be
    the constraint that turns a solvable model INFEASIBLE.
    """
    from app.schemas.context import Availability, TimeSlot
    from app.utils.pacing import compute_adaptive_daily_cap

    ctx = [
        # Day 0 is booked solid bar 20 minutes; day 1 is wide open.
        TimeSlot(name="allday", start_min=0, end_min=1420, availability=Availability.BLOCKED),
        TimeSlot(name="sleep_d1", start_min=2400, end_min=2880, availability=Availability.BLOCKED),
    ]

    cap = compute_adaptive_daily_cap(
        horizon_minutes=2880,
        total_task_minutes=125,
        daily_context=ctx,
        longest_task_minutes=25,
    )

    assert cap >= 25, f"cap {cap} cannot fit a single 25-min task on any day"


def test_run_schedule__memory_block_overlapping_habit_block__feasible():
    """The live repro: a memory-bridge block overlapping an expanded habit block.

    A "no study before 10am" habit expands to a daily 0-120 block; the memory
    constraint bridge adds its own block starting at day zero. Both are hard, both
    are fixed, and they overlap — which used to be INFEASIBLE regardless of how
    much free time the horizon actually had.
    """
    from datetime import datetime, timezone

    from app.api.v1.endpoints import schedule as sep
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata, TaskChunk
    from app.schemas.context import Availability, SemanticTimeSlot, TimeSlot
    from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots

    slots = [SemanticTimeSlot(name="morning_restriction", start_min=0, end_min=120,
                              availability="blocked", recurrence="daily")]
    ctx = expand_semantic_slots_to_time_slots(slots, 2880, datetime.now(timezone.utc))
    # What the fixed bridge emits for "never studies before 11am": 8 AM -> 11 AM.
    ctx.append(TimeSlot(name="memory_constraint_x", start_min=0, end_min=180,
                        availability=Availability.BLOCKED, recurring=True))
    chunks = [TaskChunk(task_id=f"t{i}", title=f"p{i}", duration_minutes=25,
                        difficulty_weight=0.5, dependencies=[], completion_criteria="d")
              for i in range(5)]
    graph = ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                           cognitive_load_estimate={"intrinsic_load": 0.5})

    resp = sep.run_schedule(graph, ctx, horizon_minutes=2880)

    assert len(resp.schedule) == 5
    for task in resp.schedule.values():
        assert task.start_min >= 180, "task placed inside the blocked morning"
