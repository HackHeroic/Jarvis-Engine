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
