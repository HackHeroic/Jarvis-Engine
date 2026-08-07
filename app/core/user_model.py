"""UserModel — lazy facade over Supabase tables.

The Soul of Jarvis. Every module reads from and writes to this.
Queries on first access, caches per-session, invalidates on writes.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

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

    async def get_memory_store(self) -> Any:
        return self._cache.get("memory_store")

    def set_memory_store(self, store: Any) -> None:
        self._cache["memory_store"] = store

    async def get_semantic_store(self) -> Any:
        return self._cache.get("semantic_store")

    def set_semantic_store(self, store: Any) -> None:
        self._cache["semantic_store"] = store

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

    async def get_active_goals(self) -> list[dict]:
        async with self._get_lock("active_goals"):
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

    async def build_chat_context(self, limit_memories: int = 5, limit_tasks: int = 8) -> str:
        """Build a compact text block for injection into the LLM system prompt.

        Includes top-N SM-2 memories, pending tasks, active goals, current energy.
        Used by conversation module so Jarvis 'understands you' across chats.
        """
        lines: list[str] = []

        try:
            mem_store = await self.get_memory_store()
            if mem_store is not None:
                # Recall top-K memories by SM-2 score (handles list/coroutine return shapes)
                recall_fn = getattr(mem_store, "recall_for_user", None) or getattr(mem_store, "top_k", None)
                if recall_fn:
                    raw = recall_fn(self._user_id, limit_memories) if not asyncio.iscoroutinefunction(recall_fn) \
                        else await recall_fn(self._user_id, limit_memories)
                    for m in (raw or [])[:limit_memories]:
                        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
                        mtype = m.get("memory_type") if isinstance(m, dict) else getattr(m, "memory_type", "")
                        if content:
                            lines.append(f"- [{mtype or 'note'}] {content}")
        except Exception as _e:
            _log.warning(f"build_chat_context section failed for user {self._user_id}: {_e}")

        try:
            tasks = await self.get_pending_tasks()
            if tasks:
                lines.append("")
                lines.append(f"Pending tasks ({len(tasks)} total, showing {min(limit_tasks, len(tasks))}):")
                for t in tasks[:limit_tasks]:
                    title = t.get("title", "untitled")
                    deadline = t.get("deadline_hint") or t.get("deadline_date")
                    suffix = f" — due {deadline}" if deadline else ""
                    lines.append(f"  • {title}{suffix}")
        except Exception as _e:
            _log.warning(f"build_chat_context section failed for user {self._user_id}: {_e}")

        try:
            goals = await self.get_active_goals()
            if goals:
                lines.append("")
                lines.append("Active goals:")
                for g in goals[:5]:
                    desc = g.get("goal_description") or g.get("title") or g.get("goal_id")
                    if desc:
                        lines.append(f"  • {desc}")
        except Exception as _e:
            _log.warning(f"build_chat_context section failed for user {self._user_id}: {_e}")

        try:
            draft = await self.get_active_draft()
            if draft:
                lines.append("")
                lines.append("There is an active schedule draft awaiting review.")
        except Exception as _e:
            _log.warning(f"build_chat_context section failed for user {self._user_id}: {_e}")

        try:
            energy = await self.get_estimated_energy()
            lines.append("")
            lines.append(f"Estimated cognitive energy right now: {energy:.2f} (0=low, 1=peak)")
        except Exception as _e:
            _log.warning(f"build_chat_context section failed for user {self._user_id}: {_e}")

        return "\n".join(lines).strip()
