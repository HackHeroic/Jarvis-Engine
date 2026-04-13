"""Generic orchestrator wrapper for module sub-graphs.

Replaces _planning_module_node, _knowledge_module_node, _research_agent_node
in graph.py with a single factory function.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.module_framework import ModuleRegistry


def create_module_wrapper(module_name: str, registry: ModuleRegistry) -> Callable:
    """Generate an orchestrator node that wraps a module sub-graph."""

    async def wrapper(state: dict) -> dict:
        from app.orchestrator.hooks import get_hooks, HookDecision

        hooks = get_hooks()

        pre = await hooks.execute(
            "PreModuleExecution",
            module=module_name,
            module_id=module_name,
            initiated_by=state.get("initiated_by", "user"),
        )
        if pre.decision == HookDecision.DENY:
            return {
                "response_message": pre.reason or "Action blocked.",
                "modules_invoked": state.get("modules_invoked", []) + [module_name],
            }
        if pre.decision == HookDecision.ASK:
            return {
                "response_message": pre.reason or "Action requires consent.",
                "needs_consent": True,
                "modules_invoked": state.get("modules_invoked", []) + [module_name],
            }

        definition = registry.get_definition(module_name)
        if definition.state_in:
            module_state = definition.state_in(state)
        else:
            module_state = dict(state)

        compiled = registry.get_compiled(module_name)
        result = await compiled.ainvoke(module_state)

        if definition.state_out:
            output = definition.state_out(result, module_name)
        else:
            output = {}

        output["modules_invoked"] = state.get("modules_invoked", []) + [module_name]
        return output

    wrapper.__name__ = f"{module_name}_node"
    return wrapper
