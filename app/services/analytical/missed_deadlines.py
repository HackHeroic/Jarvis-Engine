"""Anti-Guilt: detect tasks past their deadline and set them aside.

Runs at the start of every PLAN_DAY flow. Past-deadline + status=pending tasks
are marked as `missed`, which takes them out of planning (fusion reads only
pending tasks) without deleting them, and surfaced in the synthesis with
anti-guilt framing — never shame language.
"""

import asyncio
import re
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from zoneinfo import ZoneInfo

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain import errors
from app.domain.entities import TaskStatus
from app.services.planning.local_time import end_of_local_day, user_zone

if TYPE_CHECKING:
    from app.services.task_service import TaskService


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
    user_id: str,
    supabase: Any,
    zone: Optional[ZoneInfo] = None,
    *,
    task_service: "TaskService | None" = None,
) -> list[dict]:
    """Find pending tasks whose deadline has passed; mark them `missed`.

    ``zone`` is the user's (default ``user_zone()``): it decides when a date-only
    deadline has passed. With ``task_service`` each task is set aside through
    ``TaskService.mark_deadline_missed``, which also records a ``deadline_missed``
    event; a task that changed meanwhile is left alone and not returned. Without
    one (the deprecated v1 flow) the status is written directly. Returns the list
    of (task_id, title, deadline) tuples that were just marked. Empty list if none.
    Tolerates supabase=None (returns []).
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

    if task_service is not None:
        marked: list[dict] = []
        for o in overdue:
            try:
                if await task_service.mark_deadline_missed(user_id, o["task_id"]) is not None:
                    marked.append(o)
            except Exception as e:  # one task's failure never stops the others
                logger.warning(f"Could not set task {o['task_id']} aside: {type(e).__name__}")
        overdue = marked
    else:
        # Mark missed in DB
        for o in overdue:
            try:
                supabase.table("user_tasks").update(
                    {"status": "missed"}
                ).eq("id", o["id"]).eq("user_id", user_id).execute()
            except Exception as e:
                logger.warning(f"Failed to mark task {o['task_id']} missed: {e}")

    if overdue:
        logger.info(f"Set aside {len(overdue)} missed tasks for user {user_id}")
    return overdue


# "Say the word and I'll work it into this plan": the word, deterministically (no
# model call). The message must name the set-aside tasks and ask for them back; a
# negation anywhere ("don't add the missed ones") is a no. "Missed" has to name the
# tasks ("the missed tasks", "the ones I missed", "what I missed"), not be a verb
# about something else: "I missed the lecture, add a review session" is a new task.
_SA = r"(missed|set[- ]aside)"
_WHO = r"(i|i've|i have|you|you've|you have)"
_SET_ASIDE_RE = re.compile(
    rf"\b{_SA}\s+(tasks?|ones?|items?|work)\b"
    rf"|\b(the|my|those|these|all)\s+{_SA}\b"
    rf"|\b(what|tasks?|ones?|items?)\s+{_WHO}\s+{_SA}\b",
    re.IGNORECASE,
)
_BRING_BACK_RE = re.compile(
    r"\b(add|include|reschedule|bring\b.*\bback|put\b.*\bback|restore"
    r"|work (them|it) in(to)?)\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"\b(don'?t|do not|never|no need|skip)\b", re.IGNORECASE)


def asks_to_bring_back_missed(message: str | None) -> bool:
    """Does this message ask for the set-aside (missed) tasks back?"""
    text = message or ""
    return bool(
        _SET_ASIDE_RE.search(text) and _BRING_BACK_RE.search(text) and not _NEGATION_RE.search(text)
    )


async def _clear_passed_deadline(user_id: str, task_id: str, supabase: Any) -> None:
    """Drop a restored task's passed deadline (only while it is still pending)."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("user_tasks")
            .update({"deadline_hint": None})
            .eq("user_id", user_id)
            .eq("task_id", task_id)
            .eq("status", TaskStatus.PENDING.value)
            .execute()
        )
    except Exception as e:
        # Still back in this plan; a later scan may set it aside again.
        logger.warning(f"Could not clear the passed deadline of task {task_id}: {e!r}")


async def restore_missed(user_id: str, supabase: Any) -> list[dict]:
    """Flip the user's ``missed`` tasks back to ``pending``; return the ones flipped.

    Each move goes through ``TaskRepository.update_status`` as a compare-and-set
    from MISSED, so a task that changed meanwhile is left alone. Bringing a task
    back is durable: its passed ``deadline_hint`` is cleared, or the next scan
    (``detect_and_mark_missed``) would set it straight aside again. Returns
    ``[{"task_id", "title"}]`` for the tasks that are pending again.
    """
    if not supabase:
        return []
    from app.services.draft_actions import task_repository

    tasks = task_repository(supabase)
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("user_tasks")
            .select("task_id, title")
            .eq("user_id", user_id)
            .eq("status", TaskStatus.MISSED.value)
            .execute()
        )
        rows = result.data or []
    except Exception as e:
        logger.warning(f"Listing missed tasks failed: {e!r}")
        return []

    restored: list[dict] = []
    for row in rows:
        try:
            await tasks.update_status(
                user_id, row["task_id"], TaskStatus.PENDING, expected=TaskStatus.MISSED
            )
        except errors.JarvisError as e:
            logger.warning(f"Could not bring task {row.get('task_id')} back: {e.code}")
            continue
        await _clear_passed_deadline(user_id, row["task_id"], supabase)
        restored.append({"task_id": row["task_id"], "title": row.get("title")})
    if restored:
        logger.info(f"Brought {len(restored)} missed tasks back for user {user_id}")
    return restored


def build_restored_message(restored: list[dict]) -> str:
    """Say which set-aside tasks are back in the plan. Only what happened; no blame."""
    titles = [f"'{r['title']}'" for r in restored[:3] if r.get("title")]
    extra = f" and {len(restored) - 3} more" if len(restored) > 3 else ""
    named = ", ".join(titles) + extra if titles else f"{len(restored)} set-aside tasks"
    return f"Sir, as you asked, {named} {'is' if len(restored) == 1 else 'are'} back in this plan."


def build_anti_guilt_message(overdue: list[dict]) -> str:
    """Synthesize a short, blame-free framing of missed tasks.

    Anti-Guilt principle: missed deadlines are scope problems, not moral failures.
    The framing says only what is true: ``detect_and_mark_missed`` marked these
    tasks ``missed``, so they are set aside and stay out of this plan (it fuses
    pending tasks only). Nothing is deleted, and asking for them back in a
    planning turn (``asks_to_bring_back_missed``) makes them pending again.
    """
    if not overdue:
        return ""
    n = len(overdue)
    if n == 1:
        title = overdue[0]["title"] or "a task"
        return (
            f"Sir, '{title}' slipped past its deadline, so I've set it aside rather than "
            f"cram it in — nothing is lost. Say the word (\"add the missed tasks\") and "
            f"I'll work it into this plan."
        )
    titles = ", ".join(f"'{o['title']}'" for o in overdue[:3] if o.get("title"))
    extra = f" and {n - 3} more" if n > 3 else ""
    return (
        f"Sir, {n} tasks have slipped past their deadlines ({titles}{extra}), so I've set "
        f"them aside rather than cram them in — nothing is lost. Say the word (\"add the "
        f"missed tasks\") and I'll work them into this plan."
    )
