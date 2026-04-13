"""Tests for the ModuleStep execution framework."""

import pytest


def test_module_step__defaults():
    from app.core.module_framework import ModuleStep

    step = ModuleStep(name="fetch_data", handler=lambda s: {})
    assert step.name == "fetch_data"
    assert step.depends_on == []
    assert step.concurrent_safe is False
    assert step.read_only is False
    assert step.routes_to is None
    assert step.timeout_ms == 30_000
    assert step.hook_event is None
    assert step.feature_flag is None
    assert step.module_name == ""


def test_module_definition__auto_detects_entry_step():
    from app.core.module_framework import ModuleStep, ModuleDefinition

    steps = [
        ModuleStep(name="start", handler=lambda s: {}),
        ModuleStep(name="middle", handler=lambda s: {}, depends_on=["start"]),
        ModuleStep(name="end", handler=lambda s: {}, depends_on=["middle"]),
    ]
    defn = ModuleDefinition(name="test", state_class=dict, steps=steps)
    assert defn.entry_step is None
    entry_candidates = [s for s in defn.steps if not s.depends_on]
    assert len(entry_candidates) == 1
    assert entry_candidates[0].name == "start"


def test_conditional_edge__fields():
    from app.core.module_framework import ConditionalEdge

    edge = ConditionalEdge(
        from_step="solve",
        condition=lambda s: "ok",
        destinations={"ok": "__END__", "fail": "retry"},
    )
    assert edge.from_step == "solve"
    assert edge.destinations["ok"] == "__END__"
