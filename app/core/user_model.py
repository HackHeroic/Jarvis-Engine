"""UserModel — lazy facade over Supabase tables.

The Soul of Jarvis. Every module reads from and writes to this.
Queries on first access, caches per-session, invalidates on writes.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

_log = logging.getLogger(__name__)


class UserModel:
    """Lazy facade over Supabase tables. Queries on first access, caches."""

    def __init__(self, user_id: str, db: Any) -> None:
        self._user_id = user_id
        self._db = db
        self._cache: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @property
    def user_id(self) -> str:
        return self._user_id

    async def get_profile(self) -> Any:
        """The user's ``UserProfile``, or None when there is none or it cannot be read.

        Read once per facade (one turn) and off the event loop. The planner takes
        the user's timezone from it.
        """
        async with self._get_lock("profile"):
            if "profile" not in self._cache:
                from app.domain.entities import UserProfile

                profile = None
                supabase = getattr(self._db, "supabase", None)
                if supabase is not None:
                    try:
                        result = await asyncio.to_thread(
                            lambda: supabase.table("user_profiles")
                            .select("*")
                            .eq("user_id", self._user_id)
                            .limit(1)
                            .execute()
                        )
                        rows = result.data or []
                        profile = UserProfile.model_validate(rows[0]) if rows else None
                    except Exception as exc:  # no profile is a default, never a failed turn
                        _log.warning("Profile read failed for %s: %r", self._user_id, exc)
                self._cache["profile"] = profile
            return self._cache["profile"]

    async def get_behavioral_constraints(self) -> list[dict]:
        async with self._get_lock("constraints"):
            if "constraints" not in self._cache:
                result = await asyncio.to_thread(
                    lambda: self._db.supabase.table("behavioral_constraints")
                    .select("*")
                    .eq("user_id", self._user_id)
                    .execute()
                )
                self._cache["constraints"] = result.data
            return self._cache["constraints"]

    async def get_pending_tasks(self) -> list[dict]:
        async with self._get_lock("pending_tasks"):
            if "pending_tasks" not in self._cache:
                result = await asyncio.to_thread(
                    lambda: self._db.supabase.table("user_tasks")
                    .select("*")
                    .eq("user_id", self._user_id)
                    .eq("status", "pending")
                    .execute()
                )
                self._cache["pending_tasks"] = result.data
            return self._cache["pending_tasks"]

    async def get_missed_tasks(self) -> list[dict]:
        """The user's set-aside (missed) tasks, newest first, at most TASK_LIST_LIMIT."""
        from app.domain.ports import TASK_LIST_LIMIT

        async with self._get_lock("missed_tasks"):
            if "missed_tasks" not in self._cache:
                result = await asyncio.to_thread(
                    lambda: self._db.supabase.table("user_tasks")
                    .select("*")
                    .eq("user_id", self._user_id)
                    .eq("status", "missed")
                    .order("created_at", desc=True)
                    .limit(TASK_LIST_LIMIT)
                    .execute()
                )
                self._cache["missed_tasks"] = result.data
            return self._cache["missed_tasks"]

    async def get_all_tasks(self) -> list[dict]:
        """All tasks (any status) for progress tracking."""
        async with self._get_lock("all_tasks"):
            if "all_tasks" not in self._cache:
                result = await asyncio.to_thread(
                    lambda: self._db.supabase.table("user_tasks")
                    .select("*")
                    .eq("user_id", self._user_id)
                    .execute()
                )
                self._cache["all_tasks"] = result.data
            return self._cache["all_tasks"]

    async def get_estimated_energy(self) -> float:
        from app.services.energy import HeuristicEnergyEstimator
        from app.services.planning.local_time import user_zone

        # The user's hour, not the server's: energy follows their day.
        hour = datetime.now(user_zone(self._cache.get("profile"))).hour
        return HeuristicEnergyEstimator().score(hour)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)
