"""Integration tests: modules produce correct graph topology via ModuleStep framework."""

import pytest


def test_planning_module__has_correct_steps():
    from app.modules.planning_graph import planning_module

    step_names = [s.name for s in planning_module.steps]
    assert step_names == [
        # validate_goal leads: it gates every step below it, and a gate cannot
        # sit inside the fan-in it protects (a LangGraph AND-join ignores its
        # members' branches, so decompose_goal fired even on the __END__ arm).
        "validate_goal",
        "mark_missed",
        "fetch_constraints",
        "translate_habits",
        "expand_slots",
        "memory_to_constraints",
        "decompose_goal",
        "fuse_tasks",
        "solve_schedule",
        "create_draft",
        "handle_infeasible",
    ]


def test_planning_module__compiles():
    from app.modules.planning_graph import planning_module
    from app.core.module_framework import build_module_graph

    graph = build_module_graph(planning_module)
    assert graph is not None


def test_planning_module__parallel_fan_out():
    """fetch_constraints fans out to the two constraint branches...

    ...and `decompose_goal` joins them. The join is what makes the fan-out
    safe: without it each branch triggers the decomposition independently.
    """
    from app.modules.planning_graph import planning_module

    dependents = [s.name for s in planning_module.steps if "fetch_constraints" in s.depends_on]
    assert set(dependents) == {"translate_habits", "memory_to_constraints"}

    decompose = next(s for s in planning_module.steps if s.name == "decompose_goal")
    assert set(decompose.depends_on) == {"expand_slots", "memory_to_constraints"}


def test_planning_module__validate_goal_gates_the_pipeline():
    """The gate is the entry point and its True arm opens the fan-out."""
    from app.modules.planning_graph import planning_module

    assert planning_module.entry_step == "validate_goal"
    validate = next(s for s in planning_module.steps if s.name == "validate_goal")
    assert validate.depends_on == []
    for _cond, dests in validate.routes_to.items():
        assert dests[True] == "mark_missed"
        assert dests[False] == "__END__"


def test_planning_module__retry_loop():
    from app.modules.planning_graph import planning_module

    solve = next(s for s in planning_module.steps if s.name == "solve_schedule")
    assert solve.routes_to is not None
    for _cond, dests in solve.routes_to.items():
        assert dests["INFEASIBLE"] == "handle_infeasible"

    infeasible = next(s for s in planning_module.steps if s.name == "handle_infeasible")
    assert infeasible.routes_to is not None
    for _cond, dests in infeasible.routes_to.items():
        assert dests["retry"] == "solve_schedule"


def test_planning_module__state_mappers_exist():
    from app.modules.planning_graph import planning_module

    assert planning_module.state_in is not None
    assert planning_module.state_out is not None


def test_research_module__has_correct_steps():
    from app.modules.research_graph import research_module

    step_names = [s.name for s in research_module.steps]
    assert step_names == [
        "plan_research",
        "execute_search",
        "evaluate_results",
        "summarize",
        "link_to_tasks",
    ]


def test_research_module__self_loop():
    from app.modules.research_graph import research_module

    evaluate = next(s for s in research_module.steps if s.name == "evaluate_results")
    assert evaluate.routes_to is not None
    for _cond, dests in evaluate.routes_to.items():
        assert dests.get(True) == "execute_search"
        assert dests.get(False) == "summarize"


def test_research_module__compiles():
    from app.modules.research_graph import research_module
    from app.core.module_framework import build_module_graph

    graph = build_module_graph(research_module)
    assert graph is not None


def test_research_module__state_mappers_exist():
    from app.modules.research_graph import research_module

    assert research_module.state_in is not None
    assert research_module.state_out is not None


def test_knowledge_module__has_correct_steps():
    from app.modules.knowledge_graph import knowledge_module

    step_names = [s.name for s in knowledge_module.steps]
    assert step_names == [
        "classify_content",
        "extract_calendar",
        "ingest_document",
        "link_to_tasks",
        "propose_actions",
        "file_operations",
    ]


def test_knowledge_module__three_way_branch():
    from app.modules.knowledge_graph import knowledge_module

    classify = next(s for s in knowledge_module.steps if s.name == "classify_content")
    assert classify.routes_to is not None
    for _cond, dests in classify.routes_to.items():
        assert set(dests.values()) == {"extract_calendar", "ingest_document", "file_operations"}


def test_knowledge_module__compiles():
    from app.modules.knowledge_graph import knowledge_module
    from app.core.module_framework import build_module_graph

    graph = build_module_graph(knowledge_module)
    assert graph is not None


def test_knowledge_module__state_mappers_exist():
    from app.modules.knowledge_graph import knowledge_module

    assert knowledge_module.state_in is not None
    assert knowledge_module.state_out is not None
