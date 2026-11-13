"""Rules both repository families share, so the two cannot drift apart.

Storage rows carry columns the entities do not model: an entity is built from the
fields it declares and the rest is ignored. Every datetime is held in UTC, which is
what a Supabase (UTC) session hands back for a ``timestamptz``; a naive value is
read as UTC, as Postgres reads it.
"""

import copy
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import PydanticSerializationError, to_jsonable_python

from app.domain import errors
from app.domain.entities import (
    DraftSchedule,
    DraftStatus,
    LLMLocation,
    LLMUsageEvent,
    Task,
    TaskStatus,
    UserProfile,
)

M = TypeVar("M", bound=BaseModel)

# Matches the draft_schedules.expires_at column default (now() + 24 hours).
DRAFT_TTL = timedelta(hours=24)
OPEN_DRAFT_STATUSES = (DraftStatus.PENDING, DraftStatus.MODIFIED)

# Legacy user_tasks.status values, and the status each one reads as.
_LEGACY_TASK_STATUSES = {"pacing_pushed": TaskStatus.SKIPPED}

_PROFILE_FIELDS = frozenset(UserProfile.model_fields) - {"user_id"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc(dt: datetime) -> datetime:
    """Return the same instant in UTC; a naive datetime is taken to be UTC already."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return utc(dt).isoformat()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return the UTC instants [start, end) of ``day``."""
    start = datetime.combine(day, time(0), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def is_uuid(value: str) -> bool:
    """Return whether ``value`` can be an id in a uuid primary-key column."""
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def not_found(what: str, entity_id: str) -> errors.NotFoundError:
    return errors.NotFoundError(f"That {what} does not exist.", details={f"{what}_id": entity_id})


def normalized(entity: M) -> M:
    """Return a deep copy of ``entity`` with every datetime field in UTC."""
    updates = {name: utc(value) for name, value in entity if isinstance(value, datetime)}
    return entity.model_copy(update=updates, deep=True)


def to_row(entity: BaseModel) -> dict:
    """Return ``entity`` as a JSON-ready storage row."""
    return normalized(entity).model_dump(mode="json")


def from_row(model: type[M], row: dict) -> M:
    """Build ``model`` from a storage row: declared fields only; NULL means "use the default"."""
    data = {
        key: value
        for key, value in row.items()
        if key in model.model_fields and value is not None
    }
    return normalized(model.model_validate(data))


def task_from_row(row: dict) -> Task:
    """Build a Task from a user_tasks row, reading legacy statuses as their current meaning."""
    status = row.get("status")
    if status in _LEGACY_TASK_STATUSES:
        row = {**row, "status": _LEGACY_TASK_STATUSES[status].value}
    return from_row(Task, row)


def task_status_values(status: TaskStatus) -> list[str]:
    """Return every stored user_tasks.status value that reads as ``status``."""
    legacy = [raw for raw, reads_as in _LEGACY_TASK_STATUSES.items() if reads_as is status]
    return [status.value, *legacy]


def prepare_new(user_id: str, entity: M, what: str) -> M:
    """Check ``entity`` may be stored for ``user_id`` and return it normalised, with an id.

    Raises ValidationError if the entity belongs to another user or carries an id
    that a uuid primary key would reject.
    """
    if entity.user_id != user_id:
        raise errors.ValidationError(f"This {what} belongs to a different user.")
    if entity.id is not None and not is_uuid(entity.id):
        raise errors.ValidationError(f"A {what} id must be a UUID.", details={"id": entity.id})
    entity = normalized(entity)
    return entity if entity.id is not None else entity.model_copy(update={"id": str(uuid4())})


def accepted_task_rows(user_id: str, rows: list[dict]) -> list[dict]:
    """Return ``rows`` stamped with ``user_id`` and made JSON-safe, after checking each one.

    Both families receive exactly these values (a datetime becomes its ISO string), as
    the database function receives them over JSON. Raises ValidationError, before
    anything is written, if a row is for another user, carries an id a uuid primary
    key would reject, holds a value JSON cannot carry, or would not read back as a Task.
    """
    checked = []
    for row in rows:
        if row.get("user_id") not in (None, user_id):
            raise errors.ValidationError("A task row belongs to a different user.")
        if row.get("id") is not None and not is_uuid(row["id"]):
            raise errors.ValidationError("A task id must be a UUID.", details={"id": row["id"]})
        try:
            row = to_jsonable_python({**row, "user_id": user_id})
        except PydanticSerializationError as exc:
            raise errors.ValidationError(
                "A task row holds a value that cannot be stored.",
                details={"task_id": row.get("task_id")},
            ) from exc
        try:
            task_from_row(row)
        except PydanticValidationError as exc:
            raise errors.ValidationError(
                "A task row is not a valid task.", details={"task_id": row.get("task_id")}
            ) from exc
        checked.append(row)
    return checked


def new_draft(
    user_id: str,
    *,
    tasks: list[dict],
    horizon_start: str | None,
    goal_id: str | None,
    schedule: dict | None,
) -> DraftSchedule:
    return DraftSchedule(
        id=str(uuid4()),
        user_id=user_id,
        goal_id=goal_id,
        tasks=copy.deepcopy(tasks),
        schedule=copy.deepcopy(schedule),
        horizon_start=horizon_start,
        status=DraftStatus.PENDING,
        expires_at=now_utc() + DRAFT_TTL,
    )


def apply_profile_update(profile: UserProfile, fields: dict[str, Any]) -> UserProfile:
    """Return ``profile`` with ``fields`` applied.

    Raises ValidationError on an unknown field or a value the profile rejects.
    """
    unknown = sorted(set(fields) - _PROFILE_FIELDS)
    if unknown:
        raise errors.ValidationError("Those are not profile settings.", details={"fields": unknown})
    try:
        return UserProfile.model_validate({**profile.model_dump(), **fields})
    except PydanticValidationError as exc:
        bad = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        raise errors.ValidationError(
            "Some profile settings are invalid.", details={"fields": bad}
        ) from exc


def summarize_usage(events: Iterable[LLMUsageEvent]) -> dict:
    totals = {
        "local_calls": 0,
        "cloud_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "pii_redactions": 0,
    }
    for event in events:
        totals["local_calls" if event.location is LLMLocation.LOCAL else "cloud_calls"] += 1
        totals["input_tokens"] += event.input_tokens
        totals["output_tokens"] += event.output_tokens
        totals["pii_redactions"] += event.pii_redactions
    return totals
