# ModuleStep Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 3 hardcoded `build_*_graph()` functions with a generic `ModuleStep` + `build_module_graph()` framework that auto-handles SSE emission, timeouts, feature flags, and hook integration.

**Architecture:** Declarative `ModuleStep` dataclasses describe execution steps with metadata (depends_on, concurrent_safe, routes_to, feature_flag). A single `build_module_graph()` function builds LangGraph DAGs from these declarations. A generic `create_module_wrapper()` factory replaces per-module boilerplate in the orchestrator.

**Tech Stack:** Python 3.11+, LangGraph (StateGraph), asyncio, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-13-module-step-framework-design.md`

---

## File Structure

### New files

| File | Responsibility |
|------|---------------|
| `app/core/module_framework.py` | `ModuleStep`, `ConditionalEdge`, `ModuleDefinition` dataclasses. `ModuleRegistry` class. `build_module_graph()` function. `_wrap_step()` decorator. `_emit_tool_use()` helper. |
| `app/orchestrator/module_wrapper.py` | `create_module_wrapper()` factory — generates orchestrator nodes from registry. |
| `tests/test_module_framework.py` | Unit tests for framework: linear steps, parallel fan-out, fan-in, conditional routing, self-loops, feature flags, SSE emission, timeouts. |
| `tests/test_module_registration.py` | Integration tests: planning/research/knowledge modules produce correct graph topology via framework. |

### Modified files

| File | Changes |
|------|---------|
| `app/core/config.py` | Add `is_feature_enabled(flag)` function |
| `app/modules/__init__.py` | Add `module_registry` singleton + `register_default_modules()` |
| `app/modules/planning_graph.py` | Remove `build_planning_graph()`, `_emit_tool_use()`. Strip SSE calls from handlers. Add `planning_module = ModuleDefinition(...)` + state mappers. |
| `app/modules/research_graph.py` | Remove `build_research_graph()`. Add `research_module = ModuleDefinition(...)` + state mappers. |
| `app/modules/knowledge_graph.py` | Remove `build_knowledge_graph()`. Add `knowledge_module = ModuleDefinition(...)` + state mappers. |
| `app/orchestrator/graph.py` | Remove 3 wrapper functions + 3 compiled calls. Use `module_registry` + `create_module_wrapper()`. |
| `app/main.py` | Add `register_default_modules()` in lifespan. |
| `tests/test_planning_graph.py` | Update to test via framework instead of `build_planning_graph()`. |

---

### Task 1: Core Data Model

**Files:**
- Create: `app/core/module_framework.py`
- Test: `tests/test_module_framework.py`

- [ ] **Step 1: Write failing test for ModuleStep dataclass**

```python
# tests/test_module_framework.py
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
    # entry_step is None — builder will auto-detect "start" (no depends_on)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py -v`
Expected: FAIL with `ModuleNotFoundError` — `app.core.module_framework` does not exist yet.

- [ ] **Step 3: Implement the data model**

```python
# app/core/module_framework.py
"""Generic module execution framework for Jarvis.

Replaces hardcoded build_*_graph() functions with declarative ModuleStep
definitions. Inspired by Claude Code's Tool interface and StreamingToolExecutor.

See: docs/superpowers/specs/2026-04-13-module-step-framework-design.md
"""

from __future__ import annotations

import asyncio
import json as _json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.jarvis_logger import JARVIS_LOGGER as logger


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModuleStep:
    """A single execution step in a module.

    Mirrors Claude Code's Tool interface:
      handler         = Tool.call(args, ctx)
      concurrent_safe = Tool.isConcurrencySafe(input)
      read_only       = Tool.isReadOnly(input)
      hook_event      = Tool.checkPermissions(input, ctx)
      timeout_ms      ~ Tool.maxResultSizeChars
    """

    name: str
    handler: Callable[..., Awaitable[dict]]
    depends_on: list[str] = field(default_factory=list)
    concurrent_safe: bool = False
    read_only: bool = False
    routes_to: dict[Callable, dict[str, str]] | None = None
    timeout_ms: int = 30_000
    hook_event: str | None = None
    feature_flag: str | None = None
    module_name: str = ""


@dataclass
class ConditionalEdge:
    """Typed escape hatch for edges that don't fit routes_to."""

    from_step: str
    condition: Callable
    destinations: dict[str, str]


@dataclass
class ModuleDefinition:
    """Complete module declaration. Replaces build_*_graph() functions."""

    name: str
    state_class: type
    steps: list[ModuleStep]
    extra_edges: list[ConditionalEdge] = field(default_factory=list)
    entry_step: str | None = None
    state_in: Callable | None = None
    state_out: Callable | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/core/module_framework.py tests/test_module_framework.py
git commit -m "feat: add ModuleStep, ConditionalEdge, ModuleDefinition dataclasses"
```

---

### Task 2: `is_feature_enabled()` in config.py

**Files:**
- Modify: `app/core/config.py`
- Test: `tests/test_module_framework.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_module_framework.py`:

```python
import os


def test_is_feature_enabled__default_enabled():
    from app.core.config import is_feature_enabled

    # No env var set — should default to enabled
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py::test_is_feature_enabled__default_enabled -v`
Expected: FAIL with `ImportError` — `is_feature_enabled` not found.

- [ ] **Step 3: Add `is_feature_enabled` to config.py**

Read `app/core/config.py` and append at the end:

```python
def is_feature_enabled(flag: str) -> bool:
    """Check if a feature flag is enabled. Runtime check via env vars.

    Convention: JARVIS_{FLAG_NAME} env var. Default: enabled ("1").
    Set to "0" to disable.
    """
    return os.environ.get(f"JARVIS_{flag}", "1") == "1"
```

Ensure `import os` is at the top of `config.py` (it likely already is since the file reads env vars).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_module_framework.py
git commit -m "feat: add is_feature_enabled() runtime feature flag check"
```

---

### Task 3: `_emit_tool_use()` and `_wrap_step()`

**Files:**
- Modify: `app/core/module_framework.py`
- Test: `tests/test_module_framework.py`

- [ ] **Step 1: Write failing tests for _wrap_step**

Append to `tests/test_module_framework.py`:

```python
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
    # Should have 2 events: started + done
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
    result = await wrapped({})  # no progress_queue key
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py::test_wrap_step__emits_started_and_done -v`
Expected: FAIL with `ImportError` — `_wrap_step` not found.

- [ ] **Step 3: Implement `_emit_tool_use` and `_wrap_step`**

Add to `app/core/module_framework.py` after the dataclass definitions:

```python
# ---------------------------------------------------------------------------
# SSE emission
# ---------------------------------------------------------------------------


def _emit_tool_use(
    queue: Any, module_name: str, step_name: str, status: str, detail: dict | None = None
) -> None:
    """Emit a tool_use event onto the SSE progress queue."""
    if not queue:
        return
    event: dict[str, Any] = {
        "_event_type": "tool_use",
        "module": module_name,
        "tool": step_name,
        "status": status,
    }
    if detail:
        event["detail"] = detail
    queue.put_nowait(_json.dumps(event))


# ---------------------------------------------------------------------------
# Step wrapper
# ---------------------------------------------------------------------------


def _wrap_step(step: ModuleStep) -> Callable:
    """Wrap a step handler with SSE emission, timeout, feature flags, and hooks.

    Applied by build_module_graph() to every step. Module authors never call
    this directly — they write pure business logic handlers.
    """

    async def wrapped(state: dict) -> dict:
        queue = state.get("progress_queue")
        module_name = step.module_name

        # L8: Feature flag check
        if step.feature_flag:
            from app.core.config import is_feature_enabled

            if not is_feature_enabled(step.feature_flag):
                _emit_tool_use(
                    queue, module_name, step.name, "skipped",
                    {"reason": f"feature '{step.feature_flag}' disabled"},
                )
                return {}

        # L4/L5: Pre-step hook (if declared)
        if step.hook_event:
            from app.orchestrator.hooks import get_hooks, HookDecision

            hook_result = await get_hooks().execute(
                step.hook_event,
                module=module_name,
                module_id=module_name,
                step=step.name,
            )
            if hook_result.decision == HookDecision.DENY:
                _emit_tool_use(
                    queue, module_name, step.name, "skipped",
                    {"reason": hook_result.reason or "denied by hook"},
                )
                return {}

        # Emit started
        _emit_tool_use(queue, module_name, step.name, "started")

        try:
            result = await asyncio.wait_for(
                step.handler(state),
                timeout=step.timeout_ms / 1000,
            )
            detail = result.pop("_tool_detail", None) if isinstance(result, dict) else None
            _emit_tool_use(queue, module_name, step.name, "done", detail)
            return result

        except asyncio.TimeoutError:
            logger.warning(f"Step {module_name}.{step.name} timed out after {step.timeout_ms}ms")
            _emit_tool_use(queue, module_name, step.name, "error", {"error": "timeout"})
            return {}

        except Exception as e:
            logger.error(f"Step {module_name}.{step.name} failed: {e}")
            _emit_tool_use(queue, module_name, step.name, "error", {"error": str(e)})
            raise

    wrapped.__name__ = f"{step.module_name}__{step.name}"
    return wrapped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py -v`
Expected: 12 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/core/module_framework.py tests/test_module_framework.py
git commit -m "feat: add _wrap_step with SSE emission, timeout, feature flags"
```

---

### Task 4: `build_module_graph()`

**Files:**
- Modify: `app/core/module_framework.py`
- Test: `tests/test_module_framework.py`

- [ ] **Step 1: Write failing tests for the builder**

Append to `tests/test_module_framework.py`:

```python
from typing import TypedDict, Optional, Any


class SimpleState(TypedDict):
    value: int
    progress_queue: Any
    error: Optional[str]


@pytest.mark.asyncio
async def test_build_module_graph__linear_steps():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    call_order = []

    async def step_a(state):
        call_order.append("a")
        return {"value": state.get("value", 0) + 1}

    async def step_b(state):
        call_order.append("b")
        return {"value": state.get("value", 0) + 10}

    defn = ModuleDefinition(
        name="linear",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="a", handler=step_a),
            ModuleStep(name="b", handler=step_b, depends_on=["a"]),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"value": 0, "progress_queue": None, "error": None})

    assert call_order == ["a", "b"]
    assert result["value"] == 11  # 0 + 1 = 1, then 1 + 10 = 11


@pytest.mark.asyncio
async def test_build_module_graph__conditional_routes_to_end():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    def check_value(state) -> str:
        return "done" if state.get("value", 0) > 5 else "continue"

    async def step_a(state):
        return {"value": 10}

    async def step_b(state):
        return {"value": state.get("value", 0) + 100}

    defn = ModuleDefinition(
        name="conditional",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="a", handler=step_a,
                       routes_to={check_value: {"done": "__END__", "continue": "b"}}),
            ModuleStep(name="b", handler=step_b),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"value": 0, "progress_queue": None, "error": None})

    # step_a sets value=10, check_value returns "done", so step_b never runs
    assert result["value"] == 10


@pytest.mark.asyncio
async def test_build_module_graph__self_loop_terminates():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    def should_loop(state) -> str:
        return "again" if state.get("value", 0) < 3 else "stop"

    async def increment(state):
        return {"value": state.get("value", 0) + 1}

    async def finalize(state):
        return {"error": None}

    defn = ModuleDefinition(
        name="looper",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="inc", handler=increment,
                       routes_to={should_loop: {"again": "inc", "stop": "fin"}}),
            ModuleStep(name="fin", handler=finalize),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"value": 0, "progress_queue": None, "error": None})

    assert result["value"] == 3  # loops 3 times: 0->1->2->3, then stop->fin


@pytest.mark.asyncio
async def test_build_module_graph__three_way_branch():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    def route(state) -> str:
        v = state.get("value", 0)
        if v == 1:
            return "path_a"
        elif v == 2:
            return "path_b"
        return "path_c"

    async def classifier(state):
        return {}

    async def handler_a(state):
        return {"error": "took_a"}

    async def handler_b(state):
        return {"error": "took_b"}

    async def handler_c(state):
        return {"error": "took_c"}

    defn = ModuleDefinition(
        name="brancher",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="classify", handler=classifier,
                       routes_to={route: {"path_a": "a", "path_b": "b", "path_c": "c"}}),
            ModuleStep(name="a", handler=handler_a),
            ModuleStep(name="b", handler=handler_b),
            ModuleStep(name="c", handler=handler_c),
        ],
    )
    graph = build_module_graph(defn)

    r1 = await graph.ainvoke({"value": 1, "progress_queue": None, "error": None})
    assert r1["error"] == "took_a"

    r2 = await graph.ainvoke({"value": 2, "progress_queue": None, "error": None})
    assert r2["error"] == "took_b"

    r3 = await graph.ainvoke({"value": 99, "progress_queue": None, "error": None})
    assert r3["error"] == "took_c"


@pytest.mark.asyncio
async def test_build_module_graph__parallel_fan_out_fan_in():
    """Two concurrent_safe steps run after entry, then fan-in to a final step."""
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph
    from typing import Annotated

    def _merge(left, right):
        return left + right

    class FanState(TypedDict):
        progress_queue: Any
        items: Annotated[list, _merge]
        error: Optional[str]

    async def start(state):
        return {"items": ["start"]}

    async def branch_a(state):
        return {"items": ["a"]}

    async def branch_b(state):
        return {"items": ["b"]}

    async def collect(state):
        return {"items": ["done"]}

    defn = ModuleDefinition(
        name="fanout",
        state_class=FanState,
        steps=[
            ModuleStep(name="start", handler=start),
            ModuleStep(name="branch_a", handler=branch_a,
                       depends_on=["start"], concurrent_safe=True),
            ModuleStep(name="branch_b", handler=branch_b,
                       depends_on=["start"], concurrent_safe=True),
            ModuleStep(name="collect", handler=collect,
                       depends_on=["branch_a", "branch_b"]),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"progress_queue": None, "items": [], "error": None})

    # start + branch_a + branch_b (parallel) + collect, merged via _merge reducer
    assert "start" in result["items"]
    assert "a" in result["items"]
    assert "b" in result["items"]
    assert "done" in result["items"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py::test_build_module_graph__linear_steps -v`
Expected: FAIL with `ImportError` — `build_module_graph` not found.

- [ ] **Step 3: Implement `build_module_graph`**

Add to `app/core/module_framework.py` after `_wrap_step`:

```python
# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

END_SENTINEL = "__END__"


def build_module_graph(definition: ModuleDefinition):
    """Build a compiled LangGraph from a ModuleDefinition.

    Algorithm:
    1. Create StateGraph from state_class
    2. Wrap each step handler with _wrap_step
    3. Add wrapped handlers as nodes
    4. Detect entry point (step with no depends_on)
    5. Wire edges from depends_on relationships
    6. Wire routes_to as conditional edges
    7. Wire extra_edges
    8. Auto-wire terminal steps to END
    9. Compile and return
    """
    from langgraph.graph import END, StateGraph

    # Auto-set module_name on all steps
    for step in definition.steps:
        step.module_name = definition.name

    graph = StateGraph(definition.state_class)

    # Step lookup for O(1) access
    lookup: dict[str, ModuleStep] = {}
    for step in definition.steps:
        lookup[step.name] = step
        wrapped = _wrap_step(step)
        graph.add_node(step.name, wrapped)

    # Translate END_SENTINEL to LangGraph END in routes_to
    def _resolve_end(destinations: dict[str, str]) -> dict:
        return {k: END if v == END_SENTINEL else v for k, v in destinations.items()}

    # Detect entry point
    if definition.entry_step:
        entry = definition.entry_step
    else:
        candidates = [s for s in definition.steps if not s.depends_on]
        # Also exclude steps that are only reachable via routes_to
        routed_targets: set[str] = set()
        for s in definition.steps:
            if s.routes_to:
                for _cond, dests in s.routes_to.items():
                    for target in dests.values():
                        if target != END_SENTINEL:
                            routed_targets.add(target)
        entry_candidates = [s for s in candidates if s.name not in routed_targets]
        if len(entry_candidates) == 1:
            entry = entry_candidates[0].name
        elif len(entry_candidates) > 1:
            # Multiple entry points — use first (user should set entry_step explicitly)
            entry = entry_candidates[0].name
        else:
            # All no-dep steps are routed targets — fall back to first no-dep step
            if candidates:
                entry = candidates[0].name
            else:
                raise ValueError(
                    f"Module '{definition.name}' has no entry point: "
                    "all steps have depends_on"
                )
    graph.set_entry_point(entry)

    # Collect steps that are destinations of routes_to (they get edges from routing, not depends_on)
    routed_destinations: set[str] = set()
    for step in definition.steps:
        if step.routes_to:
            for _cond, dests in step.routes_to.items():
                for target in dests.values():
                    if target != END_SENTINEL:
                        routed_destinations.add(target)

    # Wire edges
    for step in definition.steps:
        if step.routes_to:
            # Conditional edges from this step
            for condition_fn, destinations in step.routes_to.items():
                graph.add_conditional_edges(step.name, condition_fn, _resolve_end(destinations))
        elif step.depends_on and step.name not in routed_destinations:
            # Plain edges from dependencies to this step
            for dep_name in step.depends_on:
                dep_step = lookup.get(dep_name)
                if dep_step and not dep_step.routes_to:
                    graph.add_edge(dep_name, step.name)
                # If dep has routes_to, it handles routing to this step via conditional edges

    # Wire extra_edges
    for edge in definition.extra_edges:
        graph.add_conditional_edges(edge.from_step, edge.condition, _resolve_end(edge.destinations))

    # Detect terminal steps and wire to END
    depended_on: set[str] = set()
    for step in definition.steps:
        for dep in step.depends_on:
            depended_on.add(dep)
    # Also include steps that are sources of routes_to (they route elsewhere, not terminal)
    has_routes: set[str] = {s.name for s in definition.steps if s.routes_to}
    # Also include steps that are sources of extra_edges
    has_extra_routes: set[str] = {e.from_step for e in definition.extra_edges}

    for step in definition.steps:
        is_terminal = (
            step.name not in depended_on
            and step.name not in has_routes
            and step.name not in has_extra_routes
            and step.name not in routed_destinations  # routed targets get END from their router
        )
        if is_terminal:
            graph.add_edge(step.name, END)

    return graph.compile()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py -v`
Expected: 17 PASSED

Note: If any test fails due to edge-wiring logic, debug by checking which edges LangGraph creates. The builder logic for `routed_destinations` may need adjustment depending on how LangGraph handles incoming edges to routed targets. Fix and re-run until all pass.

- [ ] **Step 5: Commit**

```bash
git add app/core/module_framework.py tests/test_module_framework.py
git commit -m "feat: add build_module_graph() — generic LangGraph DAG builder"
```

---

### Task 5: `ModuleRegistry`

**Files:**
- Modify: `app/core/module_framework.py`
- Test: `tests/test_module_framework.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_module_framework.py`:

```python
@pytest.mark.asyncio
async def test_module_registry__register_and_compile():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry

    async def noop(state):
        return {}

    defn = ModuleDefinition(
        name="test_mod",
        state_class=SimpleState,
        steps=[ModuleStep(name="only", handler=noop)],
    )
    registry = ModuleRegistry()
    registry.register(defn)

    assert "test_mod" in registry.registered_names()

    compiled = registry.get_compiled("test_mod")
    result = await compiled.ainvoke({"value": 0, "progress_queue": None, "error": None})
    assert isinstance(result, dict)


def test_module_registry__get_compiled_caches():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry

    async def noop(state):
        return {}

    defn = ModuleDefinition(
        name="cached",
        state_class=SimpleState,
        steps=[ModuleStep(name="only", handler=noop)],
    )
    registry = ModuleRegistry()
    registry.register(defn)

    c1 = registry.get_compiled("cached")
    c2 = registry.get_compiled("cached")
    assert c1 is c2  # same object — cached


def test_module_registry__unknown_module_raises():
    from app.core.module_framework import ModuleRegistry

    registry = ModuleRegistry()
    with pytest.raises(KeyError, match="No module 'ghost'"):
        registry.get_compiled("ghost")


def test_module_registry__re_register_invalidates_cache():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry

    async def noop(state):
        return {}

    defn = ModuleDefinition(
        name="evolving",
        state_class=SimpleState,
        steps=[ModuleStep(name="v1", handler=noop)],
    )
    registry = ModuleRegistry()
    registry.register(defn)
    c1 = registry.get_compiled("evolving")

    defn2 = ModuleDefinition(
        name="evolving",
        state_class=SimpleState,
        steps=[ModuleStep(name="v2", handler=noop)],
    )
    registry.register(defn2)
    c2 = registry.get_compiled("evolving")
    assert c1 is not c2  # cache invalidated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py::test_module_registry__register_and_compile -v`
Expected: FAIL with `ImportError` — `ModuleRegistry` not found.

- [ ] **Step 3: Implement ModuleRegistry**

Add to `app/core/module_framework.py` after `build_module_graph`:

```python
# ---------------------------------------------------------------------------
# Module Registry
# ---------------------------------------------------------------------------


class ModuleRegistry:
    """Registry for cognitive modules. Compiles and caches LangGraph sub-graphs."""

    def __init__(self) -> None:
        self._modules: dict[str, ModuleDefinition] = {}
        self._compiled: dict[str, Any] = {}

    def register(self, definition: ModuleDefinition) -> None:
        """Register a module definition. Invalidates cached compilation."""
        for step in definition.steps:
            step.module_name = definition.name
        self._modules[definition.name] = definition
        self._compiled.pop(definition.name, None)

    def get_compiled(self, name: str) -> Any:
        """Get compiled graph, building lazily on first access."""
        if name not in self._compiled:
            if name not in self._modules:
                raise KeyError(f"No module '{name}' registered")
            self._compiled[name] = build_module_graph(self._modules[name])
        return self._compiled[name]

    def get_definition(self, name: str) -> ModuleDefinition:
        """Get the raw module definition."""
        return self._modules[name]

    def registered_names(self) -> list[str]:
        """List all registered module names."""
        return list(self._modules.keys())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py -v`
Expected: 21 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/core/module_framework.py tests/test_module_framework.py
git commit -m "feat: add ModuleRegistry with lazy compilation and cache invalidation"
```

---

### Task 6: Re-register Planning Module

**Files:**
- Modify: `app/modules/planning_graph.py`
- Test: `tests/test_module_registration.py`

- [ ] **Step 1: Write failing integration test**

```python
# tests/test_module_registration.py
"""Integration tests: modules produce correct graph topology via ModuleStep framework."""

import pytest


def test_planning_module__has_correct_steps():
    from app.modules.planning_graph import planning_module

    step_names = [s.name for s in planning_module.steps]
    assert step_names == [
        "fetch_constraints",
        "translate_habits",
        "expand_slots",
        "memory_to_constraints",
        "validate_goal",
        "decompose_goal",
        "fuse_tasks",
        "solve_schedule",
        "handle_infeasible",
    ]


def test_planning_module__compiles():
    from app.modules.planning_graph import planning_module
    from app.core.module_framework import build_module_graph

    graph = build_module_graph(planning_module)
    # Should have all 9 nodes
    assert graph is not None


def test_planning_module__parallel_fan_out():
    from app.modules.planning_graph import planning_module

    # fetch_constraints has 3 dependents: translate_habits, memory_to_constraints, validate_goal
    fetch = next(s for s in planning_module.steps if s.name == "fetch_constraints")
    dependents = [s.name for s in planning_module.steps if "fetch_constraints" in s.depends_on]
    assert set(dependents) == {"translate_habits", "memory_to_constraints", "validate_goal"}
    assert fetch.concurrent_safe is True


def test_planning_module__retry_loop():
    from app.modules.planning_graph import planning_module

    solve = next(s for s in planning_module.steps if s.name == "solve_schedule")
    assert solve.routes_to is not None
    # Should route to handle_infeasible on INFEASIBLE
    for _cond, dests in solve.routes_to.items():
        assert "INFEASIBLE" in dests
        assert dests["INFEASIBLE"] == "handle_infeasible"

    infeasible = next(s for s in planning_module.steps if s.name == "handle_infeasible")
    assert infeasible.routes_to is not None
    # Should route back to solve_schedule on retry
    for _cond, dests in infeasible.routes_to.items():
        assert "retry" in dests
        assert dests["retry"] == "solve_schedule"


def test_planning_module__state_mappers_exist():
    from app.modules.planning_graph import planning_module

    assert planning_module.state_in is not None
    assert planning_module.state_out is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_registration.py -v`
Expected: FAIL with `ImportError` — `planning_module` not found in `planning_graph`.

- [ ] **Step 3: Refactor planning_graph.py**

Read `app/modules/planning_graph.py`. Make these changes:

**a) Remove `_emit_tool_use` function** (lines 34-47) — the wrapper handles this now.

**b) Strip all `_emit_tool_use()` calls from handlers.** Each handler should only contain business logic. Remove lines like:
- `_emit_tool_use(state, "fetch_constraints", "started")` 
- `_emit_tool_use(state, "fetch_constraints", "done", {"rows": len(constraints)})`
- All similar calls in `translate_habits`, `decompose_goal`, `solve_schedule`, `memory_to_constraints`, `handle_infeasible`

For handlers that want to pass detail to the SSE event (like `{"rows": len(constraints)}`), return it under the `_tool_detail` key:

```python
async def fetch_constraints(state: PlanningState) -> dict:
    cb = state.get("progress_callback")
    if cb:
        cb("habits_fetched")
    user_model = state["user_model"]
    if user_model:
        constraints = await user_model.get_behavioral_constraints()
        habits_text = "\n".join(
            c.get("raw_text", "") for c in constraints if c.get("constraint_type") == "habit"
        )
        return {
            "constraints": constraints,
            "habits_text": habits_text,
            "_tool_detail": {"rows": len(constraints)},
        }
    return {"constraints": [], "habits_text": ""}
```

Apply the same pattern to all handlers that had detail in their `_emit_tool_use` calls. Handlers that had no detail just return their normal dict.

**c) Remove `progress_callback` calls** from all handlers. The `cb = state.get("progress_callback"); if cb: cb(...)` pattern is no longer needed — SSE emission is automatic.

**d) Remove `build_planning_graph()` function** (lines 291-345).

**e) Add the ModuleDefinition and state mappers** at the bottom of the file:

```python
from app.core.module_framework import ModuleStep, ModuleDefinition


def planning_state_in(state) -> dict:
    user_model = state.get("user_model")
    brain_dump = state.get("brain_dump")
    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "planning_goal": (
            brain_dump.planning_goal
            if brain_dump and hasattr(brain_dump, "planning_goal")
            else state.get("user_message", "")
        ),
        "habits_text": "",
        "semantic_slots": [],
        "time_slots": [],
        "constraints": [],
        "task_chunks": [],
        "pending_tasks": [],
        "schedule": None,
        "horizon_minutes": 2880,
        "retry_count": 0,
        "clarification_request": None,
        "error": None,
        "progress_callback": state.get("progress_callback"),
        "progress_queue": state.get("progress_queue"),
    }


def planning_state_out(result: dict, module_name: str) -> dict:
    return {
        "schedule": result.get("schedule"),
        "execution_graph": (
            {"decomposition": result.get("task_chunks", [])}
            if result.get("task_chunks")
            else None
        ),
        "clarification_request": result.get("clarification_request"),
        "error": result.get("error"),
    }


planning_module = ModuleDefinition(
    name="planning",
    state_class=PlanningState,
    state_in=planning_state_in,
    state_out=planning_state_out,
    steps=[
        ModuleStep(name="fetch_constraints", handler=fetch_constraints, concurrent_safe=True),
        ModuleStep(name="translate_habits", handler=translate_habits,
                   depends_on=["fetch_constraints"], timeout_ms=45_000),
        ModuleStep(name="expand_slots", handler=expand_slots,
                   depends_on=["translate_habits"], concurrent_safe=True, read_only=True),
        ModuleStep(name="memory_to_constraints", handler=memory_to_constraints,
                   depends_on=["fetch_constraints"], concurrent_safe=True,
                   feature_flag="ENABLE_PEARL"),
        ModuleStep(name="validate_goal", handler=validate_goal,
                   depends_on=["fetch_constraints"], concurrent_safe=True, read_only=True,
                   routes_to={is_goal_clear: {True: "decompose_goal", False: "__END__"}}),
        ModuleStep(name="decompose_goal", handler=decompose_goal,
                   depends_on=["expand_slots", "memory_to_constraints", "validate_goal"],
                   timeout_ms=60_000),
        ModuleStep(name="fuse_tasks", handler=fuse_tasks, depends_on=["decompose_goal"]),
        ModuleStep(name="solve_schedule", handler=solve_schedule,
                   depends_on=["fuse_tasks"],
                   routes_to={check_feasibility: {"OPTIMAL": "__END__", "INFEASIBLE": "handle_infeasible"}}),
        ModuleStep(name="handle_infeasible", handler=handle_infeasible,
                   routes_to={can_retry: {"retry": "solve_schedule", "exhausted": "__END__"}}),
    ],
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_registration.py -v`
Expected: 5 PASSED

Also run existing tests to check nothing broke:
Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v`

- [ ] **Step 5: Commit**

```bash
git add app/modules/planning_graph.py tests/test_module_registration.py
git commit -m "refactor: re-register planning module via ModuleStep framework"
```

---

### Task 7: Re-register Research Module

**Files:**
- Modify: `app/modules/research_graph.py`
- Test: `tests/test_module_registration.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_module_registration.py`:

```python
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
        assert dests.get(True) == "execute_search"  # self-loop
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_registration.py::test_research_module__has_correct_steps -v`
Expected: FAIL — `research_module` not found.

- [ ] **Step 3: Refactor research_graph.py**

Read `app/modules/research_graph.py`. Remove `build_research_graph()`. Add:

```python
from app.core.module_framework import ModuleStep, ModuleDefinition


def research_state_in(state) -> dict:
    user_model = state.get("user_model")
    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "query": state.get("user_message", ""),
        "search_results": [],
        "iteration_count": 0,
        "max_iterations": 3,
        "summary": None,
        "linked_tasks": [],
        "error": None,
    }


def research_state_out(result: dict, module_name: str) -> dict:
    return {
        "research_results": result.get("search_results"),
        "error": result.get("error"),
    }


research_module = ModuleDefinition(
    name="research",
    state_class=ResearchState,
    state_in=research_state_in,
    state_out=research_state_out,
    steps=[
        ModuleStep(name="plan_research", handler=plan_research),
        ModuleStep(name="execute_search", handler=execute_search,
                   depends_on=["plan_research"], timeout_ms=30_000),
        ModuleStep(name="evaluate_results", handler=lambda s: {},
                   depends_on=["execute_search"], read_only=True,
                   routes_to={needs_more: {True: "execute_search", False: "summarize"}}),
        ModuleStep(name="summarize", handler=summarize, timeout_ms=45_000),
        ModuleStep(name="link_to_tasks", handler=link_to_tasks,
                   depends_on=["summarize"]),
    ],
)
```

Keep `ResearchState`, all handler functions, and `needs_more` condition function unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_registration.py -v`
Expected: 9 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/modules/research_graph.py tests/test_module_registration.py
git commit -m "refactor: re-register research module via ModuleStep framework"
```

---

### Task 8: Re-register Knowledge Module

**Files:**
- Modify: `app/modules/knowledge_graph.py`
- Test: `tests/test_module_registration.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_module_registration.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_registration.py::test_knowledge_module__has_correct_steps -v`
Expected: FAIL — `knowledge_module` not found.

- [ ] **Step 3: Refactor knowledge_graph.py**

Read `app/modules/knowledge_graph.py`. Remove `build_knowledge_graph()`. Add:

```python
from app.core.module_framework import ModuleStep, ModuleDefinition


def knowledge_state_in(state) -> dict:
    user_model = state.get("user_model")
    _file_bytes = None
    _file_b64 = state.get("file_base64")
    if _file_b64:
        import base64
        _file_bytes = base64.b64decode(_file_b64)

    _db_client = getattr(user_model, "_db", None) if user_model else None

    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "db_client": _db_client,
        "content": state.get("user_message", ""),
        "file_bytes": _file_bytes,
        "media_type": state.get("file_media_type"),
        "file_name": state.get("file_name"),
        "content_type": None,
        "ingestion_result": None,
        "calendar_result": None,
        "linked_tasks": [],
        "action_proposals": [],
        "error": None,
    }


def knowledge_state_out(result: dict, module_name: str) -> dict:
    return {
        "ingestion_result": result.get("ingestion_result"),
        "error": result.get("error"),
    }


knowledge_module = ModuleDefinition(
    name="knowledge",
    state_class=KnowledgeState,
    state_in=knowledge_state_in,
    state_out=knowledge_state_out,
    steps=[
        ModuleStep(name="classify_content", handler=classify_content, read_only=True,
                   routes_to={content_type_router: {
                       "calendar": "extract_calendar",
                       "document": "ingest_document",
                       "file_op": "file_operations",
                   }}),
        ModuleStep(name="extract_calendar", handler=extract_calendar),
        ModuleStep(name="ingest_document", handler=ingest_document, timeout_ms=60_000),
        ModuleStep(name="link_to_tasks", handler=link_to_tasks,
                   depends_on=["ingest_document"]),
        ModuleStep(name="propose_actions", handler=propose_actions,
                   depends_on=["link_to_tasks"]),
        ModuleStep(name="file_operations", handler=file_operations),
    ],
)
```

Keep `KnowledgeState`, all handlers, and `content_type_router` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_registration.py -v`
Expected: 13 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/modules/knowledge_graph.py tests/test_module_registration.py
git commit -m "refactor: re-register knowledge module via ModuleStep framework"
```

---

### Task 9: Module Registry Initialization

**Files:**
- Modify: `app/modules/__init__.py`
- Modify: `app/main.py`

- [ ] **Step 1: Implement `app/modules/__init__.py`**

```python
# app/modules/__init__.py
"""Cognitive module registry — extensible module execution.

Uses ModuleRegistry from app/core/module_framework.py. Adding a new module
requires only a ModuleDefinition + a register() call.
"""

from app.core.module_framework import ModuleRegistry

module_registry = ModuleRegistry()


def register_default_modules() -> None:
    """Register all built-in modules. Called during app lifespan startup."""
    from app.modules.planning_graph import planning_module
    from app.modules.research_graph import research_module
    from app.modules.knowledge_graph import knowledge_module

    module_registry.register(planning_module)
    module_registry.register(research_module)
    module_registry.register(knowledge_module)
```

- [ ] **Step 2: Add `register_default_modules()` to main.py lifespan**

Read `app/main.py` and find the lifespan function. It likely already calls `register_default_intents()` and/or `register_default_document_types()`. Add `register_default_modules()` alongside them:

```python
from app.modules import register_default_modules

# Inside the lifespan function, after other registrations:
register_default_modules()
```

- [ ] **Step 3: Verify imports work**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.modules import module_registry, register_default_modules; register_default_modules(); print(module_registry.registered_names())"`
Expected: `['planning', 'research', 'knowledge']`

- [ ] **Step 4: Commit**

```bash
git add app/modules/__init__.py app/main.py
git commit -m "feat: add module_registry singleton and register at startup"
```

---

### Task 10: Generic Orchestrator Wrapper

**Files:**
- Create: `app/orchestrator/module_wrapper.py`
- Test: `tests/test_module_framework.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_module_framework.py`:

```python
@pytest.mark.asyncio
async def test_create_module_wrapper__invokes_module():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry
    from app.orchestrator.module_wrapper import create_module_wrapper

    executed = []

    async def my_handler(state):
        executed.append(True)
        return {"result_val": 99}

    defn = ModuleDefinition(
        name="wrapper_test",
        state_class=SimpleState,
        steps=[ModuleStep(name="only", handler=my_handler)],
        state_in=lambda s: {"value": 0, "progress_queue": None, "error": None},
        state_out=lambda r, n: {"schedule": r.get("result_val")},
    )

    registry = ModuleRegistry()
    registry.register(defn)

    wrapper = create_module_wrapper("wrapper_test", registry)
    result = await wrapper({
        "initiated_by": "user",
        "modules_invoked": [],
        "progress_queue": None,
    })

    assert executed == [True]
    assert result["modules_invoked"] == ["wrapper_test"]
    assert result.get("schedule") == 99
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py::test_create_module_wrapper__invokes_module -v`
Expected: FAIL — `module_wrapper` not found.

- [ ] **Step 3: Implement module_wrapper.py**

```python
# app/orchestrator/module_wrapper.py
"""Generic orchestrator wrapper for module sub-graphs.

Replaces _planning_module_node, _knowledge_module_node, _research_agent_node
in graph.py with a single factory function.

Mirrors Claude Code's sub-agent isolation:
- Module gets its own state (like sub-agent's own ToolUseContext)
- Hooks fire with module_id scope (like agentId-scoped session hooks)
- Orchestrator sees only the output dict (like AgentToolResult)
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.module_framework import ModuleRegistry


def create_module_wrapper(module_name: str, registry: ModuleRegistry) -> Callable:
    """Generate an orchestrator node that wraps a module sub-graph."""

    async def wrapper(state: dict) -> dict:
        from app.orchestrator.hooks import get_hooks

        hooks = get_hooks()

        # Pre-module hook with module-scoped context
        pre = await hooks.execute(
            "PreModuleExecution",
            module=module_name,
            module_id=module_name,
            initiated_by=state.get("initiated_by", "user"),
        )
        if pre.decision.value == "deny":
            return {
                "response_message": pre.reason or "Action blocked.",
                "modules_invoked": state.get("modules_invoked", []) + [module_name],
            }
        if pre.decision.value == "ask":
            return {
                "response_message": pre.reason or "Action requires consent.",
                "needs_consent": True,
                "modules_invoked": state.get("modules_invoked", []) + [module_name],
            }

        # State translation: JarvisState -> module state
        definition = registry.get_definition(module_name)
        if definition.state_in:
            module_state = definition.state_in(state)
        else:
            module_state = dict(state)

        # Invoke compiled sub-graph (isolation boundary)
        compiled = registry.get_compiled(module_name)
        result = await compiled.ainvoke(module_state)

        # Extract results: module state -> JarvisState updates
        if definition.state_out:
            output = definition.state_out(result, module_name)
        else:
            output = {}

        output["modules_invoked"] = state.get("modules_invoked", []) + [module_name]
        return output

    wrapper.__name__ = f"{module_name}_node"
    return wrapper
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_module_framework.py -v`
Expected: 22 PASSED (all previous + new wrapper test)

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator/module_wrapper.py tests/test_module_framework.py
git commit -m "feat: add create_module_wrapper() — generic orchestrator node factory"
```

---

### Task 11: Simplify `build_jarvis_graph()`

**Files:**
- Modify: `app/orchestrator/graph.py`

- [ ] **Step 1: Read current graph.py**

Read `app/orchestrator/graph.py` in full. Identify:
- The 3 compiled graph module-level variables (`_planning_compiled`, `_knowledge_compiled`, `_research_compiled`)
- The 3 wrapper functions (`_planning_module_node`, `_knowledge_module_node`, `_research_agent_node`)
- The `build_jarvis_graph()` function
- The imports for `build_planning_graph`, `build_knowledge_graph`, `build_research_graph`

- [ ] **Step 2: Remove old wrapper code**

Delete:
- Import: `from app.modules.planning_graph import build_planning_graph`
- Import: `from app.modules.knowledge_graph import build_knowledge_graph`
- Import: `from app.modules.research_graph import build_research_graph`
- `_planning_compiled = build_planning_graph()` (module-level)
- `_knowledge_compiled = build_knowledge_graph()` (module-level)
- `_research_compiled = build_research_graph()` (module-level)
- Entire `_planning_module_node` function (~45 lines)
- Entire `_knowledge_module_node` function (~50 lines)
- Entire `_research_agent_node` function (~35 lines)

- [ ] **Step 3: Add new imports and simplify build_jarvis_graph()**

Add at the top of `graph.py`:

```python
from app.modules import module_registry
from app.orchestrator.module_wrapper import create_module_wrapper
```

Replace the module node registrations inside `build_jarvis_graph()`. Change:

```python
graph.add_node("planning_module", _planning_module_node)
graph.add_node("research_agent", _research_agent_node)
graph.add_node("knowledge_module", _knowledge_module_node)
```

To:

```python
for name in module_registry.registered_names():
    graph.add_node(name, create_module_wrapper(name, module_registry))
```

Replace the per-module edge wiring. Change:

```python
for module in ["planning_module", "research_agent", "coach_module", "knowledge_module"]:
    graph.add_edge(module, "synthesize_response")
```

To:

```python
for name in module_registry.registered_names():
    graph.add_edge(name, "synthesize_response")
graph.add_edge("coach_module", "synthesize_response")
```

Keep everything else unchanged: `_load_context`, `_extract_brain_dump`, `_classify_intent`, `run_general_chat`, `run_coaching_response`, `voice_of_jarvis_synthesis`, `run_observation_loop`, all routing functions, all conditional edges.

- [ ] **Step 4: Verify the orchestrator graph still builds**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.modules import register_default_modules; register_default_modules(); from app.orchestrator.graph import build_jarvis_graph; g = build_jarvis_graph(); print('Graph built successfully')"`
Expected: `Graph built successfully`

- [ ] **Step 5: Update existing test_planning_graph.py**

Read `tests/test_planning_graph.py`. It currently tests `build_planning_graph()` which no longer exists. Update it to test via the framework:

```python
# tests/test_planning_graph.py
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
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v`
Expected: All tests pass. No regressions.

- [ ] **Step 7: Commit**

```bash
git add app/orchestrator/graph.py tests/test_planning_graph.py
git commit -m "refactor: simplify build_jarvis_graph() using module_registry + generic wrapper"
```

---

### Task 12: Final Verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 2: Verify SSE event format unchanged**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "
import asyncio, json
from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph
from typing import TypedDict, Any, Optional

class TestState(TypedDict):
    value: int
    progress_queue: Any
    error: Optional[str]

async def step_a(state):
    return {'value': 42, '_tool_detail': {'msg': 'hello'}}

defn = ModuleDefinition(name='sse_test', state_class=TestState, steps=[
    ModuleStep(name='a', handler=step_a),
])
graph = build_module_graph(defn)
q = asyncio.Queue()
result = asyncio.run(graph.ainvoke({'value': 0, 'progress_queue': q, 'error': None}))
events = []
while not q.empty():
    events.append(json.loads(q.get_nowait()))
for e in events:
    print(json.dumps(e, indent=2))
print(f'Result value: {result[\"value\"]}')
"`

Expected output:
```json
{
  "_event_type": "tool_use",
  "module": "sse_test",
  "tool": "a",
  "status": "started"
}
{
  "_event_type": "tool_use",
  "module": "sse_test",
  "tool": "a",
  "status": "done",
  "detail": {"msg": "hello"}
}
```
`Result value: 42`

- [ ] **Step 3: Verify no leftover references to old functions**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && grep -r "build_planning_graph\|build_research_graph\|build_knowledge_graph" app/ --include="*.py" | grep -v __pycache__`
Expected: No matches (all references removed).

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && grep -r "_emit_tool_use" app/ --include="*.py" | grep -v __pycache__ | grep -v module_framework`
Expected: No matches outside `module_framework.py` (all manual SSE calls removed from handlers).

- [ ] **Step 4: Final commit (if any cleanup needed)**

```bash
git add -A
git commit -m "chore: final cleanup after ModuleStep framework migration"
```
