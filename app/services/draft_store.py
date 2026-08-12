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


_UNSET = object()  # sentinel: distinguish "arg not passed" from "explicitly None"


class DraftStore:
    """Persists draft schedules in Supabase draft_schedules table."""

    def __init__(self, supabase_client=_UNSET, ttl_seconds=None):
        # Explicitly-passed None (degraded mode / tests) must NOT silently
        # build a live client from env — that defeats startup degradation.
        self._supabase = _get_supabase() if supabase_client is _UNSET else supabase_client

    def create_draft(
        self, user_id: str, tasks: list, horizon_start: str,
        goal_id: str | None = None, schedule: dict | None = None,
    ) -> dict | None:
        if not self._supabase:
            return None
        draft_id = str(uuid.uuid4())
        tasks_json = [t.model_dump() if hasattr(t, "model_dump") else t for t in tasks]
        result = self._supabase.table("draft_schedules").insert({
            "id": draft_id, "user_id": user_id, "goal_id": goal_id,
            "tasks": tasks_json, "horizon_start": horizon_start, "status": "pending",
            # Solved task_id -> {start_min, end_min}; accept turns these into
            # wall-clock scheduled_start/end. Without it tasks persist timeless
            # and the frontend piles them all at 08:00.
            "schedule": schedule,
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

    def replace_tasks(self, draft_id: str, user_id: str, tasks: list) -> dict | None:
        """Overwrite a draft's whole task list. Returns the updated row, or None.

        The unit of revision for a draft is its task array — reordering
        (`POST /rearrange`) and per-task edits both end here. Status becomes
        'modified' so a revised draft is distinguishable from an untouched one.
        """
        if not self._supabase:
            return None
        if not self.get_draft(draft_id, user_id):
            return None  # absent, or not this user's — same answer either way
        self._supabase.table("draft_schedules").update({"tasks": tasks, "status": "modified"}).eq("id", draft_id).eq("user_id", user_id).execute()
        return self.get_draft(draft_id, user_id)

    def edit_task_in_draft(self, draft_id: str, user_id: str, task_id: str, edits: dict) -> dict | None:
        """Edit a single task within a draft by task_id.

        Note: This method has a read-modify-write pattern that is not atomic.
        Concurrent edits to different tasks in the same draft may cause lost updates.
        For Phase 1, single-user usage makes this acceptable. Phase 2 should use
        a PostgreSQL jsonb_set RPC for atomic updates.
        """
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
        return self.replace_tasks(draft_id, user_id, tasks)

    def delete_draft(self, draft_id: str, user_id: str) -> bool:
        if not self._supabase:
            return False
        self._supabase.table("draft_schedules").delete().eq("id", draft_id).eq("user_id", user_id).execute()
        return True

    # ── Backward-compatible aliases ──────────────────────────
    # The old in-memory DraftStore had: create(), get(), delete(), add_component(),
    # accept_component(), reject_component(), accept_all(), cleanup_expired().
    # drafts.py no longer calls any of them (Task 10 rewrote it onto the real
    # API above); control_policy.py's v1 flow still calls add_component(), so
    # the shims stay until that flow is retired. tests/test_draft_store.py pins
    # their no-op contract so nobody mistakes them for working code.

    def create(self, user_id: str, **kwargs) -> "Draft":
        """Legacy alias. Returns a Draft stub with a generated draft_id."""
        import time
        return Draft(draft_id=str(uuid.uuid4()), user_id=user_id, created_at=time.time(), metadata=kwargs.get("metadata", {}))

    def delete(self, draft_id: str, user_id: str = None, **kwargs) -> bool:
        """Legacy alias for delete_draft. Returns False if user_id missing (fail-safe)."""
        if user_id:
            return self.delete_draft(draft_id, user_id)
        return False  # Fail-safe: refuse to delete without user_id

    def get(self, draft_id: str, user_id: str = None, **kwargs):
        """Legacy alias, permanently None. No caller remains — drafts.py now uses get_draft()."""
        return None

    def add_component(self, draft_id: str, user_id: str = None, component_type: str = None, data=None, **kwargs) -> bool:
        """Legacy no-op. Still called by control_policy.py:897,907,1260,1354 (v1 flow)."""
        return True

    def update_component_data(self, draft_id: str, user_id: str = None, component_type: str = None, data=None, **kwargs) -> bool:
        """Legacy no-op. No caller remains — drafts.py now uses replace_tasks()."""
        return True

    def accept_component(self, draft_id: str, user_id: str = None, component_type: str = None, **kwargs) -> bool:
        """Legacy no-op. No caller remains — drafts.py now uses accept_draft()."""
        return True

    def reject_component(self, draft_id: str, user_id: str = None, component_type: str = None, **kwargs) -> bool:
        """Legacy no-op. No caller remains — drafts.py now uses reject_draft()."""
        return True

    def accept_all(self, draft_id: str, **kwargs) -> bool:
        """Legacy no-op."""
        return True

    def cleanup_expired(self) -> int:
        """Legacy no-op. Supabase handles expiry via expires_at column."""
        return 0
