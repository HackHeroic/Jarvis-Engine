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


import os


def test_is_feature_enabled__default_enabled():
    from app.core.config import is_feature_enabled
    os.environ.pop("JARVIS_ENABLE_PEARL", None)
    assert is_feature_enabled("ENABLE_PEARL") is True


def test_is_feature_enabled__explicitly_disabled(monkeypatch):
    from app.core.config import is_feature_enabled
    monkeypatch.setenv("JARVIS_ENABLE_PEARL", "0")
    assert is_feature_enabled("ENABLE_PEARL") is False


def test_is_feature_enabled__explicitly_enabled(monkeypatch):
    from app.core.config import is_feature_enabled
    monkeypatch.setenv("JARVIS_ENABLE_PEARL", "1")
    assert is_feature_enabled("ENABLE_PEARL") is True


import asyncio
import json


@pytest.mark.asyncio
async def test_wrap_step__emits_started_and_done():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def handler(state):
        return {"result": 42}

    step = ModuleStep(name="my_step", handler=handler, module_name="test_mod")
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {"result": 42}
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert len(events) == 2
    assert events[0]["status"] == "started"
    assert events[0]["tool"] == "my_step"
    assert events[0]["module"] == "test_mod"
    assert events[1]["status"] == "done"


@pytest.mark.asyncio
async def test_wrap_step__timeout_emits_error():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def slow_handler(state):
        await asyncio.sleep(5)
        return {}

    step = ModuleStep(name="slow", handler=slow_handler, module_name="test_mod", timeout_ms=100)
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {}
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert events[-1]["status"] == "error"
    assert events[-1]["detail"]["error"] == "timeout"


@pytest.mark.asyncio
async def test_wrap_step__feature_flag_disabled_skips(monkeypatch):
    from app.core.module_framework import ModuleStep, _wrap_step

    monkeypatch.setenv("JARVIS_ENABLE_TEST", "0")
    queue = asyncio.Queue()

    async def handler(state):
        return {"should_not": "run"}

    step = ModuleStep(name="gated", handler=handler, module_name="test_mod", feature_flag="ENABLE_TEST")
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {}
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert len(events) == 1
    assert events[0]["status"] == "skipped"


@pytest.mark.asyncio
async def test_wrap_step__handler_exception_emits_error():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def bad_handler(state):
        raise ValueError("something broke")

    step = ModuleStep(name="bad", handler=bad_handler, module_name="test_mod")
    wrapped = _wrap_step(step)

    with pytest.raises(ValueError, match="something broke"):
        await wrapped({"progress_queue": queue})

    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert events[-1]["status"] == "error"
    assert "something broke" in events[-1]["detail"]["error"]


@pytest.mark.asyncio
async def test_wrap_step__no_queue_no_crash():
    from app.core.module_framework import ModuleStep, _wrap_step

    async def handler(state):
        return {"ok": True}

    step = ModuleStep(name="quiet", handler=handler, module_name="test_mod")
    wrapped = _wrap_step(step)
    result = await wrapped({})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_wrap_step__tool_detail_extracted():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def handler(state):
        return {"data": [1, 2], "_tool_detail": {"rows": 2}}

    step = ModuleStep(name="detailed", handler=handler, module_name="test_mod")
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {"data": [1, 2]}  # _tool_detail stripped
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert events[1]["detail"] == {"rows": 2}
