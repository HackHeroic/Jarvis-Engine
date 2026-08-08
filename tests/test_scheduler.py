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
