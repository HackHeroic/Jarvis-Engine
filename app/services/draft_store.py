"""Draft persistence layer — Supabase-backed, survives server restarts."""

import uuid

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.domain.entities import OPEN_DRAFT_STATUSES


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from app.db.supabase_py import build_supabase_client

    return build_supabase_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


_UNSET = object()  # sentinel: distinguish "arg not passed" from "explicitly None"

# Statuses a draft is still under review in (the domain's one definition), as the
# column values this Supabase client filters on; every other status is final.
_OPEN_STATUSES = [s.value for s in OPEN_DRAFT_STATUSES]


def is_open_draft(draft: dict | None) -> bool:
    """Whether a draft row's status is an open one (pending or modified).

    Status only: a draft past its ``expires_at`` still reads as open here, and
    accepting it is refused all the same.
    """
    return bool(draft) and draft.get("status") in _OPEN_STATUSES


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
        """The user's newest draft still under review (pending, or edited and so modified)."""
        if not self._supabase:
            return None
        result = self._supabase.table("draft_schedules").select("*").eq("user_id", user_id).in_("status", _OPEN_STATUSES).order("created_at", desc=True).limit(1).execute()
        return result.data[0] if result.data else None

    def accept_draft(self, draft_id: str, user_id: str) -> bool:
        """Mark an open draft accepted. False if absent or already closed."""
        if not self._supabase:
            return False
        result = self._supabase.table("draft_schedules").update({"status": "accepted"}).eq("id", draft_id).eq("user_id", user_id).in_("status", _OPEN_STATUSES).execute()
        return bool(result.data)

    def reject_draft(self, draft_id: str, user_id: str, reason: str | None = None) -> bool:
        """Mark an open draft rejected, keeping the reason. False if absent or already closed."""
        if not self._supabase:
            return False
        result = self._supabase.table("draft_schedules").update({"status": "rejected", "rejection_reason": reason}).eq("id", draft_id).eq("user_id", user_id).in_("status", _OPEN_STATUSES).execute()
        return bool(result.data)

    def replace_tasks(self, draft_id: str, user_id: str, tasks: list) -> dict | None:
        """Overwrite a draft's whole task list. Returns the updated row, or None.

        The unit of revision for a draft is its task array — reordering
        (`POST /rearrange`) and per-task edits both end here. Status becomes
        'modified' so a revised draft is distinguishable from an untouched one.

        Only an open draft is revised. None means absent, or closed: reopening an
        accepted draft as 'modified' would let a second accept write its tasks
        again. ``is_open_draft`` on a fresh read tells the two apart.
        """
        if not self._supabase:
            return None
        if not is_open_draft(self.get_draft(draft_id, user_id)):
            return None  # absent, not this user's, or closed
        result = self._supabase.table("draft_schedules").update({"tasks": tasks, "status": "modified"}).eq("id", draft_id).eq("user_id", user_id).in_("status", _OPEN_STATUSES).execute()
        if not result.data:
            return None  # closed between the read and the write
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
    # What is left of the old in-memory DraftStore's API: get(), delete(),
    # cleanup_expired(). tests/test_draft_store.py pins their contract so nobody
    # mistakes the no-ops for working code. create() and add_component() went with
    # the v1 flow, their only caller.

    def delete(self, draft_id: str, user_id: str = None, **kwargs) -> bool:
        """Legacy alias for delete_draft. Returns False if user_id missing (fail-safe)."""
        if user_id:
            return self.delete_draft(draft_id, user_id)
        return False  # Fail-safe: refuse to delete without user_id

    def get(self, draft_id: str, user_id: str = None, **kwargs):
        """Legacy alias, permanently None. No caller remains — drafts.py now uses get_draft()."""
        return None

    def cleanup_expired(self) -> int:
        """Legacy no-op. Supabase handles expiry via expires_at column."""
        return 0
