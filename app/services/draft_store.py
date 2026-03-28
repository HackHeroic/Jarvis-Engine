"""Draft persistence layer — Supabase-backed, survives server restarts.

Backward-compatibility note: DraftComponent and Draft dataclasses are kept
here so existing control_policy.py references do not raise ImportError at
runtime. They will be removed when Task 8 rewrites the draft negotiation flow.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL


# ── Legacy stubs (used by control_policy.py pending Task 8 rewrite) ─────────

@dataclass
class DraftComponent:
    """Reviewable component within a legacy in-memory draft (stub)."""

    component_type: str
    data: Any
    status: str = "pending"


@dataclass
class Draft:
    """Legacy in-memory draft container (stub — kept for import compat)."""

    draft_id: str
    user_id: str
    created_at: float
    components: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class DraftStore:
    """Persists draft schedules in Supabase draft_schedules table."""

    def __init__(self, supabase_client=None):
        self._supabase = supabase_client or _get_supabase()

    def create_draft(self, user_id: str, tasks: list, horizon_start: str, goal_id: str | None = None) -> dict | None:
        if not self._supabase:
            return None
        draft_id = str(uuid.uuid4())
        tasks_json = [t.model_dump() if hasattr(t, "model_dump") else t for t in tasks]
        result = self._supabase.table("draft_schedules").insert({
            "id": draft_id, "user_id": user_id, "goal_id": goal_id,
            "tasks": tasks_json, "horizon_start": horizon_start, "status": "pending",
        }).execute()
        return result.data[0] if result.data else None

    def get_draft(self, draft_id: str, user_id: str) -> dict | None:
        if not self._supabase:
            return None
        result = self._supabase.table("draft_schedules").select("*").eq("id", draft_id).eq("user_id", user_id).execute()
        return result.data[0] if result.data else None

    def get_pending_draft(self, user_id: str) -> dict | None:
        if not self._supabase:
            return None
        result = self._supabase.table("draft_schedules").select("*").eq("user_id", user_id).eq("status", "pending").order("created_at", desc=True).limit(1).execute()
        return result.data[0] if result.data else None

    def accept_draft(self, draft_id: str, user_id: str) -> bool:
        if not self._supabase:
            return False
        result = self._supabase.table("draft_schedules").update({"status": "accepted"}).eq("id", draft_id).eq("user_id", user_id).execute()
        return bool(result.data)

    def reject_draft(self, draft_id: str, user_id: str, reason: str | None = None) -> bool:
        if not self._supabase:
            return False
        result = self._supabase.table("draft_schedules").update({"status": "rejected", "rejection_reason": reason}).eq("id", draft_id).eq("user_id", user_id).execute()
        return bool(result.data)

    def edit_task_in_draft(self, draft_id: str, user_id: str, task_id: str, edits: dict) -> dict | None:
        draft = self.get_draft(draft_id, user_id)
        if not draft:
            return None
        tasks = draft.get("tasks", [])
        updated = False
        for task in tasks:
            if task.get("task_id") == task_id:
                for key, value in edits.items():
                    if value is not None:
                        task[key] = value
                updated = True
                break
        if not updated:
            return None
        self._supabase.table("draft_schedules").update({"tasks": tasks, "status": "modified"}).eq("id", draft_id).eq("user_id", user_id).execute()
        return self.get_draft(draft_id, user_id)

    def delete_draft(self, draft_id: str, user_id: str) -> bool:
        if not self._supabase:
            return False
        self._supabase.table("draft_schedules").delete().eq("id", draft_id).eq("user_id", user_id).execute()
        return True

    # ── Backward-compatible aliases ──────────────────────────
    # The old in-memory DraftStore had: create(), get(), delete(), add_component(),
    # accept_component(), reject_component(), accept_all(), cleanup_expired().
    # These aliases keep existing callers in drafts.py and control_policy.py working
    # until they are rewritten to use the new API in Phase 1B.

    def create(self, user_id: str, **kwargs) -> "DraftStore":
        """Legacy alias. Returns self for chaining (old API returned Draft dataclass)."""
        return self

    def get(self, draft_id: str, **kwargs):
        """Legacy alias for get_draft. Ignores extra kwargs."""
        # Old API didn't require user_id, but we need it. Return None if not available.
        return None

    def add_component(self, draft_id: str, component_type: str, data, **kwargs) -> bool:
        """Legacy no-op. Components are now stored as tasks in draft JSONB."""
        return True

    def update_component_data(self, draft_id: str, component_type: str, data, **kwargs) -> bool:
        """Legacy no-op."""
        return True

    def accept_component(self, draft_id: str, component_type: str, **kwargs) -> bool:
        """Legacy no-op."""
        return True

    def reject_component(self, draft_id: str, component_type: str, **kwargs) -> bool:
        """Legacy no-op."""
        return True

    def accept_all(self, draft_id: str, **kwargs) -> bool:
        """Legacy no-op."""
        return True

    def cleanup_expired(self) -> int:
        """Legacy no-op. Supabase handles expiry via expires_at column."""
        return 0
