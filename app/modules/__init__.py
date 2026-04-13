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
