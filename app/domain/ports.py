"""Abstract ports the domain depends on; infrastructure provides the implementations.

Every per-user repository method takes ``user_id`` first and only ever sees that user's
rows. The two exceptions are ``ProfileRepository.list_user_ids`` (enumerates users) and
``UsageRepository.record`` (the event carries its own, possibly absent, ``user_id``).
"""

# Postponed annotations: SuggestionRepository defines a method named ``list``, which
# would otherwise shadow the builtin in annotations evaluated later in that class body.
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities import (
    BehavioralConstraint,
    DraftSchedule,
    DraftStatus,
    FocusProfile,
    LLMLocation,
    LLMUsageEvent,
    Memory,
    MemoryStatus,
    Suggestion,
    SuggestionStatus,
    Task,
    TaskEvent,
    TaskEventType,
    TaskStatus,
    UserProfile,
)

# --- Repositories -----------------------------------------------------------


class TaskRepository(ABC):
    """Storage for the user's committed tasks."""

    @abstractmethod
    async def list_pending(self, user_id: str) -> list[Task]:
        """Return the user's tasks whose status is PENDING."""

    @abstractmethod
    async def get(self, user_id: str, task_id: str) -> Task | None:
        """Return the user's task with this task_id, or None."""

    @abstractmethod
    async def list_scheduled_between(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[Task]:
        """Return the user's tasks scheduled within [start, end)."""

    @abstractmethod
    async def update_status(
        self, user_id: str, task_id: str, status: TaskStatus, *, expected: TaskStatus | None = None
    ) -> Task:
        """Set the status if it is ``expected`` (when given); NotFoundError / StaleVersionError."""

    @abstractmethod
    async def reschedule(self, user_id: str, task_id: str, start: datetime, end: datetime) -> Task:
        """Move the task to a new [start, end) slot and return it; raise NotFoundError if absent."""


class TaskEventRepository(ABC):
    """Append-only log of what happened to the user's tasks."""

    @abstractmethod
    async def append(self, user_id: str, event: TaskEvent) -> TaskEvent:
        """Store the event and return it as persisted (with its id)."""

    @abstractmethod
    async def append_many(self, user_id: str, events: list[TaskEvent]) -> list[TaskEvent]:
        """Store all ``events`` in one write, or none of them; return them as persisted.

        Raises ValidationError, before anything is written, if any event is not the
        user's. An empty list writes nothing and returns [].
        """

    @abstractmethod
    async def list_for_user(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
        types: set[TaskEventType] | None = None,
        task_ids: set[str] | None = None,
    ) -> list[TaskEvent]:
        """Return the user's events oldest first, filtered by ``since``, ``types`` and ``task_ids``.

        Each filter applies only when given; an empty ``task_ids`` matches nothing.

        A store may cap how many rows one read returns: Supabase's PostgREST answers
        at most ``max_rows`` (1000, ``supabase/config.toml``) and says nothing. The
        read is therefore made newest first and handed back oldest first, so whatever
        a cap leaves out is the OLDEST matching events, never the newest. A caller
        that needs every matching event should narrow the read (``task_ids``,
        ``since``) to stay under the cap.
        """


class DraftRepository(ABC):
    """Storage for draft schedules under review."""

    @abstractmethod
    async def create(
        self,
        user_id: str,
        *,
        tasks: list[dict],
        horizon_start: str | None,
        goal_id: str | None,
        schedule: dict | None,
    ) -> DraftSchedule:
        """Store a new PENDING draft and return it."""

    @abstractmethod
    async def get(self, user_id: str, draft_id: str) -> DraftSchedule | None:
        """Return the user's draft with this id, or None."""

    @abstractmethod
    async def get_open(self, user_id: str) -> DraftSchedule | None:
        """Return the user's newest PENDING or MODIFIED draft, or None."""

    @abstractmethod
    async def replace_tasks(self, user_id: str, draft_id: str, tasks: list[dict]) -> DraftSchedule:
        """Replace an open draft's tasks and return the updated draft.

        Raises NotFoundError if the user has no such draft, and InvalidStateTransition
        if it is no longer open (accepted, rejected, expired or superseded).
        """

    @abstractmethod
    async def set_status(
        self, user_id: str, draft_id: str, to: DraftStatus, *, reason: str | None = None
    ) -> DraftSchedule:
        """Move the draft to ``to`` if DRAFT_TRANSITIONS allows it; else InvalidStateTransition."""

    @abstractmethod
    async def accept_atomic(self, user_id: str, draft_id: str, rows: list[dict]) -> int:
        """Atomically write ``rows`` and mark the draft ACCEPTED; return rows written.

        ``rows`` replace the user's PENDING tasks, plus their MISSED tasks whose
        task_id appears in ``rows`` (a passed slot re-slotted under its own id). Every
        other task stays. The user's other open drafts become SUPERSEDED in the same step.

        Raises NotFoundError if the user has no such draft, InvalidStateTransition if
        it is not open or has expired, ConflictError if a row's task_id is already held
        by one of the user's remaining tasks or repeats within ``rows``, and
        ValidationError for a row that is not the user's.
        """


class ConstraintRepository(ABC):
    """Storage for the user's behavioural constraints."""

    @abstractmethod
    async def list_active(self, user_id: str) -> list[BehavioralConstraint]:
        """Return the user's constraints that currently apply."""

    @abstractmethod
    async def add(self, user_id: str, c: BehavioralConstraint) -> BehavioralConstraint:
        """Store a constraint and return it as persisted."""

    @abstractmethod
    async def delete(self, user_id: str, constraint_id: str) -> bool:
        """Delete the user's constraint; return whether one was deleted."""


class ProfileRepository(ABC):
    """Storage for user profiles."""

    @abstractmethod
    async def get_or_create(self, user_id: str) -> UserProfile:
        """Return the user's profile, creating it with defaults if absent."""

    @abstractmethod
    async def update(self, user_id: str, **fields: Any) -> UserProfile:
        """Update the given profile fields and return the profile."""

    @abstractmethod
    async def list_user_ids(self) -> list[str]:
        """Return the ids of every user with a profile."""


class SuggestionRepository(ABC):
    """Storage for proactive suggestions."""

    @abstractmethod
    async def add(self, user_id: str, s: Suggestion) -> Suggestion:
        """Store a suggestion and return it as persisted (with its id)."""

    @abstractmethod
    async def list(
        self, user_id: str, *, status: SuggestionStatus | None = None, limit: int = 100
    ) -> list[Suggestion]:
        """Return the user's newest ``limit`` suggestions (only ``status``, if given), newest first.

        A store may cap one read below ``limit`` (PostgREST answers at most 1000 rows
        and says nothing); the read is newest first, so a cap drops only the oldest.
        A negative ``limit`` raises ValidationError.
        """

    @abstractmethod
    async def get(self, user_id: str, suggestion_id: str) -> Suggestion | None:
        """Return the user's suggestion with this id, or None."""

    @abstractmethod
    async def set_status(
        self,
        user_id: str,
        suggestion_id: str,
        to: SuggestionStatus,
        *,
        expected: SuggestionStatus | None = None,
    ) -> Suggestion:
        """Set the status if it is ``expected`` (when given) and return the suggestion.

        A compare-and-set: of two racing calls expecting the same status, one wins.
        Raises NotFoundError if the user has no such suggestion, and StaleVersionError
        if its status is no longer ``expected``.
        """

    @abstractmethod
    async def exists_today(
        self, user_id: str, trigger: str, day: date, *, tz: str = "UTC"
    ) -> bool:
        """Return whether ``trigger`` already made the user a suggestion on ``day``.

        ``day`` is a calendar day on the clock of ``tz`` (an IANA zone name), so
        00:30 in India is the new day there while still yesterday in UTC. An unknown
        zone raises ValidationError.
        """


class MemoryMatch(BaseModel):
    """A memory found by similarity search, with how close it is to the query."""

    memory: Memory = Field(description="The memory found.")
    similarity: float = Field(
        description="Cosine similarity to the query embedding, from -1 to 1 (1 is identical)."
    )


class MemoryRepository(ABC):
    """Storage for what Jarvis remembers about the user, searchable by meaning.

    Embeddings are written alongside a memory but never read back: no method
    returns one. An embedding (and the query of ``nearest``) must be 384 finite
    numbers, not all zero, or the call raises ValidationError.
    """

    @abstractmethod
    async def add(
        self, user_id: str, m: Memory, *, embedding: list[float] | None = None
    ) -> Memory:
        """Store the memory (and its embedding, if given) and return it with its id.

        Raises ValidationError if the memory belongs to another user, or its id or a
        reference (source_session_id, source_message_id, superseded_by) is not a UUID;
        ConflictError if its id is already in use, or the user already has a memory
        with its pattern_key.
        """

    @abstractmethod
    async def get(self, user_id: str, memory_id: str) -> Memory | None:
        """Return the user's memory with this id, whatever its status, or None."""

    @abstractmethod
    async def list(
        self,
        user_id: str,
        *,
        statuses: frozenset[MemoryStatus] = frozenset({MemoryStatus.ACTIVE}),
        types: frozenset[str] | None = None,
    ) -> list[Memory]:
        """Return the user's memories in ``statuses`` (and ``types``), oldest first.

        A superseded memory is never returned.
        """

    @abstractmethod
    async def find_by_pattern_key(self, user_id: str, pattern_key: str) -> Memory | None:
        """Return the user's memory with this pattern_key, whatever its status, or None."""

    @abstractmethod
    async def nearest(
        self,
        user_id: str,
        embedding: list[float],
        *,
        statuses: frozenset[MemoryStatus],
        types: frozenset[str] | None = None,
        limit: int = 5,
        min_similarity: float = 0.0,
    ) -> list[MemoryMatch]:
        """Return up to ``limit`` of the user's memories most similar to ``embedding``.

        Only memories in ``statuses`` (and ``types``) that have an embedding, are not
        superseded, and score at least ``min_similarity`` count; most similar first.
        A negative ``limit`` raises ValidationError.
        """

    @abstractmethod
    async def update(self, user_id: str, memory_id: str, **fields: Any) -> Memory:
        """Set the given fields and return the memory; ``embedding`` may be one of them.

        Raises NotFoundError if the user has no such memory, ValidationError for an
        unknown field (``id`` and ``user_id`` included), a value the memory rejects or
        a reference that is not a UUID, and ConflictError if a new pattern_key is
        already the user's.
        """


class UsageRepository(ABC):
    """Ledger of language-model calls."""

    @abstractmethod
    async def record(self, event: LLMUsageEvent) -> None:
        """Store one usage event."""

    @abstractmethod
    async def summary(self, user_id: str, *, since: datetime | None = None) -> dict:
        """Return local_calls, cloud_calls, input_tokens, output_tokens, pii_redactions totals."""


# --- Background work --------------------------------------------------------


class Sweep(ABC):
    """A periodic job over every user, owned by the subsystem whose data it maintains."""

    name: ClassVar[str]
    interval: ClassVar[timedelta]

    @abstractmethod
    async def run(self, user_ids: list[str], now: datetime) -> int:
        """Sweep each user as of ``now``; return items changed.

        One user's failure is logged, never raised: the other users are still swept.
        """


# --- Learning ---------------------------------------------------------------


class EnergyEstimator(ABC):
    """Estimates how well the user focuses at each hour of their day (Strategy)."""

    @abstractmethod
    async def estimate(self, user_id: str, zone: ZoneInfo, *, now: datetime) -> FocusProfile:
        """Return the user's focus profile as of ``now``, hours read on ``zone``'s clock."""


# --- Embeddings -------------------------------------------------------------


class TextEmbedder(ABC):
    """Turns text into vectors whose cosine similarity tracks meaning (Strategy)."""

    dimensions: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one ``dimensions``-long vector per text, in order.

        Synchronous CPU work: async callers run it with ``asyncio.to_thread``.
        """


# --- Language models --------------------------------------------------------


class LLMRequest(BaseModel):
    """One request to a language model, independent of which provider serves it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: str = Field(description="Routing task name (e.g. socratic_chunker).")
    role: Literal["primary", "fast", "cloud"] = Field(description="Model role the task maps to.")
    prompt: str = Field(description="The user-turn prompt.")
    system_prompt: str = Field(default="", description="System instructions for the model.")
    conversation_history: list[dict] | None = Field(
        default=None, description="Prior chat messages ({role, content}) to send before the prompt."
    )
    response_schema: type[BaseModel] | None = Field(
        default=None,
        description="Pydantic model class the answer must parse into; None for free text.",
    )
    timeout_s: float = Field(default=60.0, description="Seconds before the call is abandoned.")
    user_id: str | None = Field(
        default=None, description="User the call is made for, for usage accounting."
    )
    session_id: str | None = Field(
        default=None, description="Chat session the call belongs to, for usage accounting."
    )


class LLMProvider(ABC):
    """A source of language-model completions (Strategy)."""

    name: str
    location: LLMLocation

    @abstractmethod
    def is_available(self) -> bool:
        """Return cheaply, without I/O, whether this provider can be tried right now."""

    @abstractmethod
    async def complete(self, req: LLMRequest) -> str | BaseModel:
        """Return the completion: an instance of req.response_schema when set, else text."""

    @abstractmethod
    def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        """Yield (event_type, token) pairs as they arrive; req.response_schema must be None."""


class RetryPolicy(ABC):
    """Decides whether and when a failed call is retried (Strategy)."""

    @abstractmethod
    def should_retry(self, error: Exception, attempt: int) -> bool:
        """Return whether to try again after ``error`` on attempt number ``attempt``."""

    @abstractmethod
    def delay(self, attempt: int) -> float:
        """Return seconds to wait before retry number ``attempt``."""
