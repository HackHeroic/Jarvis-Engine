"""The replanner: the planning sub-graph run over the tasks the user already has.

``PlanningGraphReplanner`` is the ``DraftPlanner`` the morning brief and an accepted
rule use. It runs the same compiled planning sub-graph a planning turn does, in replan
mode: no goal, so no decomposition; the user's pending tasks, plus the set-aside ones
it is asked to re-slot. The draft it proposes is stored for review, never written to
the user's tasks. The only model call it can make is PRIMARY habit translation, and
only for habits written as free text.

Its collaborators are the ``PlanningServices`` it is built with, put in the sub-graph's
state as a planning turn's are; it reads the user through them, never through a
process-wide lookup.
"""

import asyncio
from typing import Any

from app.core.config import DEFAULT_HORIZON_MINUTES
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain import errors
from app.domain.entities import DraftSchedule, TaskStatus
from app.domain.ports import DraftPlanner
from app.services.planning.services import PlanningServices

_EXHAUSTED_FALLBACK = "I couldn't fit everything in even with a 30-day window."
_HABITS_UNREAD = "I couldn't read your habits just now, so the plan waits until I can."
_HABITS_UNREAD_ON_ACCEPT = (
    "A few of those times had already gone by, and I couldn't read your habits just now"
    " to fit them again, so I haven't locked it in. The draft is still open: try again"
    " in a moment."
)


class _PlannedUser:
    """The user as the planning sub-graph reads them (the ``UserModel`` surface it uses),
    through the replanner's ports: the rules that currently apply, the pending and
    set-aside tasks, the profile. Each is read once and kept, as ``UserModel`` does."""

    def __init__(self, user_id: str, planning: PlanningServices) -> None:
        self._user_id = user_id
        self._planning = planning
        self._cache: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def user_id(self) -> str:
        return self._user_id

    async def _once(self, key: str, read: Any) -> Any:
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key not in self._cache:
                self._cache[key] = await read()
            return self._cache[key]

    async def get_profile(self) -> Any:
        """The user's ``UserProfile``; None when it cannot be read (the default zone)."""

        async def read() -> Any:
            try:
                return await self._planning.profiles.get_or_create(self._user_id)
            except Exception as exc:  # no profile is a default, never a failed plan
                logger.warning(f"Profile read failed (default timezone used): {type(exc).__name__}")
                return None

        return await self._once("profile", read)

    async def get_behavioral_constraints(self) -> list[dict]:
        async def read() -> list[dict]:
            rules = await self._planning.constraints.list_active(self._user_id)
            return [rule.model_dump(mode="json") for rule in rules]

        return await self._once("constraints", read)

    async def get_pending_tasks(self) -> list[dict]:
        async def read() -> list[dict]:
            tasks = await self._planning.task_repo.list_pending(self._user_id)
            return [task.model_dump(mode="json") for task in tasks]

        return await self._once("pending_tasks", read)

    async def get_missed_tasks(self) -> list[dict]:
        """The user's set-aside tasks, newest first, at most TASK_LIST_LIMIT."""

        async def read() -> list[dict]:
            tasks = await self._planning.task_repo.list_by_status(
                self._user_id, frozenset({TaskStatus.MISSED})
            )
            return [task.model_dump(mode="json") for task in tasks]

        return await self._once("missed_tasks", read)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)


class PlanningGraphReplanner(DraftPlanner):
    """Redrafts the user's plan with the planning sub-graph (replan mode)."""

    def __init__(self, planning: PlanningServices) -> None:
        self._planning = planning
        self._graph: Any = None

    async def replan(self, user_id: str, *, reslot_task_ids: list[str]) -> DraftSchedule | None:
        """Draft the pending tasks plus the missed ones in ``reslot_task_ids``.

        None when there is nothing to plan, before anything else runs (no habit is
        translated for an empty plan), and when no draft could be proposed.
        ScheduleInfeasibleError when nothing fits even in 30 days; PersistenceError
        when the user's tasks cannot be read. LLMUnavailableError, a transient error,
        when the user's free-text habits could not be read (the model failed, or its
        time ran out): never a draft that ignores them, so a later try can make it.
        """
        if not await self._anything_to_plan(
            _PlannedUser(user_id, self._planning), set(reslot_task_ids)
        ):
            return None

        from app.modules.planning_graph import habits_unread, planning_state_in

        user = _PlannedUser(user_id, self._planning)  # fresh: nothing read before this run
        state = planning_state_in({"user_model": user, "user_message": ""})
        state.update(
            user_id=user_id,
            planning=self._planning,
            replan_only=True,
            reslot_task_ids=list(reslot_task_ids),
            horizon_minutes=DEFAULT_HORIZON_MINUTES,
            retry_count=0,
        )
        result = await self._compiled().ainvoke(state)

        if habits_unread(result):
            raise errors.LLMUnavailableError(_HABITS_UNREAD)
        if result.get("error") == "INFEASIBLE_EXHAUSTED":
            raise errors.ScheduleInfeasibleError(
                result.get("clarification_request") or _EXHAUSTED_FALLBACK
            )
        draft_id = result.get("draft_id")
        return await self._planning.drafts.get(user_id, draft_id) if draft_id else None

    async def reslot_from_now(self, user_id: str, draft: DraftSchedule) -> dict | None:
        """The draft's own tasks re-solved from now (see ``DraftPlanner``).

        The time slots are the planning module's own (``plan_time_slots``), from the
        draft's horizon; the solve is the draft-edit re-solve (``resolve_from_now``) on
        the user's clock with their learned focus.
        """
        from datetime import datetime, timezone

        from app.modules.planning_graph import _learned_focus, compact_schedule, plan_time_slots
        from app.services.analytical.schedule_modifier import resolve_from_now
        from app.services.planning.local_time import user_zone

        user = _PlannedUser(user_id, self._planning)
        horizon_start = datetime.fromisoformat(draft.horizon_start)
        if horizon_start.tzinfo is None:  # a legacy horizon without an offset was UTC
            horizon_start = horizon_start.replace(tzinfo=timezone.utc)
        built = await plan_time_slots({
            "user_id": user_id,
            "user_model": user,
            "horizon_start": horizon_start.isoformat(),
        })
        if built["habits_unread"]:
            raise errors.LLMUnavailableError(_HABITS_UNREAD_ON_ACCEPT)
        zone = user_zone(await user.get_profile())
        focus = await _learned_focus(user_id, zone)
        resolved = await asyncio.to_thread(
            resolve_from_now,
            draft.tasks,
            built["time_slots"],
            horizon_start,
            focus_by_hour=focus,
            zone=zone,
        )
        if resolved is None:
            return None
        _graph, schedule = resolved
        return compact_schedule(schedule.model_dump(mode="json"))

    @staticmethod
    async def _anything_to_plan(user: _PlannedUser, reslot_ids: set[str]) -> bool:
        if await user.get_pending_tasks():
            return True
        if not reslot_ids:
            return False
        return any(t.get("task_id") in reslot_ids for t in await user.get_missed_tasks())

    def _compiled(self) -> Any:
        """The compiled planning sub-graph, built on first use and reused."""
        if self._graph is None:
            from app.modules.planning_graph import build_planning_graph

            self._graph = build_planning_graph()
        return self._graph
