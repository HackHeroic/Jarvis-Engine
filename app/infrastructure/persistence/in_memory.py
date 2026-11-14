"""In-memory repository family: the ports over plain Python lists.

Used by tests and anywhere no database is configured. Every repository built over
the same ``InMemoryStore`` sees the same data, which is what lets draft
acceptance write tasks. Values are copied in and out, so a caller mutating a
returned entity cannot change what is stored — just as with a database.
"""

# Postponed annotations: InMemorySuggestionRepository defines a method named ``list``.
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from app.domain import errors
from app.domain.entities import (
    BehavioralConstraint,
    ChatMessage,
    ConsentDecision,
    ConversationSession,
    DraftSchedule,
    DraftStatus,
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
from app.domain.ports import (
    RECENT_SESSIONS_LIMIT,
    TASK_LIST_LIMIT,
    ConstraintRepository,
    ConversationRepository,
    DraftRepository,
    MemoryMatch,
    MemoryRepository,
    ProfileRepository,
    SuggestionRepository,
    TaskEventRepository,
    TaskRepository,
    UsageRepository,
)
from app.infrastructure.persistence._mapping import (
    OPEN_DRAFT_STATUSES,
    EMBEDDING,
    accepted_task_rows,
    apply_memory_update,
    apply_profile_update,
    check_limit,
    checked_summary,
    day_bounds,
    draft_closed,
    embedding_values,
    new_draft,
    normalized,
    not_found,
    now_utc,
    memory_changed,
    memory_id_taken,
    pattern_key_taken,
    prepare_new,
    prepare_new_memory,
    suggestion_changed,
    summarize_usage,
    task_from_row,
    task_id_clash,
    utc,
)
from app.utils.embedding import cosine_similarity


@dataclass
class InMemoryStore:
    """The shared tables the in-memory repositories read and write."""

    tasks: list[Task] = field(default_factory=list)
    task_events: list[TaskEvent] = field(default_factory=list)
    drafts: list[DraftSchedule] = field(default_factory=list)
    constraints: list[BehavioralConstraint] = field(default_factory=list)
    profiles: list[UserProfile] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    usage: list[LLMUsageEvent] = field(default_factory=list)
    memories: list[Memory] = field(default_factory=list)
    # Conversations, as the chat endpoint writes them (no port writes these two).
    chat_sessions: list[ConversationSession] = field(default_factory=list)
    chat_messages: list[ChatMessage] = field(default_factory=list)
    # Each memory's embedding, keyed by (user_id, memory id); held apart, as no read returns it.
    memory_embeddings: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    # Serialises draft status changes, as the row lock does in the database.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _value_of(status: TaskStatus | None) -> str | None:
    return TaskStatus(status).value if status is not None else None


class InMemoryTaskRepository(TaskRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def _indexes(self, user_id: str, task_id: str) -> list[int]:
        return [
            i
            for i, t in enumerate(self._store.tasks)
            if t.user_id == user_id and t.task_id == task_id
        ]

    async def list_pending(self, user_id: str) -> list[Task]:
        return [
            t.model_copy(deep=True)
            for t in self._store.tasks
            if t.user_id == user_id and t.status == TaskStatus.PENDING
        ]

    async def list_by_status(self, user_id: str, statuses: frozenset[TaskStatus]) -> list[Task]:
        wanted = {TaskStatus(status) for status in statuses}
        newest_first = [
            t for t in reversed(self._store.tasks) if t.user_id == user_id and t.status in wanted
        ]
        return [t.model_copy(deep=True) for t in newest_first[:TASK_LIST_LIMIT]]

    async def get(self, user_id: str, task_id: str) -> Task | None:
        indexes = self._indexes(user_id, task_id)
        return self._store.tasks[indexes[0]].model_copy(deep=True) if indexes else None

    async def list_scheduled_between(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[Task]:
        start, end = utc(start), utc(end)
        found = [
            t
            for t in self._store.tasks
            if t.user_id == user_id
            and t.scheduled_start is not None
            and start <= t.scheduled_start < end
        ]
        return [t.model_copy(deep=True) for t in sorted(found, key=lambda t: t.scheduled_start)]

    async def update_status(
        self,
        user_id: str,
        task_id: str,
        status: TaskStatus,
        *,
        expected: TaskStatus | None = None,
        expected_start: datetime | None = None,
    ) -> Task:
        indexes = self._indexes(user_id, task_id)
        if not indexes:
            raise not_found("task", task_id)
        if expected is not None or expected_start is not None:
            start = utc(expected_start) if expected_start is not None else None
            indexes = [
                i
                for i in indexes
                if (expected is None or self._store.tasks[i].status == expected)
                and (start is None or self._store.tasks[i].scheduled_start == start)
            ]
            if not indexes:
                raise errors.StaleVersionError(
                    "That task changed before this update; reload and try again.",
                    details={"task_id": task_id, "expected": _value_of(expected)},
                )
        for i in indexes:
            self._store.tasks[i] = self._store.tasks[i].model_copy(
                update={"status": TaskStatus(status)}
            )
        return self._store.tasks[indexes[0]].model_copy(deep=True)


class InMemoryTaskEventRepository(TaskEventRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def append(self, user_id: str, event: TaskEvent) -> TaskEvent:
        stored = prepare_new(user_id, event, "task event")
        self._store.task_events.append(stored)
        return stored.model_copy(deep=True)

    async def append_many(self, user_id: str, events: list[TaskEvent]) -> list[TaskEvent]:
        # Every event is checked before any is stored: all or nothing.
        stored = [prepare_new(user_id, event, "task event") for event in events]
        self._store.task_events.extend(stored)
        return [event.model_copy(deep=True) for event in stored]

    async def list_for_user(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
        types: set[TaskEventType] | None = None,
        task_ids: set[str] | None = None,
    ) -> list[TaskEvent]:
        since = utc(since) if since is not None else None
        found = [
            e
            for e in self._store.task_events
            if e.user_id == user_id
            and (since is None or e.occurred_at >= since)
            and (types is None or e.event_type in types)
            and (task_ids is None or e.task_id in task_ids)
        ]
        return [e.model_copy(deep=True) for e in sorted(found, key=lambda e: e.occurred_at)]


class InMemoryDraftRepository(DraftRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def _index(self, user_id: str, draft_id: str) -> int | None:
        return next(
            (
                i
                for i, d in enumerate(self._store.drafts)
                if d.id == draft_id and d.user_id == user_id
            ),
            None,
        )

    async def create(
        self,
        user_id: str,
        *,
        tasks: list[dict],
        horizon_start: str | None,
        goal_id: str | None,
        schedule: dict | None,
    ) -> DraftSchedule:
        draft = new_draft(
            user_id, tasks=tasks, horizon_start=horizon_start, goal_id=goal_id, schedule=schedule
        )
        self._store.drafts.append(draft)
        return draft.model_copy(deep=True)

    async def get(self, user_id: str, draft_id: str) -> DraftSchedule | None:
        i = self._index(user_id, draft_id)
        return self._store.drafts[i].model_copy(deep=True) if i is not None else None

    async def get_open(self, user_id: str) -> DraftSchedule | None:
        # The store is in creation order, so the last open draft is the newest.
        return next(
            (
                d.model_copy(deep=True)
                for d in reversed(self._store.drafts)
                if d.user_id == user_id and d.status in OPEN_DRAFT_STATUSES
            ),
            None,
        )

    async def replace_tasks(self, user_id: str, draft_id: str, tasks: list[dict]) -> DraftSchedule:
        # Under the lock: an edit must not land in the middle of an accept.
        async with self._store.lock:
            i = self._index(user_id, draft_id)
            if i is None:
                raise not_found("draft", draft_id)
            if self._store.drafts[i].status not in OPEN_DRAFT_STATUSES:
                raise draft_closed(draft_id, self._store.drafts[i].status)
            # model_copy assigns update values by reference: copy, or the caller keeps a handle.
            self._store.drafts[i] = self._store.drafts[i].model_copy(
                update={"tasks": copy.deepcopy(tasks)}
            )
            return self._store.drafts[i].model_copy(deep=True)

    async def set_status(
        self, user_id: str, draft_id: str, to: DraftStatus, *, reason: str | None = None
    ) -> DraftSchedule:
        async with self._store.lock:
            i = self._index(user_id, draft_id)
            if i is None:
                raise not_found("draft", draft_id)
            moved = self._store.drafts[i].transition(DraftStatus(to))
            if reason is not None:
                moved = moved.model_copy(update={"rejection_reason": reason})
            self._store.drafts[i] = moved
            return moved.model_copy(deep=True)

    async def accept_atomic(self, user_id: str, draft_id: str, rows: list[dict]) -> int:
        rows = accepted_task_rows(user_id, rows)
        async with self._store.lock:
            i = self._index(user_id, draft_id)
            if i is None:
                raise not_found("draft", draft_id)
            draft = self._store.drafts[i]
            if draft.status not in OPEN_DRAFT_STATUSES or (
                draft.expires_at is not None and draft.expires_at < now_utc()
            ):
                raise errors.InvalidStateTransition(
                    "This draft is no longer open, so it cannot be accepted.",
                    details={"draft_id": draft_id, "from": draft.status.value, "to": "accepted"},
                )
            # A database answers the status check and the writes in separate round
            # trips, with other requests running in between. Yield here as it would,
            # so it is the lock (not the lack of an await) that keeps a concurrent
            # accept out.
            await asyncio.sleep(0)
            new_tasks = [task_from_row(row) for row in rows]
            new_tasks = [
                t if t.id is not None else t.model_copy(update={"id": str(uuid4())})
                for t in new_tasks
            ]
            # As the accept_draft SQL function does: the plan replaces the user's
            # pending tasks, plus their missed tasks it re-slots under the same task_id.
            reslotted = {task.task_id for task in new_tasks}
            remaining = [
                t
                for t in self._store.tasks
                if not (
                    t.user_id == user_id
                    and (
                        t.status == TaskStatus.PENDING
                        or (t.status == TaskStatus.MISSED and t.task_id in reslotted)
                    )
                )
            ]
            # user_tasks is UNIQUE (user_id, task_id); checked before any write.
            taken = {t.task_id for t in remaining if t.user_id == user_id}
            for task in new_tasks:
                if task.task_id in taken:
                    raise task_id_clash(draft_id)
                taken.add(task.task_id)
            self._store.tasks[:] = remaining
            self._store.tasks.extend(new_tasks)
            # Re-read rather than write back the copy from before the yield: only
            # the status is this call's to change.
            i = self._index(user_id, draft_id)
            self._store.drafts[i] = self._store.drafts[i].model_copy(
                update={"status": DraftStatus.ACCEPTED}
            )
            # As the accept_draft SQL function does: the accepted plan replaces
            # every other plan the user had open.
            for j, other in enumerate(self._store.drafts):
                if other.user_id == user_id and j != i and other.status in OPEN_DRAFT_STATUSES:
                    self._store.drafts[j] = other.model_copy(
                        update={"status": DraftStatus.SUPERSEDED}
                    )
            return len(new_tasks)


class InMemoryConstraintRepository(ConstraintRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def list_active(self, user_id: str) -> list[BehavioralConstraint]:
        # An entity has no validity window, so every stored constraint applies.
        return [c.model_copy(deep=True) for c in self._store.constraints if c.user_id == user_id]

    async def add(self, user_id: str, c: BehavioralConstraint) -> BehavioralConstraint:
        stored = prepare_new(user_id, c, "constraint")
        if stored.created_at is None:  # the column default: stamped when stored
            stored = stored.model_copy(update={"created_at": now_utc()})
        self._store.constraints.append(stored)
        return stored.model_copy(deep=True)

    async def delete(self, user_id: str, constraint_id: str) -> bool:
        before = len(self._store.constraints)
        self._store.constraints[:] = [
            c
            for c in self._store.constraints
            if not (c.id == constraint_id and c.user_id == user_id)
        ]
        return len(self._store.constraints) < before

    async def set_semantics(
        self, user_id: str, constraint_id: str, structured_semantics: dict
    ) -> BehavioralConstraint | None:
        for i, c in enumerate(self._store.constraints):
            if c.id == constraint_id and c.user_id == user_id:
                updated = c.model_copy(update={"structured_semantics": dict(structured_semantics)})
                self._store.constraints[i] = updated
                return updated.model_copy(deep=True)
        return None


class InMemoryProfileRepository(ProfileRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def _index(self, user_id: str) -> int:
        i = next((i for i, p in enumerate(self._store.profiles) if p.user_id == user_id), None)
        if i is None:
            self._store.profiles.append(UserProfile(user_id=user_id))
            i = len(self._store.profiles) - 1
        return i

    async def get_or_create(self, user_id: str) -> UserProfile:
        return self._store.profiles[self._index(user_id)].model_copy(deep=True)

    async def update(self, user_id: str, **fields: Any) -> UserProfile:
        i = self._index(user_id)
        self._store.profiles[i] = apply_profile_update(self._store.profiles[i], fields)
        return self._store.profiles[i].model_copy(deep=True)

    async def list_user_ids(self) -> list[str]:
        return [p.user_id for p in self._store.profiles]


class InMemorySuggestionRepository(SuggestionRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def _index(self, user_id: str, suggestion_id: str) -> int | None:
        return next(
            (
                i
                for i, s in enumerate(self._store.suggestions)
                if s.id == suggestion_id and s.user_id == user_id
            ),
            None,
        )

    async def add(self, user_id: str, s: Suggestion) -> Suggestion:
        stored = prepare_new(user_id, s, "suggestion")
        self._store.suggestions.append(stored)
        return stored.model_copy(deep=True)

    async def list(
        self, user_id: str, *, status: SuggestionStatus | None = None, limit: int = 100
    ) -> list[Suggestion]:
        """Return the user's newest ``limit`` suggestions, newest first."""
        check_limit(limit)
        found = [
            s
            for s in self._store.suggestions
            if s.user_id == user_id and (status is None or s.status == status)
        ]
        return [
            s.model_copy(deep=True)
            for s in sorted(found, key=lambda s: s.created_at, reverse=True)[:limit]
        ]

    async def dedupe_keys(
        self, user_id: str, *, status: SuggestionStatus, limit: int = 500
    ) -> list[str]:
        newest = await self.list(user_id, status=status, limit=limit)
        keys = (s.action.get("dedupe_key") for s in newest)
        return [key for key in keys if isinstance(key, str)]

    async def get(self, user_id: str, suggestion_id: str) -> Suggestion | None:
        i = self._index(user_id, suggestion_id)
        return self._store.suggestions[i].model_copy(deep=True) if i is not None else None

    async def set_status(
        self,
        user_id: str,
        suggestion_id: str,
        to: SuggestionStatus,
        *,
        expected: SuggestionStatus | None = None,
        consent: ConsentDecision | None = None,
    ) -> Suggestion:
        # No await between the check and the write: the compare-and-set is atomic.
        i = self._index(user_id, suggestion_id)
        if i is None:
            raise not_found("suggestion", suggestion_id)
        if expected is not None and self._store.suggestions[i].status != expected:
            raise suggestion_changed(suggestion_id, expected)
        update: dict = {"status": SuggestionStatus(to)}
        if consent is not None:
            update |= {
                "consent_behavior": consent.behavior,
                "consent_reason": consent.message,
                "consent_kind": consent.kind,
            }
        self._store.suggestions[i] = self._store.suggestions[i].model_copy(update=update)
        return self._store.suggestions[i].model_copy(deep=True)

    async def exists_today(
        self, user_id: str, trigger: str, day: date, *, tz: str = "UTC"
    ) -> bool:
        start, end = day_bounds(day, tz)
        return any(
            s.user_id == user_id
            and s.trigger == trigger
            and s.status is not SuggestionStatus.EXPIRED
            and start <= s.created_at < end
            for s in self._store.suggestions
        )


class InMemoryMemoryRepository(MemoryRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def _index(self, user_id: str, memory_id: str) -> int | None:
        return next(
            (
                i
                for i, m in enumerate(self._store.memories)
                if m.id == memory_id and m.user_id == user_id
            ),
            None,
        )

    def _key_taken(self, user_id: str, pattern_key: str | None, *, other_than: str | None) -> bool:
        """The unique (user_id, pattern_key) index, which ignores a NULL key."""
        return pattern_key is not None and any(
            m.user_id == user_id and m.pattern_key == pattern_key and m.id != other_than
            for m in self._store.memories
        )

    def _set_embedding(self, user_id: str, memory_id: str, values: list[float] | None) -> None:
        """Store already-checked ``embedding_values`` (None removes the embedding)."""
        key = (user_id, memory_id)
        if values is None:
            self._store.memory_embeddings.pop(key, None)
        else:
            self._store.memory_embeddings[key] = values

    async def add(
        self, user_id: str, m: Memory, *, embedding: list[float] | None = None
    ) -> Memory:
        stored = prepare_new_memory(user_id, m)
        values = embedding_values(embedding)
        # The primary key spans every user, and Postgres checks it before the
        # pattern-key index.
        if any(existing.id == stored.id for existing in self._store.memories):
            raise memory_id_taken(stored.id)
        if self._key_taken(user_id, stored.pattern_key, other_than=None):
            raise pattern_key_taken(stored.pattern_key)
        self._store.memories.append(stored)
        self._set_embedding(user_id, stored.id, values)
        return stored.model_copy(deep=True)

    async def get(self, user_id: str, memory_id: str) -> Memory | None:
        i = self._index(user_id, memory_id)
        return self._store.memories[i].model_copy(deep=True) if i is not None else None

    async def list(
        self,
        user_id: str,
        *,
        statuses: frozenset[MemoryStatus] = frozenset({MemoryStatus.ACTIVE}),
        types: frozenset[str] | None = None,
    ) -> list[Memory]:
        found = [
            m
            for m in self._store.memories
            if m.user_id == user_id
            and m.status in statuses
            and (types is None or m.memory_type in types)
            and m.superseded_by is None
        ]
        return [m.model_copy(deep=True) for m in sorted(found, key=lambda m: m.created_at)]

    async def find_by_pattern_key(self, user_id: str, pattern_key: str) -> Memory | None:
        return next(
            (
                m.model_copy(deep=True)
                for m in self._store.memories
                if m.user_id == user_id and m.pattern_key == pattern_key
            ),
            None,
        )

    async def find_by_pattern_keys(self, user_id: str, pattern_keys: list[str]) -> list[Memory]:
        keys = set(pattern_keys)
        return [
            m.model_copy(deep=True)
            for m in self._store.memories
            if m.user_id == user_id and m.pattern_key is not None and m.pattern_key in keys
        ]

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
        query = embedding_values(embedding)
        check_limit(limit)
        scored = []
        for m in self._store.memories:
            stored = self._store.memory_embeddings.get((user_id, m.id))
            if (
                m.user_id != user_id
                or stored is None
                or m.status not in statuses
                or (types is not None and m.memory_type not in types)
                or m.superseded_by is not None
            ):
                continue
            similarity = cosine_similarity(stored, query)
            if similarity >= min_similarity:
                scored.append(MemoryMatch(memory=m.model_copy(deep=True), similarity=similarity))
        scored.sort(key=lambda match: match.similarity, reverse=True)
        return scored[:limit]

    async def update(
        self,
        user_id: str,
        memory_id: str,
        *,
        only_if_status: frozenset[MemoryStatus] | None = None,
        **fields: Any,
    ) -> Memory:
        i = self._index(user_id, memory_id)
        if i is None:
            raise not_found("memory", memory_id)
        status = self._store.memories[i].status
        if only_if_status is not None and status not in only_if_status:
            raise memory_changed(memory_id, status)
        updated = apply_memory_update(self._store.memories[i], fields)
        values = embedding_values(fields.get(EMBEDDING))
        if "pattern_key" in fields and self._key_taken(
            user_id, updated.pattern_key, other_than=memory_id
        ):
            raise pattern_key_taken(updated.pattern_key)
        self._store.memories[i] = updated
        if EMBEDDING in fields:
            self._set_embedding(user_id, memory_id, values)
        return updated.model_copy(deep=True)


class InMemoryUsageRepository(UsageRepository):
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def record(self, event: LLMUsageEvent) -> None:
        self._store.usage.append(normalized(event))

    async def summary(self, user_id: str, *, since: datetime | None = None) -> dict:
        since = utc(since) if since is not None else None
        return summarize_usage(
            e
            for e in self._store.usage
            if e.user_id == user_id and (since is None or e.created_at >= since)
        )


class InMemoryConversationRepository(ConversationRepository):
    """Reads the store's sessions and messages; writes only the summary columns."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def _index(self, user_id: str, session_id: str) -> int | None:
        return next(
            (
                i
                for i, s in enumerate(self._store.chat_sessions)
                if s.id == session_id and s.user_id == user_id
            ),
            None,
        )

    async def get_session(self, user_id: str, session_id: str) -> ConversationSession | None:
        i = self._index(user_id, session_id)
        return normalized(self._store.chat_sessions[i]) if i is not None else None

    async def previous_session(
        self, user_id: str, before_session_id: str
    ) -> ConversationSession | None:
        others = [
            m
            for m in self._store.chat_messages
            if m.user_id == user_id and m.session_id != before_session_id
        ]
        if not others:
            return None
        newest = max(others, key=lambda m: (utc(m.created_at), m.id))
        return await self.get_session(user_id, newest.session_id)

    async def messages(
        self,
        user_id: str,
        session_id: str,
        *,
        after: datetime | None = None,
        limit: int = 50,
        oldest_first: bool = False,
    ) -> list[ChatMessage]:
        check_limit(limit)
        found = [
            m
            for m in self._store.chat_messages
            if m.user_id == user_id
            and m.session_id == session_id
            and (after is None or utc(m.created_at) > utc(after))
        ]
        found.sort(key=lambda m: (utc(m.created_at), m.id))
        chosen = found[:limit] if oldest_first else found[max(0, len(found) - limit) :]
        return [normalized(m) for m in chosen]

    async def set_summary(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        through: datetime,
        *,
        written_at: datetime | None = None,
    ) -> bool:
        summary, through = checked_summary(summary, through)
        # No await between the check and the write: the compare-and-set is atomic.
        i = self._index(user_id, session_id)
        if i is None:
            raise not_found("session", session_id)
        stored = self._store.chat_sessions[i].summary_through
        if stored is not None and utc(stored) > through:
            return False
        self._store.chat_sessions[i] = self._store.chat_sessions[i].model_copy(
            update={
                "summary": summary,
                "summary_through": through,
                "summary_updated_at": utc(written_at) if written_at else now_utc(),
            }
        )
        return True

    async def recent_sessions(
        self, user_id: str, *, since: datetime
    ) -> list[ConversationSession]:
        found = [
            s
            for s in self._store.chat_sessions
            if s.user_id == user_id and utc(s.updated_at) >= utc(since)
        ]
        found.sort(key=lambda s: (utc(s.updated_at), s.id), reverse=True)
        return [normalized(s) for s in found[:RECENT_SESSIONS_LIMIT]]
