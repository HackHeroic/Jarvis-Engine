"""Tests for the planning module graph topology."""

from app.modules.planning_graph import planning_module
from app.core.module_framework import build_module_graph


def test_planning_graph_compiles():
    """Planning module compiles via ModuleStep framework."""
    graph = build_module_graph(planning_module)
    assert graph is not None


def test_planning_graph_has_expected_steps():
    """Planning module has all 9 expected steps."""
    expected = {
        "fetch_constraints", "translate_habits", "expand_slots",
        "memory_to_constraints", "validate_goal", "decompose_goal",
        "fuse_tasks", "solve_schedule", "handle_infeasible",
    }
    actual = {s.name for s in planning_module.steps}
    assert actual == expected
