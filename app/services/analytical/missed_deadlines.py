"""Anti-Guilt: detect tasks past their deadline and set them aside.

Runs at the start of every PLAN_DAY flow. Past-deadline + status=pending tasks
are marked as `missed`, which takes them out of planning (fusion reads only
pending tasks) without deleting them, and surfaced in the synthesis with
anti-guilt framing — never shame language.
"""

from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.services.planning.local_time import end_of_local_day, user_zone


def _parse_deadline(value: Any, zone: ZoneInfo) -> Optional[datetime]:
    """Parse a deadline into an aware datetime, or None.

    Deadlines are the user's: a plain date passes at the end of that day on their
    clock (23:59:59 in ``zone``, not in UTC, which for India is 05:29 the next
    morning), and a timestamp without an offset is their wall time. Aware always,
    or ``dl < now`` raises TypeError and silently breaks the scan.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=zone)
    try:
        s = str(value).strip()
        if not s:
            return None
        if "T" not in s and len(s) == 10:
            return end_of_local_day(date.fromisoformat(s), zone)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=zone)
    except Exception:
        return None


async def detect_and_mark_missed(
    user_id: str, supabase: Any, zone: Optional[ZoneInfo] = None
) -> list[dict]:
    """Find pending tasks whose deadline has passed; mark them `missed`.

    ``zone`` is the user's (default ``user_zone()``): it decides when a date-only
    deadline has passed. Returns the list of (task_id, title, deadline) tuples
    that were just marked. Empty list if none. Tolerates supabase=None (returns []).
    """
    if not supabase:
        return []
    zone = zone or user_zone()
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
        dl = _parse_deadline(row.get("deadline_hint"), zone)
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

    logger.info(f"Set aside {len(overdue)} missed tasks for user {user_id}")
    return overdue


def build_anti_guilt_message(overdue: list[dict]) -> str:
    """Synthesize a short, blame-free framing of missed tasks.

    Anti-Guilt principle: missed deadlines are scope problems, not moral failures.
    The framing says only what is true: ``detect_and_mark_missed`` marked these
    tasks ``missed``, so they are set aside and stay out of this plan (it fuses
    pending tasks only). Nothing is deleted, and the user can ask for them back.
    """
    if not overdue:
        return ""
    n = len(overdue)
    if n == 1:
        title = overdue[0]["title"] or "a task"
        return (
            f"Sir, '{title}' slipped past its deadline, so I've set it aside rather than "
            f"cram it in — nothing is lost. Say the word and I'll work it into this plan."
        )
    titles = ", ".join(f"'{o['title']}'" for o in overdue[:3] if o.get("title"))
    extra = f" and {n - 3} more" if n > 3 else ""
    return (
        f"Sir, {n} tasks have slipped past their deadlines ({titles}{extra}), so I've set "
        f"them aside rather than cram them in — nothing is lost. Say the word and I'll "
        f"work them into this plan."
    )
