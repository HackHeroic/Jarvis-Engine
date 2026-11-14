"""Rules both repository families share, so the two cannot drift apart.

Storage rows carry columns the entities do not model: an entity is built from the
fields it declares and the rest is ignored. Every datetime is held in UTC, which is
what a Supabase (UTC) session hands back for a ``timestamptz``; a naive value is
read as UTC, as Postgres reads it.
"""

import copy
import math
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, TypeVar
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import PydanticSerializationError, to_jsonable_python

from app.domain import errors
from app.domain.entities import (
    DraftSchedule,
    DraftStatus,
    LLMLocation,
    LLMUsageEvent,
    Memory,
    MemoryStatus,
    OPEN_DRAFT_STATUSES,  # noqa: F401  (re-exported to the repositories)
    SuggestionStatus,
    Task,
    TaskStatus,
    UserProfile,
)

M = TypeVar("M", bound=BaseModel)

# Matches the draft_schedules.expires_at column default (now() + 24 hours).
DRAFT_TTL = timedelta(hours=24)

# Legacy user_tasks.status values, and the status each one reads as.
_LEGACY_TASK_STATUSES = {"pacing_pushed": TaskStatus.SKIPPED}

_PROFILE_FIELDS = frozenset(UserProfile.model_fields) - {"user_id"}

# Memory fields an update may set; the embedding is stored beside the entity.
_MEMORY_FIELDS = frozenset(Memory.model_fields) - {"id", "user_id"}
EMBEDDING = "embedding"
# The width of user_memories.embedding, vector(384): all-MiniLM-L6-v2's output.
MEMORY_EMBEDDING_DIMENSIONS = 384
# Memory fields held in uuid columns. They are soft references (no foreign key), but
# Postgres still rejects a value that is not a UUID, so both families check the form.
_MEMORY_UUID_FIELDS = ("source_session_id", "source_message_id", "superseded_by")

# The entity's origin is the legacy user_memories.source column, which the v1 memory
# path still writes; the new provenance column says whether the user said it.
_MEMORY_COLUMN_FOR = {"origin": "source"}
_MEMORY_FIELD_FOR = {column: name for name, column in _MEMORY_COLUMN_FOR.items()}

# The user_memories columns every memory read selects: the entity's, never the
# embedding (384 floats a row, which no caller reads).
MEMORY_COLUMNS: tuple[str, ...] = tuple(
    _MEMORY_COLUMN_FOR.get(name, name) for name in Memory.model_fields
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc(dt: datetime) -> datetime:
    """Return the same instant in UTC; a naive datetime is taken to be UTC already."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return utc(dt).isoformat()


def day_bounds(day: date, zone: str) -> tuple[datetime, datetime]:
    """Return the instants [start, end), in UTC, of ``day`` on the clock of ``zone``.

    ``zone`` is an IANA name: ``day_bounds(15 Nov, "Asia/Kolkata")`` starts at 18:30
    UTC on the 14th. Each bound is local midnight, so a day with a daylight-saving
    change is 23 or 25 hours long. An unknown zone raises ValidationError.
    """
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise errors.ValidationError("That is not a known timezone.", details={"tz": zone}) from exc
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    return utc(start), utc(end)


def is_uuid(value: str) -> bool:
    """Return whether ``value`` can be an id in a uuid primary-key column."""
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def not_found(what: str, entity_id: str) -> errors.NotFoundError:
    return errors.NotFoundError(f"That {what} does not exist.", details={f"{what}_id": entity_id})


def suggestion_changed(suggestion_id: str, expected: SuggestionStatus) -> errors.StaleVersionError:
    """A compare-and-set on a suggestion's status lost: it is no longer ``expected``."""
    return errors.StaleVersionError(
        "That suggestion was already handled, so nothing changed.",
        details={"suggestion_id": suggestion_id, "expected": SuggestionStatus(expected).value},
    )


def draft_closed(draft_id: str, status: DraftStatus | str | None) -> errors.InvalidStateTransition:
    """An edit aimed at a draft that is no longer open (accepted, rejected, superseded...)."""
    status_value = getattr(status, "value", status)
    return errors.InvalidStateTransition(
        "This draft is no longer open, so it cannot be changed.",
        details={"draft_id": draft_id, "from": status_value},
    )


def task_id_clash(draft_id: str) -> errors.ConflictError:
    """Accepting the draft would break user_tasks' UNIQUE (user_id, task_id).

    A task the user already finished keeps its id, since accept replaces only
    pending tasks. A plan that reuses that id, or repeats one, can never be written.
    """
    return errors.ConflictError(
        "That plan clashes with tasks you already have.", details={"draft_id": draft_id}
    )


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


def memory_to_row(memory: Memory, fields: Iterable[str] | None = None) -> dict:
    """Return ``memory`` as a user_memories row: every field, or only ``fields``."""
    row = to_row(memory)
    names = Memory.model_fields if fields is None else fields
    return {_MEMORY_COLUMN_FOR.get(name, name): row[name] for name in names}


def memory_from_row(row: dict) -> Memory:
    """Build a Memory from a user_memories row (or a match_memories result row).

    A legacy row may hold NULL where the entity has a default (confidence, strength,
    stability, observation_count, source): the default applies. A missing created_at
    reads as updated_at, then strength_as_of; a missing strength_as_of as created_at.
    """
    data = {_MEMORY_FIELD_FOR.get(key, key): value for key, value in row.items()}
    created_at = data.get("created_at") or data.get("updated_at") or data.get("strength_as_of")
    data["created_at"] = created_at or now_utc()
    data["strength_as_of"] = data.get("strength_as_of") or data["created_at"]
    return from_row(Memory, data)


def _check_memory_references(memory: Memory) -> Memory:
    for name in _MEMORY_UUID_FIELDS:
        value = getattr(memory, name)
        if value is not None and not is_uuid(value):
            raise errors.ValidationError(
                f"A memory's {name} must be a UUID.", details={name: value}
            )
    return memory


def prepare_new_memory(user_id: str, memory: Memory) -> Memory:
    """``prepare_new`` for a memory, which also checks its uuid references' form."""
    return _check_memory_references(prepare_new(user_id, memory, "memory"))


def apply_memory_update(memory: Memory, fields: dict[str, Any]) -> Memory:
    """Return ``memory`` with ``fields`` applied; an ``embedding`` key is left to the caller.

    Raises ValidationError on an unknown field (``id`` and ``user_id`` included), a
    value the memory rejects, or a reference that is not a UUID.
    """
    unknown = sorted(set(fields) - _MEMORY_FIELDS - {EMBEDDING})
    if unknown:
        raise errors.ValidationError("Those are not memory fields.", details={"fields": unknown})
    changes = {name: value for name, value in fields.items() if name != EMBEDDING}
    try:
        updated = normalized(Memory.model_validate({**memory.model_dump(), **changes}))
    except PydanticValidationError as exc:
        bad = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        raise errors.ValidationError(
            "Some memory fields are invalid.", details={"fields": bad}
        ) from exc
    return _check_memory_references(updated)


def embedding_values(embedding: list[float] | None) -> list[float] | None:
    """Return a checked copy of ``embedding`` as plain floats (None stays None).

    Raises ValidationError unless it holds exactly MEMORY_EMBEDDING_DIMENSIONS finite
    numbers with a non-zero norm: the column rejects any other length, and a zero
    vector has no direction (pgvector's cosine distance to it is NaN).
    """
    if embedding is None:
        return None
    try:
        values = [float(x) for x in embedding]
    except (TypeError, ValueError) as exc:
        raise errors.ValidationError("An embedding must be a list of numbers.") from exc
    if (
        len(values) != MEMORY_EMBEDDING_DIMENSIONS
        or not all(math.isfinite(v) for v in values)
        or math.fsum(v * v for v in values) == 0.0
    ):
        raise errors.ValidationError(
            f"An embedding must be {MEMORY_EMBEDDING_DIMENSIONS} finite numbers, not all zero.",
            details={"length": len(values)},
        )
    return values


def check_limit(limit: int) -> int:
    """Return ``limit``; raise ValidationError if it is negative."""
    if limit < 0:
        raise errors.ValidationError("A limit cannot be negative.", details={"limit": limit})
    return limit


def pattern_key_taken(pattern_key: str | None) -> errors.ConflictError:
    """Storing the memory would break user_memories' unique (user_id, pattern_key)."""
    return errors.ConflictError(
        "Jarvis already remembers this.", details={"pattern_key": pattern_key}
    )


def memory_id_taken(memory_id: str | None) -> errors.ConflictError:
    """Storing the memory would break user_memories' primary key: the id is in use."""
    return errors.ConflictError(
        "A memory with this id already exists.", details={"memory_id": memory_id}
    )


def memory_changed(memory_id: str, status: MemoryStatus | str) -> errors.StaleVersionError:
    """A guarded memory update lost: the memory's status is no longer one it allows."""
    return errors.StaleVersionError(
        "That memory changed in the meantime, so nothing was changed.",
        details={"memory_id": memory_id, "status": MemoryStatus(status).value},
    )


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
