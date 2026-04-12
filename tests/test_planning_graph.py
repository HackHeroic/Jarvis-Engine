# tests/test_planning_graph.py
import pytest
from app.modules.planning_graph import build_planning_graph


def test_planning_graph_compiles():
    graph = build_planning_graph()
    assert graph is not None


def test_planning_graph_has_expected_nodes():
    graph = build_planning_graph()
    node_names = set(graph.nodes.keys())
    expected = {
        "fetch_constraints",
        "translate_habits",
        "expand_slots",
        "memory_to_constraints",
        "validate_goal",
        "decompose_goal",
        "fuse_tasks",
        "solve_schedule",
        "handle_infeasible",
    }
    assert expected.issubset(node_names), f"Missing: {expected - node_names}"
