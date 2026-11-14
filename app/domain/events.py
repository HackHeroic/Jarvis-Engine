"""Domain events and the in-process event bus (Observer).

Emitters publish what happened; listeners (learning, telemetry, anticipation)
react without the emitter knowing them. A failing listener is logged and
never breaks the emitter or the other listeners.
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.entities import LLMUsageEvent, Pattern

logger = logging.getLogger(__name__)


class DomainEvent(BaseModel):
    """Base of every domain event."""

    occurred_at: datetime = Field(description="When the event happened.")
    user_id: str | None = Field(default=None, description="User the event concerns, if any.")


class TaskCompleted(DomainEvent):
    """The user finished a task."""

    task_id: str = Field(description="Task that was completed.")


class TaskSkipped(DomainEvent):
    """The user chose to skip a task."""

    task_id: str = Field(description="Task that was skipped.")
    reason: str | None = Field(default=None, description="Why the user skipped it, if they said.")


class TaskMissed(DomainEvent):
    """A task's slot passed without it being done or skipped."""

    task_id: str = Field(description="Task that was missed.")


class DraftAccepted(DomainEvent):
    """The user accepted a draft schedule and its tasks were persisted."""

    draft_id: str = Field(description="Draft that was accepted.")
    task_count: int = Field(description="Number of tasks written by the accept.")


class PatternDetected(DomainEvent):
    """A detector found a recurring behaviour."""

    pattern: Pattern = Field(description="The pattern that was found.")


class LLMCallCompleted(DomainEvent):
    """A language-model call finished."""

    usage: LLMUsageEvent = Field(description="Usage record for the call.")


class EventListener(ABC):
    """Something that reacts to domain events."""

    @abstractmethod
    async def handle(self, event: DomainEvent) -> None:
        """React to one published event."""


class EventBus:
    """Observer subject: delivers each event to the listeners of its type and of its bases.

    A subscription to a base class (``DomainEvent`` itself, say) receives every event
    of every subclass. Listeners of the most specific type run first.
    """

    def __init__(self) -> None:
        self._listeners: dict[type[DomainEvent], list[EventListener]] = {}

    def subscribe(self, event_type: type[DomainEvent], listener: EventListener) -> None:
        """Deliver every future event of ``event_type``, or of a subclass, to ``listener``."""
        self._listeners.setdefault(event_type, []).append(listener)

    def _listeners_for(self, event: DomainEvent) -> list[EventListener]:
        """Listeners of each class in the event's MRO, most specific first, each once."""
        found: list[EventListener] = []
        for cls in type(event).__mro__:
            for listener in self._listeners.get(cls, ()):
                if not any(listener is seen for seen in found):
                    found.append(listener)
        return found

    async def publish(self, event: DomainEvent) -> None:
        """Hand ``event`` to each listener in turn; a listener's failure is logged, never raised."""
        for listener in self._listeners_for(event):
            try:
                await listener.handle(event)
            except Exception:
                logger.exception(
                    "Event listener %s failed on %s", type(listener).__name__, type(event).__name__
                )


class NullEventBus(EventBus):
    """An event bus that drops every event — for callers and tests with nobody listening."""

    async def publish(self, event: DomainEvent) -> None:
        """Do nothing."""
