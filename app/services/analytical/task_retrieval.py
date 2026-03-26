"""Global task retrieval for multi-goal fusion."""

from typing import Any

from app.api.v1.endpoints.reasoning import TaskChunk
from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from supabase import create_client


def _get_supabase(client: Any = None):
    if client is not None:
        return client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_all_pending_tasks(
    user_id: str,
    supabase_client: Any = None,
) -> list[TaskChunk]:
    """Fetch all pending tasks from user_tasks, hydrating to TaskChunk format.

    Returns chunks with prefixed task_ids (goal_id_task_id). Dependencies are
    already prefixed from the last fusion persist. Deduplicates by task_id
    (keeps latest per task_id).
    """
    supabase = _get_supabase(supabase_client)
    if not supabase or not user_id:
        return []

    try:
        # Note: duration_minutes may not exist in user_tasks; we default to 25
        result = (
            supabase.table("user_tasks")
            .select("task_id, title, goal_id, duration_minutes, difficulty_weight, dependencies, deadline_hint")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        rows = result.data or []
    except Exception as e:
        print(f"[task_retrieval] get_all_pending_tasks failed: {e}")
        return []

    seen: set[str] = set()
    chunks: list[TaskChunk] = []

    # Keep latest per task_id (rows ordered by created_at desc)
    for r in rows:
        task_id = r.get("task_id")
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)

        duration = r.get("duration_minutes") or 25
        duration = max(1, min(25, int(duration)))
        difficulty = r.get("difficulty_weight") or 0.5
        difficulty = max(0.0, min(1.0, float(difficulty)))

        deps = r.get("dependencies")
        if deps is None:
            deps = []
        elif isinstance(deps, list):
            deps = [str(x) for x in deps if x]
        else:
            deps = []

        try:
            chunk = TaskChunk(
                task_id=task_id,
                title=r.get("title") or "(untitled)",
                duration_minutes=duration,
                difficulty_weight=difficulty,
                dependencies=deps,
                completion_criteria="(from prior plan)",
                implementation_intention=None,
                deadline_hint=r.get("deadline_hint"),
            )
            chunks.append(chunk)
        except Exception as e:
            print(f"[task_retrieval] Skip invalid chunk {task_id}: {e}")

    return chunks
