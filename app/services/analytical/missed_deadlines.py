"""Anti-Guilt auto-reschedule: detect tasks past their deadline and push them forward.

Runs at the start of every PLAN_DAY flow. Past-deadline + status=pending tasks
are marked as `missed`, queued for re-decomposition, and surfaced in the
synthesis with anti-guilt framing — never shame language.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.jarvis_logger import JARVIS_LOGGER as logger


def _parse_deadline(value: Any) -> Optional[datetime]:
    """Parse a deadline; ALWAYS returns tz-aware UTC datetime or None.

    Naive datetimes (older Supabase rows) are coerced to UTC. Without this,
    `dl < now()` raises TypeError and silently breaks the missed-deadline scan.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).strip()
        if not s:
            return None
        # Allow plain dates and ISO timestamps
        if "T" not in s and len(s) == 10:
            s = f"{s}T23:59:59+00:00"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def detect_and_mark_missed(user_id: str, supabase: Any) -> list[dict]:
    """Find pending tasks whose deadline has passed; mark them `missed`.

    Returns the list of (task_id, title, deadline) tuples that were just marked.
    Empty list if none. Tolerates supabase=None (returns []).
    """
    if not supabase:
        return []
    now = datetime.now(timezone.utc)
    try:
        result = (
            supabase.table("user_tasks")
            .select("id, task_id, title, deadline_hint, status")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .execute()
        )
        rows = result.data or []
    except Exception as e:
        logger.warning(f"missed-deadline scan failed: {e}")
        return []

    overdue: list[dict] = []
    for row in rows:
        dl = _parse_deadline(row.get("deadline_hint"))
        if dl and dl < now:
            overdue.append({
                "id": row.get("id"),
                "task_id": row.get("task_id"),
                "title": row.get("title"),
                "deadline": dl.isoformat(),
            })

    if not overdue:
        return []

    # Mark missed in DB
    for o in overdue:
        try:
            supabase.table("user_tasks").update(
                {"status": "missed"}
            ).eq("id", o["id"]).eq("user_id", user_id).execute()
        except Exception as e:
            logger.warning(f"Failed to mark task {o['task_id']} missed: {e}")

    logger.info(f"Auto-rescheduled {len(overdue)} missed tasks for user {user_id}")
    return overdue


def build_anti_guilt_message(overdue: list[dict]) -> str:
    """Synthesize a short, blame-free framing of missed tasks.

    Anti-Guilt principle: missed deadlines are scope problems, not moral failures.
    We acknowledge the change, propose a path forward, never use 'you failed' language.
    """
    if not overdue:
        return ""
    n = len(overdue)
    if n == 1:
        title = overdue[0]["title"] or "a task"
        return (
            f"Sir, '{title}' slipped past its deadline. "
            f"I've recalibrated the plan — this is a scope adjustment, not a setback."
        )
    titles = ", ".join(f"'{o['title']}'" for o in overdue[:3] if o.get("title"))
    extra = f" and {n - 3} more" if n > 3 else ""
    return (
        f"Sir, {n} tasks have slipped past their deadlines ({titles}{extra}). "
        f"I've folded them back into the planning queue. Scope, not failure."
    )
