"""
Generic registry framework for Jarvis.

All extensible subsystems (intents, document types, memory types,
PEARL patterns) inherit from BaseRegistry. Adding a new capability
to ANY subsystem = defining a handler + registering it.

Inspired by Django's app registry and FastAPI's dependency injection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class RegistryEntry(Generic[T]):
    """A single entry in any registry."""

    name: str
    description: str
    handler: Callable[..., Any]  # Accepts both sync and async handlers
    examples: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseRegistry(Generic[T]):
    """
    Generic registry. Provides:
    - Registration with validation
    - LLM classification prompt generation (auto-discovers registered types)
    - Handler lookup with fallback
    - Introspection
    """

    def __init__(self, name: str, fallback_key: str | None = None):
        self._name = name
        self._entries: dict[str, RegistryEntry[T]] = {}
        self._fallback_key = fallback_key

    def register(self, entry: RegistryEntry[T]) -> None:
        """Register a new entry. Re-registering overwrites."""
        if not entry.name or not entry.handler:
            raise ValueError("Registry entry must have name and handler")
        self._entries[entry.name] = entry

    def get(self, name: str) -> RegistryEntry[T] | None:
        """Look up an entry by name."""
        return self._entries.get(name)

    def get_or_fallback(self, name: str) -> RegistryEntry[T]:
        """Look up entry, fall back to default if not found."""
        entry = self._entries.get(name)
        if entry:
            return entry
        if self._fallback_key and self._fallback_key in self._entries:
            return self._entries[self._fallback_key]
        raise KeyError(
            f"No entry '{name}' in {self._name} registry and no fallback"
        )

    def classification_prompt(self) -> str:
        """Generate a classification prompt from all registered entries."""
        lines = [f"Classify into one of these {self._name} types:\n"]
        for name, entry in self._entries.items():
            examples = ", ".join(entry.examples[:3]) if entry.examples else "N/A"
            lines.append(f"- {name}: {entry.description} (e.g., {examples})")
        if self._fallback_key:
            lines.append(f"\nIf none match clearly, use: {self._fallback_key}")
        return "\n".join(lines)

    def all_entries(self) -> dict[str, RegistryEntry[T]]:
        """List all registered entries."""
        return dict(self._entries)

    def registered_names(self) -> list[str]:
        """List all registered type names."""
        return list(self._entries.keys())
