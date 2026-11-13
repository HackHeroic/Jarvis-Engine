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
from typing import Any

from app.domain import errors
from app.domain.entities import (
    BehavioralConstraint,
    DraftSchedule,
    DraftStatus,
    LLMUsageEvent,
    Suggestion,
    SuggestionStatus,
    Task,
    TaskEvent,
    TaskEventType,
    TaskStatus,
    UserProfile,
)
from app.domain.ports import (
    ConstraintRepository,
    DraftRepository,
    ProfileRepository,
    SuggestionRepository,
    TaskEventRepository,
    TaskRepository,
    UsageRepository,
)
from app.infrastructure.persistence._mapping import (
    OPEN_DRAFT_STATUSES,
    accepted_task_rows,
    apply_profile_update,
    day_bounds,
    from_row,
    is_uuid,
    iso,
    new_draft,
    not_found,
    now_utc,
    prepare_new,
    summarize_usage,
    task_from_row,
    task_status_values,
    to_row,
    utc,
)

_UNREACHABLE = "Could not reach the database."


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
        return [task_from_row(r) for r in rows]

    async def get(self, user_id: str, task_id: str) -> Task | None:
        rows = await self._run(
            lambda q: q.select("*").eq("user_id", user_id).eq("task_id", task_id).limit(1)
        )
        return task_from_row(rows[0]) if rows else None

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
        return [task_from_row(r) for r in rows]

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
            return task_from_row(rows[0])
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
        return task_from_row(rows[0])


class SupabaseTaskEventRepository(_SupabaseRepository, TaskEventRepository):
    table = "task_events"

    async def append(self, user_id: str, event: TaskEvent) -> TaskEvent:
        stored = prepare_new(user_id, event, "task event")
        rows = await self._run(lambda q: q.insert(to_row(stored)))
        return from_row(TaskEvent, rows[0]) if rows else stored

    async def list_for_user(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
        types: set[TaskEventType] | None = None,
    ) -> list[TaskEvent]:
        def build(q: Any) -> Any:
            q = q.select("*").eq("user_id", user_id)
            if since is not None:
                q = q.gte("occurred_at", iso(since))
            if types is not None:
                q = q.in_("event_type", sorted(TaskEventType(t).value for t in types))
            return q.order("occurred_at")

        return [from_row(TaskEvent, r) for r in await self._run(build)]


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
        return from_row(DraftSchedule, rows[0]) if rows else draft

    async def get(self, user_id: str, draft_id: str) -> DraftSchedule | None:
        if not is_uuid(draft_id):  # the id column is a uuid; anything else matches nothing
            return None
        rows = await self._run(
            lambda q: q.select("*").eq("id", draft_id).eq("user_id", user_id).limit(1)
        )
        return from_row(DraftSchedule, rows[0]) if rows else None

    async def get_open(self, user_id: str) -> DraftSchedule | None:
        rows = await self._run(
            lambda q: q.select("*")
            .eq("user_id", user_id)
            .in_("status", [s.value for s in OPEN_DRAFT_STATUSES])
            .order("created_at", desc=True)
            .limit(1)
        )
        return from_row(DraftSchedule, rows[0]) if rows else None

    async def replace_tasks(self, user_id: str, draft_id: str, tasks: list[dict]) -> DraftSchedule:
        if not is_uuid(draft_id):
            raise not_found("draft", draft_id)
        rows = await self._run(
            lambda q: q.update({"tasks": tasks}).eq("id", draft_id).eq("user_id", user_id)
        )
        if not rows:
            raise not_found("draft", draft_id)
        return from_row(DraftSchedule, rows[0])

    async def set_status(
        self, user_id: str, draft_id: str, to: DraftStatus, *, reason: str | None = None
    ) -> DraftSchedule:
        to = DraftStatus(to)
        current = await self.get(user_id, draft_id)
        if current is None:
            raise not_found("draft", draft_id)
        current.transition(to)  # raises InvalidStateTransition if the move is illegal
        payload: dict[str, Any] = {"status": to.value}
        if reason is not None:
            payload["rejection_reason"] = reason
        # Compare-and-set on the status we checked, so a concurrent move cannot be overwritten.
        rows = await self._run(
            lambda q: q.update(payload)
            .eq("id", draft_id)
            .eq("user_id", user_id)
            .eq("status", current.status.value)
        )
        if rows:
            return from_row(DraftSchedule, rows[0])
        latest = await self.get(user_id, draft_id)
        if latest is None:
            raise not_found("draft", draft_id)
        latest.transition(to)
        raise errors.StaleVersionError(
            "This draft changed while it was being updated; reload and try again.",
            details={"draft_id": draft_id},
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
            raise errors.PersistenceError(_UNREACHABLE) from exc
        return int(result.data)


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
        return [from_row(BehavioralConstraint, r) for r in rows if _applies_at(r, now)]

    async def add(self, user_id: str, c: BehavioralConstraint) -> BehavioralConstraint:
        stored = prepare_new(user_id, c, "constraint")
        rows = await self._run(lambda q: q.insert(to_row(stored)))
        return from_row(BehavioralConstraint, rows[0]) if rows else stored

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
        return from_row(UserProfile, rows[0]) if rows else None

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
        return from_row(UserProfile, rows[0]) if rows else profile

    async def update(self, user_id: str, **fields: Any) -> UserProfile:
        updated = apply_profile_update(await self.get_or_create(user_id), fields)
        row = to_row(updated)
        rows = await self._run(
            lambda q: q.update({name: row[name] for name in fields}).eq("user_id", user_id)
        )
        return from_row(UserProfile, rows[0]) if rows else updated

    async def list_user_ids(self) -> list[str]:
        # Deliberately unfiltered: this enumerates users (see ports.py).
        rows = await self._run(lambda q: q.select("user_id"))
        return [r["user_id"] for r in rows]


class SupabaseSuggestionRepository(_SupabaseRepository, SuggestionRepository):
    table = "proactive_suggestions"

    async def add(self, user_id: str, s: Suggestion) -> Suggestion:
        stored = prepare_new(user_id, s, "suggestion")
        rows = await self._run(lambda q: q.insert(to_row(stored)))
        return from_row(Suggestion, rows[0]) if rows else stored

    async def list(
        self, user_id: str, *, status: SuggestionStatus | None = None
    ) -> list[Suggestion]:
        """Return the user's suggestions, newest first."""

        def build(q: Any) -> Any:
            q = q.select("*").eq("user_id", user_id)
            if status is not None:
                q = q.eq("status", SuggestionStatus(status).value)
            return q.order("created_at", desc=True)

        return [from_row(Suggestion, r) for r in await self._run(build)]

    async def get(self, user_id: str, suggestion_id: str) -> Suggestion | None:
        if not is_uuid(suggestion_id):
            return None
        rows = await self._run(
            lambda q: q.select("*").eq("id", suggestion_id).eq("user_id", user_id).limit(1)
        )
        return from_row(Suggestion, rows[0]) if rows else None

    async def set_status(
        self, user_id: str, suggestion_id: str, to: SuggestionStatus
    ) -> Suggestion:
        if not is_uuid(suggestion_id):
            raise not_found("suggestion", suggestion_id)
        rows = await self._run(
            lambda q: q.update({"status": SuggestionStatus(to).value})
            .eq("id", suggestion_id)
            .eq("user_id", user_id)
        )
        if not rows:
            raise not_found("suggestion", suggestion_id)
        return from_row(Suggestion, rows[0])

    async def exists_today(self, user_id: str, trigger: str, day: date) -> bool:
        """``day`` is a UTC calendar day."""
        start, end = day_bounds(day)
        rows = await self._run(
            lambda q: q.select("id")
            .eq("user_id", user_id)
            .eq("trigger", trigger)
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .limit(1)
        )
        return bool(rows)


class SupabaseUsageRepository(_SupabaseRepository, UsageRepository):
    table = "llm_usage_events"

    async def record(self, event: LLMUsageEvent) -> None:
        # Deliberately takes no user_id: the event carries its own (see ports.py).
        await self._run(lambda q: q.insert(to_row(event)))

    async def summary(self, user_id: str, *, since: datetime | None = None) -> dict:
        def build(q: Any) -> Any:
            q = q.select("*").eq("user_id", user_id)
            return q.gte("created_at", iso(since)) if since is not None else q

        return summarize_usage(from_row(LLMUsageEvent, r) for r in await self._run(build))
