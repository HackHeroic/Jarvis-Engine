"""From planned task chunks to ``user_tasks`` rows. Pure: no I/O.

Two writers share this builder: draft acceptance (``DraftService``) and the
deprecated v1 ``_persist_fused_tasks``. With one builder, the rows the two paths
write cannot drift apart.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def to_task_chunk(raw: dict, index: int) -> Any:
    """Coerce a stored draft/planning task dict into a valid ``TaskChunk``.

    Draft tasks come from two places that disagree about completeness:
    ``decompose_goal`` emits fully-formed chunks, while ``fuse_tasks`` merges
    pending Supabase rows that carry no ``completion_criteria`` (required) and
    may carry a ``duration_minutes`` above TaskChunk's 25-minute Pomodoro
    ceiling. Strict validation would reject those, so fill and clamp first.

    Used by the planning sub-graph before scheduling and again when the same
    tasks are read back off a draft being accepted. Raises pydantic's
    ValidationError (a ValueError), or OverflowError for an infinite duration,
    when the dict cannot become a chunk.
    """
    # TaskChunk is defined in the API layer; imported here, lazily, as before.
    from app.api.v1.endpoints.reasoning import TaskChunk

    data = dict(raw)
    data.setdefault("task_id", f"t{index}")
    data.setdefault("title", data["task_id"])
    data.setdefault("completion_criteria", f"Finish: {data['title']}")

    raw_duration = data.get("duration_minutes")
    try:
        duration = int(raw_duration) if raw_duration else 25
    except (TypeError, ValueError):
        duration = 25
    data["duration_minutes"] = max(1, min(25, duration))

    raw_difficulty = data.get("difficulty_weight")
    try:
        difficulty = 0.5 if raw_difficulty is None else float(raw_difficulty)
    except (TypeError, ValueError):
        difficulty = 0.5
    data["difficulty_weight"] = max(0.0, min(1.0, difficulty))

    data["dependencies"] = list(data.get("dependencies") or [])
    return TaskChunk.model_validate(data)


def _goal_id_of(task_id: str) -> str | None:
    """The goal namespace of a prefixed task_id (``thesis_task_1`` -> ``thesis``).

    Legacy fallback only: it reads up to the first underscore, so a multi-word goal
    comes out wrong (``learn_dsa_task_1`` -> ``learn``).
    """
    if not task_id or "_" not in task_id:
        return None
    return task_id.split("_", 1)[0]


def _from_dict(chunk: Any, name: str, default: Any = None) -> Any:
    return chunk.get(name, default) if isinstance(chunk, dict) else None


def _field(chunk: Any, name: str, default: Any = None) -> Any:
    """``chunk.name`` for a TaskChunk, else ``chunk[name]`` for a plain dict."""
    value = getattr(chunk, name, None)
    return _from_dict(chunk, name, default) if value is None else value


def _minutes(slot: dict, *keys: str) -> Any:
    """The first of ``keys`` the slot sets. Minute 0 counts: it is the horizon's start."""
    return next((slot[key] for key in keys if slot.get(key) is not None), None)


def _wall_clock(slot: Any, horizon: datetime) -> tuple[str, str] | None:
    """The slot's (start, end) as ISO instants, or None if it has no usable minutes."""
    # Both key spellings occur: start_min/end_min (planner) and start/end (solver).
    if isinstance(slot, dict):
        start_min = _minutes(slot, "start_min", "start")
        end_min = _minutes(slot, "end_min", "end")
    elif hasattr(slot, "start_min"):
        start_min, end_min = slot.start_min, slot.end_min
    else:
        return None
    if start_min is None or end_min is None:
        return None
    return (
        (horizon + timedelta(minutes=int(start_min))).isoformat(),
        (horizon + timedelta(minutes=int(end_min))).isoformat(),
    )


def build_task_rows(
    user_id: str, chunks: list, *, schedule: dict | None, horizon_start: str | None
) -> tuple[str, list[dict]]:
    """Return ``(plan_id, rows)``: one pending ``user_tasks`` row per chunk.

    Chunks are TaskChunk objects or plain dicts with prefixed task_ids
    (``{goal_id}_{run}_{task_id}``, or ``{goal_id}_{task_id}`` from older plans).
    Every row carries the plan_id minted here.

    A row's goal_id is the chunk's own ``goal_id`` (the planner stamps one on every
    chunk). Only a chunk without one, written before goal_id travelled, falls back
    to reading it off the task_id.

    Args:
        schedule: Optional map of task_id -> {"start_min", "end_min"} (solver
            output). A task in it gets wall-clock ``scheduled_start``/``_end``.
        horizon_start: ISO-8601 instant that the schedule's minute 0 stands for.
    """
    plan_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    horizon: datetime | None = None
    if horizon_start:
        try:
            horizon = datetime.fromisoformat(horizon_start)
        except (ValueError, TypeError):
            logger.warning("Could not parse horizon_start %r; tasks stay timeless", horizon_start)

    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        # The two reading styles are deliberate legacy semantics. task_id, title and
        # duration fall back on any falsy attribute (`or`); every other field falls
        # back only when the attribute is None (`_field`).
        task_id = getattr(chunk, "task_id", None) or _from_dict(chunk, "task_id")
        goal_id = _field(chunk, "goal_id") or (_goal_id_of(task_id) if task_id else None)

        row: dict[str, Any] = {
            "user_id": user_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "title": getattr(chunk, "title", None) or _from_dict(chunk, "title"),
            "status": "pending",
            "duration_minutes": (
                getattr(chunk, "duration_minutes", None) or _from_dict(chunk, "duration_minutes")
            ),
            "difficulty_weight": _field(chunk, "difficulty_weight"),
            "dependencies": _field(chunk, "dependencies", []),
            "deadline_hint": _field(chunk, "deadline_hint"),
            "created_at": now_iso,
        }
        if goal_id:
            row["goal_id"] = goal_id

        criteria = _field(chunk, "completion_criteria")
        if criteria:
            row["completion_criteria"] = criteria

        # WOOP implementation intention, stored as a plain dict.
        intention = _field(chunk, "implementation_intention")
        if intention is not None:
            if hasattr(intention, "model_dump"):
                intention = intention.model_dump()
            elif hasattr(intention, "dict"):
                intention = intention.dict()
            row["implementation_intention"] = intention

        keywords = _field(chunk, "topic_keywords")
        if keywords:
            row["topic_keywords"] = keywords

        if schedule and task_id and task_id in schedule and horizon:
            times = _wall_clock(schedule[task_id], horizon)
            if times:
                row["scheduled_start"], row["scheduled_end"] = times

        rows.append(row)

    return plan_id, rows
