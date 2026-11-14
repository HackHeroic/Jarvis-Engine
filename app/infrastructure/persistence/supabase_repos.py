"""Supabase repository family: the ports over supabase-py (an Adapter).

supabase-py is synchronous, so every call runs on a worker thread via
``asyncio.to_thread``. Any failure in transport surfaces as PersistenceError with
the original exception chained; its message never repeats the driver's text.
Every query filters by ``user_id``, except the two methods ``ports.py`` documents.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.domain import errors
from app.domain.entities import (
    BehavioralConstraint,
    ConsentDecision,
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
    TASK_LIST_LIMIT,
    ConstraintRepository,
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
    EMBEDDING,
    MEMORY_COLUMNS,
    OPEN_DRAFT_STATUSES,
    accepted_task_rows,
    apply_memory_update,
    apply_profile_update,
    check_limit,
    constraint_to_row,
    day_bounds,
    draft_closed,
    embedding_values,
    from_row,
    is_uuid,
    iso,
    memory_from_row,
    memory_changed,
    memory_id_taken,
    memory_to_row,
    new_draft,
    not_found,
    now_utc,
    pattern_key_taken,
    prepare_new,
    prepare_new_memory,
    suggestion_changed,
    USAGE_SUMMARY_COLUMNS,
    summarize_usage_rows,
    task_from_row,
    task_id_clash,
    task_status_values,
    to_row,
    utc,
)

_UNREACHABLE = "Could not reach the database."
# The name 20261114000001_capstone_lld.sql gives user_tasks' UNIQUE (user_id, task_id).
_TASK_ID_KEY = "user_tasks_user_id_task_id_key"
_UNREADABLE = "Stored data is not readable."
# The name 20261115000002_memory_lifecycle_and_vectors.sql gives the unique
# (user_id, pattern_key) index on user_memories, and user_memories' primary key.
_PATTERN_KEY_INDEX = "idx_user_memories_user_pattern_key"
_MEMORY_PRIMARY_KEY = "user_memories_pkey"
_MEMORY_SELECT = ", ".join(MEMORY_COLUMNS)
# A lost compare-and-set on a draft's status is retried against the status that won.
_STATUS_CAS_ATTEMPTS = 3

# One page of a usage summary. It must not exceed PostgREST's max_rows (1000,
# supabase/config.toml): a page cut by that cap would read as the last one.
USAGE_PAGE_ROWS = 1000
# Pages one summary may read (20,000 calls); past it the summary says it is truncated.
USAGE_MAX_PAGES = 20

M = TypeVar("M", bound=BaseModel)


def _read(model: type[M], row: dict) -> M:
    """Build ``model`` from a stored row.

    A row the entity rejects (legacy data) is a storage fault, not a request fault.
    """
    try:
        return from_row(model, row)
    except PydanticValidationError as exc:
        raise errors.PersistenceError(_UNREADABLE) from exc


def _read_task(row: dict) -> Task:
    try:
        return task_from_row(row)
    except PydanticValidationError as exc:
        raise errors.PersistenceError(_UNREADABLE) from exc


def _read_memory(row: dict) -> Memory:
    try:
        return memory_from_row(row)
    except PydanticValidationError as exc:
        raise errors.PersistenceError(_UNREADABLE) from exc


class _SupabaseRepository:
    """Shared plumbing: one table, calls off the event loop, failures translated."""

    table: str

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _run(self, build: Callable[[Any], Any]) -> list[dict]:
        """Execute ``build(client.table(self.table))`` on a worker thread; return its rows."""
        try:
            result = await asyncio.to_thread(
                lambda: build(self._client.table(self.table)).execute()
            )
        except Exception as exc:  # any transport or PostgREST failure
            raise errors.PersistenceError(_UNREACHABLE) from exc
        return result.data or []


class SupabaseTaskRepository(_SupabaseRepository, TaskRepository):
    table = "user_tasks"

    async def list_pending(self, user_id: str) -> list[Task]:
        rows = await self._run(
            lambda q: q.select("*")
            .eq("user_id", user_id)
            .eq("status", TaskStatus.PENDING.value)
            .order("created_at")
        )
        return [_read_task(r) for r in rows]

    async def list_by_status(self, user_id: str, statuses: frozenset[TaskStatus]) -> list[Task]:
        values = sorted({v for status in statuses for v in task_status_values(TaskStatus(status))})
        if not values:
            return []
        rows = await self._run(
            lambda q: q.select("*")
            .eq("user_id", user_id)
            .in_("status", values)
            # Newest first: whatever the bound leaves out is the oldest.
            .order("created_at", desc=True)
            .limit(TASK_LIST_LIMIT)
        )
        return [_read_task(r) for r in rows]

    async def get(self, user_id: str, task_id: str) -> Task | None:
        rows = await self._run(
            lambda q: q.select("*").eq("user_id", user_id).eq("task_id", task_id).limit(1)
        )
        return _read_task(rows[0]) if rows else None

    async def list_scheduled_between(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[Task]:
        rows = await self._run(
            lambda q: q.select("*")
            .eq("user_id", user_id)
            .gte("scheduled_start", iso(start))
            .lt("scheduled_start", iso(end))
            .order("scheduled_start")
        )
        return [_read_task(r) for r in rows]

    async def update_status(
        self, user_id: str, task_id: str, status: TaskStatus, *, expected: TaskStatus | None = None
    ) -> Task:
        def build(q: Any) -> Any:
            q = q.update({"status": TaskStatus(status).value}).eq("user_id", user_id).eq(
                "task_id", task_id
            )
            if expected is not None:  # compare-and-set: only rows still in ``expected``
                q = q.in_("status", task_status_values(TaskStatus(expected)))
            return q

        rows = await self._run(build)
        if rows:
            return _read_task(rows[0])
        if expected is None or await self.get(user_id, task_id) is None:
            raise not_found("task", task_id)
        raise errors.StaleVersionError(
            "That task changed before this update; reload and try again.",
            details={"task_id": task_id, "expected": TaskStatus(expected).value},
        )

    async def reschedule(self, user_id: str, task_id: str, start: datetime, end: datetime) -> Task:
        if utc(end) <= utc(start):
            raise errors.ValidationError("A task must end after it starts.")
        rows = await self._run(
            lambda q: q.update({"scheduled_start": iso(start), "scheduled_end": iso(end)})
            .eq("user_id", user_id)
            .eq("task_id", task_id)
        )
        if not rows:
            raise not_found("task", task_id)
        return _read_task(rows[0])


class SupabaseTaskEventRepository(_SupabaseRepository, TaskEventRepository):
    table = "task_events"

    async def append(self, user_id: str, event: TaskEvent) -> TaskEvent:
        stored = prepare_new(user_id, event, "task event")
        rows = await self._run(lambda q: q.insert(to_row(stored)))
        return _read(TaskEvent, rows[0]) if rows else stored

    async def append_many(self, user_id: str, events: list[TaskEvent]) -> list[TaskEvent]:
        # Every event is checked before the one insert: all or nothing.
        stored = [prepare_new(user_id, event, "task event") for event in events]
        if not stored:
            return []
        rows = await self._run(lambda q: q.insert([to_row(event) for event in stored]))
        # The ids are minted here, so ``stored`` is already what was written; the echoed
        # rows are preferred only when the response carries all of them.
        return [_read(TaskEvent, r) for r in rows] if len(rows) == len(stored) else stored

    async def list_for_user(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
        types: set[TaskEventType] | None = None,
        task_ids: set[str] | None = None,
    ) -> list[TaskEvent]:
        if task_ids is not None and not task_ids:
            return []

        def build(q: Any) -> Any:
            q = q.select("*").eq("user_id", user_id)
            if since is not None:
                q = q.gte("occurred_at", iso(since))
            if types is not None:
                q = q.in_("event_type", sorted(TaskEventType(t).value for t in types))
            if task_ids is not None:
                q = q.in_("task_id", sorted(task_ids))
            # Newest first: if PostgREST's max_rows cap cuts the answer short, the
            # rows it leaves out are the oldest. Handed back oldest first, as the port says.
            return q.order("occurred_at", desc=True)

        return [_read(TaskEvent, r) for r in reversed(await self._run(build))]


class SupabaseDraftRepository(_SupabaseRepository, DraftRepository):
    table = "draft_schedules"

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
        rows = await self._run(lambda q: q.insert(to_row(draft)))
        return _read(DraftSchedule, rows[0]) if rows else draft

    async def get(self, user_id: str, draft_id: str) -> DraftSchedule | None:
        if not is_uuid(draft_id):  # the id column is a uuid; anything else matches nothing
            return None
        rows = await self._run(
            lambda q: q.select("*").eq("id", draft_id).eq("user_id", user_id).limit(1)
        )
        return _read(DraftSchedule, rows[0]) if rows else None

    async def get_open(self, user_id: str) -> DraftSchedule | None:
        rows = await self._run(
            lambda q: q.select("*")
            .eq("user_id", user_id)
            .in_("status", [s.value for s in OPEN_DRAFT_STATUSES])
            .order("created_at", desc=True)
            .limit(1)
        )
        return _read(DraftSchedule, rows[0]) if rows else None

    async def replace_tasks(self, user_id: str, draft_id: str, tasks: list[dict]) -> DraftSchedule:
        if not is_uuid(draft_id):
            raise not_found("draft", draft_id)
        # Only an open draft takes the edit; the status filter makes the check and
        # the write one statement, so an accept that lands first closes the door.
        rows = await self._run(
            lambda q: q.update({"tasks": tasks})
            .eq("id", draft_id)
            .eq("user_id", user_id)
            .in_("status", [s.value for s in OPEN_DRAFT_STATUSES])
        )
        if rows:
            return _read(DraftSchedule, rows[0])
        current = await self.get(user_id, draft_id)
        if current is None:
            raise not_found("draft", draft_id)
        raise draft_closed(draft_id, current.status)

    async def set_status(
        self, user_id: str, draft_id: str, to: DraftStatus, *, reason: str | None = None
    ) -> DraftSchedule:
        to = DraftStatus(to)
        payload: dict[str, Any] = {"status": to.value}
        if reason is not None:
            payload["rejection_reason"] = reason
        for _ in range(_STATUS_CAS_ATTEMPTS):
            current = await self.get(user_id, draft_id)
            if current is None:
                raise not_found("draft", draft_id)
            current.transition(to)  # raises InvalidStateTransition if the move is illegal
            # Compare-and-set on the status just checked, so a concurrent move is never
            # overwritten; if one got in first, check the move again from where it left.
            rows = await self._run(
                lambda q, seen=current.status.value: q.update(payload)
                .eq("id", draft_id)
                .eq("user_id", user_id)
                .eq("status", seen)
            )
            if rows:
                return _read(DraftSchedule, rows[0])
        raise errors.InvalidStateTransition(
            "This draft kept changing while it was being updated; reload and try again.",
            details={"draft_id": draft_id, "to": to.value},
        )

    async def accept_atomic(self, user_id: str, draft_id: str, rows: list[dict]) -> int:
        """One call to the ``accept_draft`` database function, which runs as one transaction."""
        rows = accepted_task_rows(user_id, rows)
        if not is_uuid(draft_id):
            raise not_found("draft", draft_id)
        params = {"p_draft_id": draft_id, "p_user_id": user_id, "p_rows": rows}
        try:
            result = await asyncio.to_thread(
                lambda: self._client.rpc("accept_draft", params).execute()
            )
        except Exception as exc:
            text = _error_text(exc)
            if "DRAFT_NOT_OPEN" in text:
                raise errors.InvalidStateTransition(
                    "This draft is no longer open, so it cannot be accepted.",
                    details={"draft_id": draft_id, "to": "accepted"},
                ) from exc
            if "DRAFT_NOT_FOUND" in text:
                raise not_found("draft", draft_id) from exc
            # 23505 is unique_violation; the only unique key the insert can hit.
            if _TASK_ID_KEY in text or getattr(exc, "code", None) == "23505":
                raise task_id_clash(draft_id) from exc
            raise errors.PersistenceError(_UNREACHABLE) from exc
        try:
            return int(result.data)
        except (TypeError, ValueError) as exc:
            # The database answered, with something that is not a row count.
            raise errors.PersistenceError(_UNREADABLE) from exc


def _error_text(exc: Exception) -> str:
    """The text a database error carries: postgrest's APIError keeps it in attributes."""
    parts = [str(exc), *(str(getattr(exc, name, "") or "") for name in ("message", "details"))]
    return " ".join(parts)


class SupabaseConstraintRepository(_SupabaseRepository, ConstraintRepository):
    table = "behavioral_constraints"

    async def list_active(self, user_id: str) -> list[BehavioralConstraint]:
        rows = await self._run(
            lambda q: q.select("*").eq("user_id", user_id).order("created_at")
        )
        now = now_utc()
        return [_read(BehavioralConstraint, r) for r in rows if _applies_at(r, now)]

    async def add(self, user_id: str, c: BehavioralConstraint) -> BehavioralConstraint:
        stored = prepare_new(user_id, c, "constraint")
        rows = await self._run(lambda q: q.insert(constraint_to_row(stored)))
        return _read(BehavioralConstraint, rows[0]) if rows else stored

    async def delete(self, user_id: str, constraint_id: str) -> bool:
        if not is_uuid(constraint_id):
            return False
        rows = await self._run(
            lambda q: q.delete().eq("id", constraint_id).eq("user_id", user_id)
        )
        return bool(rows)


def _applies_at(row: dict, now: datetime) -> bool:
    """Whether a behavioral_constraints row's validity window (both ends optional) holds now."""
    valid_from, valid_until = row.get("valid_from"), row.get("valid_until")
    if valid_from and utc(datetime.fromisoformat(valid_from)) > now:
        return False
    return not (valid_until and utc(datetime.fromisoformat(valid_until)) <= now)


class SupabaseProfileRepository(_SupabaseRepository, ProfileRepository):
    table = "user_profiles"

    async def _find(self, user_id: str) -> UserProfile | None:
        rows = await self._run(lambda q: q.select("*").eq("user_id", user_id).limit(1))
        return _read(UserProfile, rows[0]) if rows else None

    async def get_or_create(self, user_id: str) -> UserProfile:
        found = await self._find(user_id)
        if found is not None:
            return found
        profile = UserProfile(user_id=user_id)
        try:
            rows = await self._run(lambda q: q.insert(to_row(profile)))
        except errors.PersistenceError:
            # A concurrent first request may have created it (primary-key conflict).
            found = await self._find(user_id)
            if found is None:
                raise
            return found
        return _read(UserProfile, rows[0]) if rows else profile

    async def update(self, user_id: str, **fields: Any) -> UserProfile:
        updated = apply_profile_update(await self.get_or_create(user_id), fields)
        row = to_row(updated)
        rows = await self._run(
            lambda q: q.update({name: row[name] for name in fields}).eq("user_id", user_id)
        )
        return _read(UserProfile, rows[0]) if rows else updated

    async def list_user_ids(self) -> list[str]:
        # Deliberately unfiltered: this enumerates users (see ports.py).
        rows = await self._run(lambda q: q.select("user_id"))
        return [r["user_id"] for r in rows]


class SupabaseSuggestionRepository(_SupabaseRepository, SuggestionRepository):
    table = "proactive_suggestions"

    async def add(self, user_id: str, s: Suggestion) -> Suggestion:
        stored = prepare_new(user_id, s, "suggestion")
        rows = await self._run(lambda q: q.insert(to_row(stored)))
        return _read(Suggestion, rows[0]) if rows else stored

    async def list(
        self, user_id: str, *, status: SuggestionStatus | None = None, limit: int = 100
    ) -> list[Suggestion]:
        """Return the user's newest ``limit`` suggestions, newest first.

        Newest first, so PostgREST's silent read cap could only drop the oldest.
        """
        check_limit(limit)

        def build(q: Any) -> Any:
            q = q.select("*").eq("user_id", user_id)
            if status is not None:
                q = q.eq("status", SuggestionStatus(status).value)
            return q.order("created_at", desc=True).limit(limit)

        return [_read(Suggestion, r) for r in await self._run(build)]

    async def dedupe_keys(
        self, user_id: str, *, status: SuggestionStatus, limit: int = 500
    ) -> list[str]:
        """Selects only the action column, newest first."""
        check_limit(limit)
        rows = await self._run(
            lambda q: q.select("action")
            .eq("user_id", user_id)
            .eq("status", SuggestionStatus(status).value)
            .order("created_at", desc=True)
            .limit(limit)
        )
        keys = ((r.get("action") or {}).get("dedupe_key") for r in rows)
        return [key for key in keys if isinstance(key, str)]

    async def get(self, user_id: str, suggestion_id: str) -> Suggestion | None:
        if not is_uuid(suggestion_id):
            return None
        rows = await self._run(
            lambda q: q.select("*").eq("id", suggestion_id).eq("user_id", user_id).limit(1)
        )
        return _read(Suggestion, rows[0]) if rows else None

    async def set_status(
        self,
        user_id: str,
        suggestion_id: str,
        to: SuggestionStatus,
        *,
        expected: SuggestionStatus | None = None,
        consent: ConsentDecision | None = None,
    ) -> Suggestion:
        if not is_uuid(suggestion_id):
            raise not_found("suggestion", suggestion_id)
        payload: dict = {"status": SuggestionStatus(to).value}
        if consent is not None:
            payload |= {
                "consent_behavior": consent.behavior,
                "consent_reason": consent.message,
                "consent_kind": consent.kind.value,
            }

        def build(q: Any) -> Any:
            q = q.update(payload).eq("id", suggestion_id).eq("user_id", user_id)
            if expected is not None:  # compare-and-set: only a row still in ``expected``
                q = q.eq("status", SuggestionStatus(expected).value)
            return q

        rows = await self._run(build)
        if rows:
            return _read(Suggestion, rows[0])
        if expected is None or await self.get(user_id, suggestion_id) is None:
            raise not_found("suggestion", suggestion_id)
        raise suggestion_changed(suggestion_id, expected)

    async def exists_today(
        self, user_id: str, trigger: str, day: date, *, tz: str = "UTC"
    ) -> bool:
        start, end = day_bounds(day, tz)
        rows = await self._run(
            lambda q: q.select("id")
            .eq("user_id", user_id)
            .eq("trigger", trigger)
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .limit(1)
        )
        return bool(rows)


class SupabaseMemoryRepository(_SupabaseRepository, MemoryRepository):
    """Reads name their columns (never ``embedding``); similarity search is the
    ``match_memories`` database function, which filters and ranks in one query."""

    table = "user_memories"

    async def _write(self, build: Callable[[Any], Any], memory: Memory) -> list[dict]:
        """Run a write of ``memory``; a clash on one of its unique keys is a ConflictError.

        Only the two keys named here are conflicts; any other failure stays a
        PersistenceError.
        """
        try:
            return await self._run(build)
        except errors.PersistenceError as exc:
            text = _error_text(exc.__cause__) if isinstance(exc.__cause__, Exception) else ""
            if _PATTERN_KEY_INDEX in text:
                raise pattern_key_taken(memory.pattern_key) from exc.__cause__
            if _MEMORY_PRIMARY_KEY in text:
                raise memory_id_taken(memory.id) from exc.__cause__
            raise

    async def add(
        self, user_id: str, m: Memory, *, embedding: list[float] | None = None
    ) -> Memory:
        stored = prepare_new_memory(user_id, m)
        row = memory_to_row(stored)
        if embedding is not None:
            row[EMBEDDING] = embedding_values(embedding)
        rows = await self._write(lambda q: q.insert(row), stored)
        return _read_memory(rows[0]) if rows else stored

    async def get(self, user_id: str, memory_id: str) -> Memory | None:
        if not is_uuid(memory_id):  # the id column is a uuid; anything else matches nothing
            return None
        rows = await self._run(
            lambda q: q.select(_MEMORY_SELECT).eq("id", memory_id).eq("user_id", user_id).limit(1)
        )
        return _read_memory(rows[0]) if rows else None

    async def list(
        self,
        user_id: str,
        *,
        statuses: frozenset[MemoryStatus] = frozenset({MemoryStatus.ACTIVE}),
        types: frozenset[str] | None = None,
    ) -> list[Memory]:
        def build(q: Any) -> Any:
            q = (
                q.select(_MEMORY_SELECT)
                .eq("user_id", user_id)
                .in_("status", sorted(MemoryStatus(s).value for s in statuses))
                .is_("superseded_by", "null")
            )
            if types is not None:
                q = q.in_("memory_type", sorted(types))
            return q.order("created_at")

        return [_read_memory(r) for r in await self._run(build)]

    async def find_by_pattern_key(self, user_id: str, pattern_key: str) -> Memory | None:
        rows = await self._run(
            lambda q: q.select(_MEMORY_SELECT)
            .eq("user_id", user_id)
            .eq("pattern_key", pattern_key)
            .limit(1)
        )
        return _read_memory(rows[0]) if rows else None

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
        params = {
            "p_user_id": user_id,
            "p_query": embedding_values(embedding),
            "p_statuses": sorted(MemoryStatus(s).value for s in statuses),
            "p_types": sorted(types) if types is not None else None,
            "p_limit": check_limit(limit),
            "p_min_similarity": min_similarity,
        }
        try:
            result = await asyncio.to_thread(
                lambda: self._client.rpc("match_memories", params).execute()
            )
        except Exception as exc:  # any transport or PostgREST failure
            raise errors.PersistenceError(_UNREACHABLE) from exc
        try:
            return [
                MemoryMatch(memory=_read_memory(row), similarity=float(row["similarity"]))
                for row in result.data or []
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise errors.PersistenceError(_UNREADABLE) from exc

    async def update(
        self,
        user_id: str,
        memory_id: str,
        *,
        only_if_status: frozenset[MemoryStatus] | None = None,
        **fields: Any,
    ) -> Memory:
        current = await self.get(user_id, memory_id)
        if current is None:
            raise not_found("memory", memory_id)
        if only_if_status is not None and current.status not in only_if_status:
            raise memory_changed(memory_id, current.status)
        updated = apply_memory_update(current, fields)
        changed = [name for name in fields if name != EMBEDDING]
        payload = memory_to_row(updated, changed)
        if EMBEDDING in fields:
            payload[EMBEDDING] = embedding_values(fields[EMBEDDING])
        if not payload:
            return current

        def build(q: Any) -> Any:
            q = q.update(payload).eq("id", memory_id).eq("user_id", user_id)
            if only_if_status is not None:
                # The guard is part of the UPDATE itself: a status changed since the
                # read above makes it match no row, so nothing is overwritten.
                q = q.in_("status", sorted(MemoryStatus(s).value for s in only_if_status))
            return q

        rows = await self._write(build, updated)
        if rows:
            return _read_memory(rows[0])
        now = await self.get(user_id, memory_id)
        if now is None:  # deleted since it was read
            raise not_found("memory", memory_id)
        raise memory_changed(memory_id, now.status)


class SupabaseUsageRepository(_SupabaseRepository, UsageRepository):
    table = "llm_usage_events"

    async def record(self, event: LLMUsageEvent) -> None:
        # Deliberately takes no user_id: the event carries its own (see ports.py).
        await self._run(lambda q: q.insert(to_row(event)))

    async def summary(self, user_id: str, *, since: datetime | None = None) -> dict:
        # PostgREST answers at most max_rows rows per read and says nothing, so the
        # summary reads page after page (oldest first, a stable order) until one comes
        # back short. Past USAGE_MAX_PAGES it stops and says so (truncated), never
        # reporting a short total as the whole.
        rows: list[dict] = []
        for page in range(USAGE_MAX_PAGES):
            first = page * USAGE_PAGE_ROWS

            def build(q: Any, first: int = first) -> Any:
                q = q.select(",".join(USAGE_SUMMARY_COLUMNS)).eq("user_id", user_id)
                if since is not None:
                    q = q.gte("created_at", iso(since))
                return (
                    q.order("created_at").order("id").range(first, first + USAGE_PAGE_ROWS - 1)
                )

            batch = await self._run(build)
            rows.extend(batch)
            if len(batch) < USAGE_PAGE_ROWS:
                return summarize_usage_rows(rows)
        return summarize_usage_rows(rows, truncated=True)
