"""Unit tests for OR-Tools JarvisScheduler and INFEASIBLE states."""

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


def test_infeasible_too_much_work():
    """Impossible constraint (50 hours of tasks in 24) should return INFEASIBLE."""
    scheduler = JarvisScheduler(horizon_minutes=1440)  # 24 hours
    scheduler.add_hard_block(0, 480, "sleep")  # 8h sleep
    # 50 hours of tasks in 24h horizon with 8h blocked
    for i in range(60):  # 60 tasks x 50 min = 3000 min > 1440
        scheduler.add_task(f"task_{i}", 50, 1, [])
    result, status = scheduler.solve()
    assert result == "INFEASIBLE"
