"""The replanner: the planning sub-graph run over the tasks the user already has.

``PlanningGraphReplanner`` is the ``DraftPlanner`` the morning brief and an accepted
rule use. It runs the same compiled planning sub-graph a planning turn does, in replan
mode: no goal, so no decomposition; the user's pending tasks, plus the set-aside ones
it is asked to re-slot. The draft it proposes is stored for review, never written to
the user's tasks. The only model call it can make is PRIMARY habit translation, and
only for habits written as free text.
"""

from collections.abc import Callable
from typing import Any

from app.core.config import DEFAULT_HORIZON_MINUTES
from app.core.runtime import get_db, get_memory_store
from app.core.user_model import UserModel
from app.domain import errors
from app.domain.entities import DraftSchedule
from app.domain.ports import DraftPlanner, DraftRepository
from app.services.draft_store import DraftStore

_EXHAUSTED_FALLBACK = "I couldn't fit everything in even with a 30-day window."
_HABITS_UNREAD = "I couldn't read your habits just now, so the plan waits until I can."
_HABITS_UNREAD_ON_ACCEPT = (
    "A few of those times had already gone by, and I couldn't read your habits just now"
    " to fit them again, so I haven't locked it in. The draft is still open: try again"
    " in a moment."
)


class PlanningGraphReplanner(DraftPlanner):
    """Redrafts the user's plan with the planning sub-graph (replan mode)."""

    def __init__(
        self,
        drafts: DraftRepository,
        *,
        db_provider: Callable[[], Any] = get_db,
        memory_provider: Callable[[], Any] = get_memory_store,
    ) -> None:
        self._drafts = drafts
        self._db_provider = db_provider
        self._memory_provider = memory_provider
        self._graph: Any = None

    async def replan(self, user_id: str, *, reslot_task_ids: list[str]) -> DraftSchedule | None:
        """Draft the pending tasks plus the missed ones in ``reslot_task_ids``.

        None when there is nothing to plan, before anything else runs (no habit is
        translated for an empty plan), and when no draft could be proposed.
        ScheduleInfeasibleError when nothing fits even in 30 days; PersistenceError
        when there is no database to plan from. LLMUnavailableError, a transient error,
        when the user's free-text habits could not be read (the model failed, or its
        time ran out): never a draft that ignores them, so a later try can make it.
        """
        db = self._db_provider()
        if getattr(db, "supabase", None) is None:
            raise errors.PersistenceError("Could not reach the database.")
        if not await self._anything_to_plan(UserModel(user_id, db), set(reslot_task_ids)):
            return None

        from app.modules.planning_graph import habits_unread, planning_state_in

        user_model = UserModel(user_id, db)  # fresh: planning sets aside overdue tasks first
        user_model.set_memory_store(self._memory_provider())
        state = planning_state_in({
            "user_model": user_model,
            "user_message": "",
            "draft_store": DraftStore(db.supabase),
        })
        state.update(
            user_id=user_id,
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
        return await self._drafts.get(user_id, draft_id) if draft_id else None

    async def reslot_from_now(self, user_id: str, draft: DraftSchedule) -> dict | None:
        """The draft's own tasks re-solved from now (see ``DraftPlanner``).

        The time slots are the planning module's own (``plan_time_slots``), from the
        draft's horizon; the solve is the draft-edit re-solve (``resolve_from_now``) on
        the user's clock with their learned focus. PersistenceError without a database.
        """
        import asyncio
        from datetime import datetime, timezone

        from app.modules.planning_graph import _learned_focus, compact_schedule, plan_time_slots
        from app.services.analytical.schedule_modifier import resolve_from_now
        from app.services.planning.local_time import user_zone

        db = self._db_provider()
        if getattr(db, "supabase", None) is None:
            raise errors.PersistenceError("Could not reach the database.")
        user_model = UserModel(user_id, db)
        user_model.set_memory_store(self._memory_provider())
        horizon_start = datetime.fromisoformat(draft.horizon_start)
        if horizon_start.tzinfo is None:  # a legacy horizon without an offset was UTC
            horizon_start = horizon_start.replace(tzinfo=timezone.utc)
        built = await plan_time_slots({
            "user_id": user_id,
            "user_model": user_model,
            "horizon_start": horizon_start.isoformat(),
        })
        if built["habits_unread"]:
            raise errors.LLMUnavailableError(_HABITS_UNREAD_ON_ACCEPT)
        zone = user_zone(await user_model.get_profile())
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
    async def _anything_to_plan(user_model: UserModel, reslot_ids: set[str]) -> bool:
        if await user_model.get_pending_tasks():
            return True
        if not reslot_ids:
            return False
        return any(t.get("task_id") in reslot_ids for t in await user_model.get_missed_tasks())

    def _compiled(self) -> Any:
        """The compiled planning sub-graph, built on first use and reused."""
        if self._graph is None:
            from app.modules.planning_graph import build_planning_graph

            self._graph = build_planning_graph()
        return self._graph
