"""UserModel: the user as a turn reads them, a facade over the repository ports.

Every read goes through a port the composition root built (``ProfileRepository``,
``ConstraintRepository``, ``TaskRepository``); there is no database handle here. Each
read happens once per facade (one turn, or one replan) and is kept; ``invalidate``
drops one after a write.
"""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.domain.entities import TaskStatus, UserProfile
from app.domain.ports import ConstraintRepository, ProfileRepository, TaskRepository

if TYPE_CHECKING:
    from app.services.planning.services import PlanningServices

_log = logging.getLogger(__name__)


class UserModel:
    """The user's profile, rules and tasks, each read once through its port."""

    def __init__(
        self,
        user_id: str,
        *,
        profiles: ProfileRepository,
        constraints: ConstraintRepository,
        tasks: TaskRepository,
    ) -> None:
        self._user_id = user_id
        self._profiles = profiles
        self._constraints = constraints
        self._tasks = tasks
        self._cache: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def for_planning(cls, user_id: str, planning: "PlanningServices") -> "UserModel":
        """The facade over the planning sub-graph's ports: a chat turn and the replanner
        read the user the same way."""
        return cls(
            user_id,
            profiles=planning.profiles,
            constraints=planning.constraints,
            tasks=planning.task_repo,
        )

    @property
    def user_id(self) -> str:
        return self._user_id

    async def _once(self, key: str, read: Callable[[], Awaitable[Any]]) -> Any:
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key not in self._cache:
                self._cache[key] = await read()
            return self._cache[key]

    async def get_profile(self) -> UserProfile | None:
        """The user's ``UserProfile``; None when it cannot be read (the default zone).

        The planner takes the user's timezone from it.
        """

        async def read() -> UserProfile | None:
            try:
                return await self._profiles.get_or_create(self._user_id)
            except Exception as exc:  # no profile is a default, never a failed turn
                _log.warning("Profile read failed (default timezone used): %s", type(exc).__name__)
                return None

        return await self._once("profile", read)

    async def get_behavioral_constraints(self) -> list[dict]:
        """The user's rules that apply now (``ConstraintRepository.list_active``)."""

        async def read() -> list[dict]:
            rules = await self._constraints.list_active(self._user_id)
            return [rule.model_dump(mode="json") for rule in rules]

        return await self._once("constraints", read)

    async def get_pending_tasks(self) -> list[dict]:
        async def read() -> list[dict]:
            tasks = await self._tasks.list_pending(self._user_id)
            return [task.model_dump(mode="json") for task in tasks]

        return await self._once("pending_tasks", read)

    async def get_missed_tasks(self) -> list[dict]:
        """The user's set-aside tasks, newest first, at most TASK_LIST_LIMIT."""

        async def read() -> list[dict]:
            tasks = await self._tasks.list_by_status(
                self._user_id, frozenset({TaskStatus.MISSED})
            )
            return [task.model_dump(mode="json") for task in tasks]

        return await self._once("missed_tasks", read)

    async def get_all_tasks(self) -> list[dict]:
        """The user's newest TASK_LIST_LIMIT tasks, any status, for progress tracking."""

        async def read() -> list[dict]:
            tasks = await self._tasks.list_for_user(self._user_id)
            return [task.model_dump(mode="json") for task in tasks]

        return await self._once("all_tasks", read)

    async def get_estimated_energy(self) -> float:
        from app.services.energy import HeuristicEnergyEstimator
        from app.services.planning.local_time import user_zone

        # The user's hour, not the server's: energy follows their day.
        hour = datetime.now(user_zone(self._cache.get("profile"))).hour
        return HeuristicEnergyEstimator().score(hour)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)
