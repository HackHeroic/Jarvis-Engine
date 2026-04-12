"""UserModel — lazy facade over Supabase tables.

The Soul of Jarvis. Every module reads from and writes to this.
Queries on first access, caches per-session, invalidates on writes.
"""

import asyncio
from datetime import datetime
from typing import Any, Optional


class UserModel:
    """Lazy facade over Supabase tables. Queries on first access, caches."""

    def __init__(self, user_id: str, db: Any) -> None:
        self._user_id = user_id
        self._db = db
        self._cache: dict[str, Any] = {}

    @property
    def user_id(self) -> str:
        return self._user_id

    async def get_memory_store(self) -> Any:
        return self._cache.get("memory_store")

    def set_memory_store(self, store: Any) -> None:
        self._cache["memory_store"] = store

    async def get_semantic_store(self) -> Any:
        return self._cache.get("semantic_store")

    def set_semantic_store(self, store: Any) -> None:
        self._cache["semantic_store"] = store

    async def get_behavioral_constraints(self) -> list[dict]:
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

    async def get_all_tasks(self) -> list[dict]:
        """All tasks (any status) for progress tracking."""
        if "all_tasks" not in self._cache:
            result = await asyncio.to_thread(
                lambda: self._db.supabase.table("user_tasks")
                .select("*")
                .eq("user_id", self._user_id)
                .execute()
            )
            self._cache["all_tasks"] = result.data
        return self._cache["all_tasks"]

    async def get_active_goals(self) -> list[dict]:
        if "active_goals" not in self._cache:
            result = await asyncio.to_thread(
                lambda: self._db.supabase.table("user_plan_updates")
                .select("*")
                .eq("user_id", self._user_id)
                .execute()
            )
            self._cache["active_goals"] = result.data
        return self._cache["active_goals"]

    async def get_active_draft(self) -> Optional[dict]:
        return self._cache.get("active_draft")

    def set_active_draft(self, draft: Optional[dict]) -> None:
        self._cache["active_draft"] = draft

    async def get_pearl_patterns(self) -> list[dict]:
        if "pearl_patterns" not in self._cache:
            self._cache["pearl_patterns"] = []
        return self._cache["pearl_patterns"]

    def set_pearl_patterns(self, patterns: list[dict]) -> None:
        self._cache["pearl_patterns"] = patterns

    async def get_estimated_energy(self) -> float:
        hour = datetime.now().hour
        if 9 <= hour <= 12:
            return 0.9
        elif 15 <= hour <= 17:
            return 0.85
        elif 13 <= hour <= 14:
            return 0.5
        elif 7 <= hour <= 9:
            return 0.7
        elif 17 < hour <= 21:
            return 0.6
        else:
            return 0.3

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    async def upsert_behavioral_constraint(self, constraint: dict) -> None:
        await asyncio.to_thread(
            lambda: self._db.supabase.table("behavioral_constraints")
            .upsert(constraint)
            .execute()
        )
        self.invalidate("constraints")
