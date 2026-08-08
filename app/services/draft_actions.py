"""Draft accept/resolve logic shared by the orchestrator node and the REST API.

Two callers act on the same draft rows and must behave identically:

* ``app/orchestrator/graph.py::handle_draft_action`` — the conversational
  "accept"/"reject" turn inside the LangGraph orchestrator.
* ``app/api/v1/endpoints/drafts.py`` — ``POST /api/v1/drafts/{id}/accept``,
  which the frontend calls from the review UI.

Accepting is the *only* moment v2 writes to ``user_tasks``: the planning
sub-graph proposes a draft and persists nothing. Duplicating that two-step
(flip ``draft_schedules.status``, then ``_persist_fused_tasks``) invites the two
paths to drift, so it lives here once and both callers import it.

Every store call takes ``user_id`` — ``DraftStore`` filters on it, so a draft
belonging to somebody else simply reads as absent (house IDOR rule).
"""

import asyncio
from typing import Any, Optional

from app.core.jarvis_logger import JARVIS_LOGGER as logger


def draft_id_of(row: Any) -> Optional[str]:
    """Read the draft id out of a ``draft_schedules`` row.

    The table's primary key column is ``id`` (migration 20260329000002) and
    ``DraftStore.create_draft`` echoes the inserted row back verbatim — the
    locally generated uuid is *written* as ``id``, never returned as
    ``draft_id``. Accept both names so callers work against the live store and
    against any wrapper that hands back the friendlier one.
    """
    if not isinstance(row, dict):
        return None
    return row.get("draft_id") or row.get("id")


def to_task_chunk(raw: dict, index: int) -> Any:
    """Coerce a stored draft/planning task dict into a valid ``TaskChunk``.

    Draft tasks come from two places that disagree about completeness:
    ``decompose_goal`` emits fully-formed chunks, while ``fuse_tasks`` merges
    pending Supabase rows that carry no ``completion_criteria`` (required) and
    may carry a ``duration_minutes`` above TaskChunk's 25-minute Pomodoro
    ceiling. Strict validation would reject those, so fill and clamp first.

    Used by the planning sub-graph before scheduling and again here when the
    same tasks are read back off an accepted draft.
    """
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


async def resolve_draft(
    draft_store: Any, user_id: str, draft_id: Optional[str] = None
) -> Optional[dict]:
    """Find the draft this turn is about: the named one, else the newest pending.

    ``draft_id`` is authoritative when the caller has one — orphaned pending
    rows can accumulate (a re-plan proposes a second draft without resolving the
    first), so ``get_pending_draft`` alone could act on the wrong schedule. It
    stays as the fallback for turns resumed without a draft_id in state.

    ``DraftStore`` is a synchronous Supabase client; ``to_thread`` keeps the
    round-trip off the event loop (same pattern as planning_graph.create_draft).
    """
    if draft_store is None:
        return None
    if draft_id:
        row = await asyncio.to_thread(draft_store.get_draft, draft_id, user_id)
        if row:
            return row
    return await asyncio.to_thread(draft_store.get_pending_draft, user_id)


def _schedule_map_of(draft: dict) -> Optional[dict]:
    """Pull the ``{task_id: {start_min, end_min}}`` map out of a draft row.

    ``draft_schedules`` has no ``schedule`` column today (the solver output is
    not persisted with the draft), so this is normally ``None`` — accepting then
    stores tasks without wall-clock times, exactly as before. Handled anyway,
    and unwrapped one level, because when the column does arrive it will hold a
    whole ``ScheduleResponse`` payload whose map sits under ``"schedule"``.
    """
    schedule = draft.get("schedule")
    if isinstance(schedule, dict) and "schedule" in schedule:
        return schedule.get("schedule")
    return schedule if isinstance(schedule, dict) else None


def _count_persisted(supabase: Any, user_id: str, task_ids: set) -> int:
    """How many of ``task_ids`` are actually sitting in ``user_tasks`` now.

    The only way to know whether the write landed: ``_persist_fused_tasks``
    catches its own exceptions and returns ``None`` either way
    (control_policy.py:438-439), so calling it successfully proves nothing.
    """
    result = (
        supabase.table("user_tasks")
        .select("task_id")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    )
    stored = {row.get("task_id") for row in (result.data or [])}
    return len(task_ids & stored)


async def accept_draft_and_persist(
    draft_store: Any, user_id: str, draft: dict, supabase: Any
) -> Optional[int]:
    """Write the draft's tasks to ``user_tasks``, then mark the draft accepted.

    Returns:
        ``n > 0`` — that many tasks are verifiably in ``user_tasks`` and the
        draft's status is now 'accepted'.
        ``0`` — the draft held nothing schedulable. Nothing was written and the
        status is untouched; there is nothing here to accept.
        ``None`` — the write did not land. The status is *deliberately* left
        'pending' so ``get_pending_draft`` can still find it and the user can
        retry; the caller must not report success.

    Order matters. Flipping the status first (the obvious reading of "accept
    the draft, then persist it") means a failed persist leaves an
    accepted-but-empty draft that no retry can ever find again — the user's
    schedule silently evaporates. So: persist, verify, then flip.

    Both writes are offloaded with ``to_thread``; ``DraftStore`` and
    ``_persist_fused_tasks`` are sync Supabase calls that would otherwise stall
    the event loop. ``_persist_fused_tasks`` is imported inside the function
    because control_policy pulls in the whole v1 flow at import time.
    """
    from app.services.analytical.control_policy import _persist_fused_tasks

    draft_id = draft_id_of(draft)

    chunks = []
    for index, raw in enumerate(draft.get("tasks") or []):
        if not isinstance(raw, dict):
            continue
        try:
            chunks.append(to_task_chunk(raw, index))
        except Exception as exc:  # pydantic ValidationError and friends
            logger.warning(f"Skipping unparseable draft task {raw!r}: {exc}")

    if not chunks:
        # _persist_fused_tasks refuses empty lists anyway (it would look like a
        # retrieval failure and wipe every pending row); say so plainly here.
        logger.warning(f"Draft {draft_id} holds no persistable tasks — nothing accepted")
        return 0

    if supabase is None:
        logger.error(f"Draft {draft_id} not accepted: no Supabase client to persist through")
        return None

    await asyncio.to_thread(
        _persist_fused_tasks,
        user_id,
        chunks,
        supabase,
        schedule=_schedule_map_of(draft),
        horizon_start=draft.get("horizon_start"),
    )

    task_ids = {c.task_id for c in chunks}
    try:
        landed = await asyncio.to_thread(_count_persisted, supabase, user_id, task_ids)
    except Exception as exc:
        logger.error(f"Could not verify persistence for draft {draft_id}: {exc}")
        return None

    if landed <= 0:
        logger.error(f"Draft {draft_id} persist did not land — leaving it pending for retry")
        return None

    if draft_id:
        await asyncio.to_thread(draft_store.accept_draft, draft_id, user_id)
    return landed
