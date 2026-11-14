"""Goal ids and task-id namespacing for planned chunks. Pure: no I/O.

Every chunk a planning run decomposes leaves it as ``{goal_id}_{run}_{task_id}``,
dependencies included: ``derive_goal_id`` names the goal, ``mint_run_token`` names the
run, ``namespace_chunk`` applies both. Fusion tells one goal's pending tasks from
another's by this namespace, and a re-plan never reuses a finished task's id.
"""

import uuid
from typing import Any

from app.schemas.decomposition import TaskChunk


def derive_goal_id(goal_metadata: Any, fallback_objective: str = "") -> str:
    """The namespace every chunk of one goal is prefixed with.

    Order matters: the model's own ``goal_metadata.goal_id`` wins, else the
    objective (or the raw planning goal when the model left it blank) is
    slugified, else a uuid so two goals can never share a namespace by accident.

    The one derivation, which the planning sub-graph calls: a second copy would
    drift and re-open F5, where un-namespaced positional ids let a new plan
    shadow (and then delete) the previous one's pending rows.
    """
    goal_id = getattr(goal_metadata, "goal_id", None) if goal_metadata else None
    if goal_id:
        return goal_id

    objective = (getattr(goal_metadata, "objective", "") if goal_metadata else "") or fallback_objective or ""
    slug = "".join(c if c.isalnum() or c == " " else "" for c in objective)[:30].replace(" ", "_").lower()
    return slug or f"plan_{uuid.uuid4().hex[:8]}"


def mint_run_token() -> str:
    """A short token naming one planning run: 6 hex characters of a uuid4.

    The decomposer's ids are positional (task_1…task_N), so ``{goal_id}_{task_id}``
    alone repeats every time a goal is re-planned. Accepting a plan deletes only
    the user's pending tasks: one already completed, skipped or missed keeps its id,
    the re-plan emits that id again, and the unique key on user_tasks
    (user_id, task_id) refuses the whole accept. Six hex characters, not four,
    because a token reused by two runs of the same goal causes that failure, and
    a goal is re-planned many times over its life.
    """
    return uuid.uuid4().hex[:6]


def namespace_chunk(chunk: TaskChunk, goal_id: str, run: str) -> TaskChunk:
    """Prefix task_id and dependencies with ``{goal_id}_{run}_`` for multi-goal fusion.

    ``run`` comes from ``mint_run_token``, once per planning run. The ids stay
    ``{goal_id}_``-prefixed, which is how fusion tells this goal's pending tasks
    from every other goal's.

    CRITICAL: dependencies must use prefixed refs so the OR-Tools solver's
    build_dependencies() receives consistent IDs for precedence constraints.
    """
    prefix = f"{goal_id}_{run}_"
    prefixed_id = f"{prefix}{chunk.task_id}"
    prefixed_deps = [f"{prefix}{d}" for d in chunk.dependencies]
    return TaskChunk(
        task_id=prefixed_id,
        title=chunk.title,
        duration_minutes=chunk.duration_minutes,
        difficulty_weight=chunk.difficulty_weight,
        dependencies=prefixed_deps,
        completion_criteria=chunk.completion_criteria,
        implementation_intention=chunk.implementation_intention,
        deadline_hint=chunk.deadline_hint,
    )
