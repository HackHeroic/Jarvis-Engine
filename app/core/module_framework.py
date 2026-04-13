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
