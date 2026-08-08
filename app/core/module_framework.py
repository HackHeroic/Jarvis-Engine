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


@dataclass
class ModuleStep:
    """A single execution step in a module."""

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
    """Wrap a step handler with SSE emission, timeout, feature flags, and hooks."""

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
            if hook_result.decision in (HookDecision.DENY, HookDecision.ASK):
                _emit_tool_use(
                    queue, module_name, step.name, "skipped",
                    {"reason": hook_result.reason or "blocked by hook"},
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


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

END_SENTINEL = "__END__"


def build_module_graph(definition: ModuleDefinition):
    """Build a compiled LangGraph from a ModuleDefinition."""
    from langgraph.graph import END, StateGraph

    for step in definition.steps:
        step.module_name = definition.name

    graph = StateGraph(definition.state_class)

    lookup: dict[str, ModuleStep] = {}
    for step in definition.steps:
        lookup[step.name] = step
        wrapped = _wrap_step(step)
        graph.add_node(step.name, wrapped)

    def _resolve_end(destinations: dict[str, str]) -> dict:
        return {k: END if v == END_SENTINEL else v for k, v in destinations.items()}

    # Which steps does each step reach through a *branch* (routes_to or an
    # extra_edge)? Everything below is expressed against this map so that
    # "reaches conditionally" is decided per (source, target) pair rather than
    # per step — a step that routes somewhere is not thereby excused from its
    # obligations to every other step.
    conditional_reach: dict[str, set[str]] = {}

    def _record(source: str, destinations: dict[str, str]) -> None:
        conditional_reach.setdefault(source, set()).update(
            t for t in destinations.values() if t != END_SENTINEL
        )

    for s in definition.steps:
        if s.routes_to:
            for _cond, dests in s.routes_to.items():
                _record(s.name, dests)
    for e in definition.extra_edges:
        _record(e.from_step, e.destinations)

    routed_targets: set[str] = set()
    for targets in conditional_reach.values():
        routed_targets |= targets

    # Detect entry point
    if definition.entry_step:
        entry = definition.entry_step
    else:
        candidates = [s for s in definition.steps if not s.depends_on]
        entry_candidates = [s for s in candidates if s.name not in routed_targets]
        if len(entry_candidates) >= 1:
            entry = entry_candidates[0].name
        elif candidates:
            entry = candidates[0].name
        else:
            raise ValueError(
                f"Module '{definition.name}' has no entry point: all steps have depends_on"
            )
    graph.set_entry_point(entry)

    # Wire outgoing branches
    for step in definition.steps:
        if step.routes_to:
            for condition_fn, destinations in step.routes_to.items():
                graph.add_conditional_edges(step.name, condition_fn, _resolve_end(destinations))

    for edge in definition.extra_edges:
        graph.add_conditional_edges(edge.from_step, edge.condition, _resolve_end(edge.destinations))

    # Wire incoming dependencies. Independent of the loop above: routing OUT of
    # a step says nothing about what must complete before it runs, and a step
    # keeps its dependency edges even when some other step routes back INTO it
    # (solve_schedule is handle_infeasible's retry target and still needs its
    # forward edge from fuse_tasks).
    for step in definition.steps:
        plain_deps: list[str] = []
        for dep_name in step.depends_on:
            if dep_name not in lookup:
                raise ValueError(
                    f"Module '{definition.name}': step '{step.name}' depends on "
                    f"unknown step '{dep_name}'"
                )
            # A dep that branches to this step already reaches it through that
            # branch; a plain edge on top would fire on the arm not taken.
            if step.name not in conditional_reach.get(dep_name, ()):
                plain_deps.append(dep_name)

        if len(plain_deps) == 1:
            graph.add_edge(plain_deps[0], step.name)
        elif len(plain_deps) > 1:
            # List form is a `waiting_edge`: ALL of them must complete, and the
            # target fires exactly once. Separate add_edge calls would be
            # independent triggers and fire it once per dependency.
            graph.add_edge(plain_deps, step.name)

    # Detect terminal steps and wire to END
    depended_on: set[str] = set()
    for step in definition.steps:
        for dep in step.depends_on:
            depended_on.add(dep)
    has_outgoing: set[str] = {s.name for s in definition.steps if s.routes_to}
    has_outgoing |= {e.from_step for e in definition.extra_edges}

    for step in definition.steps:
        is_terminal = (
            step.name not in depended_on
            and step.name not in has_outgoing
        )
        if is_terminal:
            graph.add_edge(step.name, END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Module Registry
# ---------------------------------------------------------------------------


class ModuleRegistry:
    """Registry for cognitive modules. Compiles and caches LangGraph sub-graphs."""

    def __init__(self) -> None:
        self._modules: dict[str, ModuleDefinition] = {}
        self._compiled: dict[str, Any] = {}

    def register(self, definition: ModuleDefinition) -> None:
        for step in definition.steps:
            step.module_name = definition.name
        self._modules[definition.name] = definition
        self._compiled.pop(definition.name, None)

    def get_compiled(self, name: str) -> Any:
        if name not in self._compiled:
            if name not in self._modules:
                raise KeyError(f"No module '{name}' registered")
            self._compiled[name] = build_module_graph(self._modules[name])
        return self._compiled[name]

    def get_definition(self, name: str) -> ModuleDefinition:
        return self._modules[name]

    def registered_names(self) -> list[str]:
        return list(self._modules.keys())
