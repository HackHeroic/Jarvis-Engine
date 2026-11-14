"""The draft re-solve from now: a draft's own tasks re-solved on its horizon, never before now.

``resolve_from_now`` is the one solve outside a planning turn. Its caller, the
replanner's ``reslot_from_now`` (``app/modules/planning_replan.py``), builds the time
slots with the planning module's own ``plan_time_slots``, so every rule a plan keeps
holds here too.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# The re-solve's horizon ladder past the default horizon, in minutes (3 d, 5 d, 7 d).
_LONGER_HORIZONS = (4320, 7200, 10080)


def resolve_from_now(
    tasks: list[dict[str, Any]],
    time_slots: list,
    horizon_start: Any,
    *,
    focus_by_hour: Optional[list[float]] = None,
    zone: Any = None,
    goal_metadata: Optional[dict] = None,
    cognitive_load: Optional[dict] = None,
) -> Optional[tuple[Any, Any]]:
    """Re-solve ``tasks`` on the draft's own horizon, never before now: the draft-edit
    re-solve. ``(ExecutionGraph, GenerateScheduleResponse)``, or None when no rung of
    the horizon ladder fits them (or they cannot be read as tasks).

    ``horizon_start`` is the draft's minute 0; the minutes already gone by are blocked
    (``block_past``). ``time_slots`` (TimeSlots or their dicts) may run past a rung's
    horizon, as planning's do: each rung drops the ones that start past it.
    ``focus_by_hour`` is the user's learned focus profile, ``zone`` their clock.
    CPU-bound (CP-SAT): an async caller runs it with ``asyncio.to_thread``.
    """
    from app.api.v1.endpoints.schedule import run_schedule
    from app.core.config import DEFAULT_HORIZON_MINUTES, MAX_HORIZON_MINUTES
    from app.schemas.context import TimeSlot
    from app.schemas.decomposition import ExecutionGraph, TaskChunk

    try:
        chunks = [
            TaskChunk(
                task_id=t.get("task_id", "unknown"),
                title=t.get("title", "Task"),
                duration_minutes=t.get("duration_minutes", 15),
                difficulty_weight=t.get("difficulty_weight", 0.5),
                dependencies=t.get("dependencies", []),
                completion_criteria=t.get("completion_criteria"),
                implementation_intention=t.get("implementation_intention"),
                deadline_hint=t.get("deadline_hint"),
            )
            for t in tasks
        ]
        avg_load = sum(c.difficulty_weight for c in chunks) / max(len(chunks), 1)
        graph = ExecutionGraph(
            goal_metadata=goal_metadata or {},
            decomposition=chunks,
            cognitive_load_estimate=cognitive_load or {"intrinsic_load": round(avg_load, 3)},
        )
    except Exception as exc:  # tasks that cannot be read as tasks cannot be re-solved
        logger.warning("Re-solve skipped: the tasks could not be read (%s)", type(exc).__name__)
        return None
    slots = []
    for slot in time_slots:
        try:
            slots.append(slot if isinstance(slot, TimeSlot) else TimeSlot.model_validate(slot))
        except (ValueError, TypeError):  # pydantic's ValidationError is a ValueError
            logger.warning("Re-solve skipped an unreadable time slot")
    for horizon_min in (DEFAULT_HORIZON_MINUTES, *_LONGER_HORIZONS):
        if horizon_min > MAX_HORIZON_MINUTES:
            break
        try:
            schedule = run_schedule(
                graph,
                # A fresh list each rung: run_schedule appends its night block in place.
                [s for s in slots if s.start_min < horizon_min],
                horizon_minutes=horizon_min,
                horizon_start=horizon_start,
                zone=zone,
                # The draft's horizon began earlier: a re-solve now must not put
                # tasks before now.
                block_past=True,
                focus_by_hour=focus_by_hour,
            )
        except Exception:  # INFEASIBLE on this rung: try a longer horizon
            continue
        return graph, schedule
    return None
