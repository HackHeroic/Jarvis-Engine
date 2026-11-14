"""Lightweight mastery tracker — statistical bridge until DKT arrives.

3 signals: quality scores (SM-2), success ratio, reschedule penalty.
Same output interface as future DKT: compute_mastery(user_id, topic) → float.

The reschedule penalty and the streak read the outcome history through the
``TaskEventRepository`` port, never task statuses, keeping only the latest outcome of
each slot (task, scheduled start). Every history read stays inside a time window,
under the store's row cap. A streak's days are dates on the user's clock.
"""

import asyncio

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import app.core.config as config
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain.entities import TaskEvent, TaskEventType
from app.domain.ports import TaskEventRepository
from app.services.task_service import utc_now

# The outcomes a slot can come to: scheduled and rescheduled events are not outcomes.
OUTCOME_TYPES = frozenset({TaskEventType.COMPLETED, TaskEventType.SKIPPED, TaskEventType.MISSED})
# How far back a streak is counted: a longer run reads as this many days.
STREAK_WINDOW_DAYS = 60


def _aware(moment: datetime) -> datetime:
    """Stored instants are UTC: a naive one is read as such."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _latest_outcomes(events: Iterable[TaskEvent]) -> list[TaskEvent]:
    """The last outcome of each slot (task, scheduled start); events that are not outcomes dropped.

    A slot can have more than one outcome (skipped, then completed after all); it
    counts once, as what happened last.
    """
    latest: dict[tuple[str, datetime | None], TaskEvent] = {}
    for event in sorted(events, key=lambda e: _aware(e.occurred_at)):
        if event.event_type in OUTCOME_TYPES:
            start = _aware(event.scheduled_start) if event.scheduled_start is not None else None
            latest[(event.task_id, start)] = event
    return list(latest.values())


def _calculate_sr(user_id: str, goal_id: Optional[str], db) -> float:
    """Success Ratio: completed / total for a goal or all tasks.

    Feeds into: TMT Expectancy, Mastery score, Coach messages.
    Returns 0.5 (neutral prior) when no tasks exist — not inflated confidence.
    """
    try:
        query = db.table("user_tasks").select("status").eq("user_id", user_id)
        if goal_id:
            query = query.eq("goal_id", goal_id)
        result = query.execute()
        tasks = result.data or []
        total = len(tasks)
        if total == 0:
            return 0.5  # neutral prior for cold start
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        return completed / total
    except Exception as e:
        logger.warning(f"SR calculation failed: {e}")
        return 0.5


def _get_avg_quality(user_id: str, goal_id: Optional[str], db) -> float:
    """Average task completion quality (SM-2 scale 0-5) for a goal."""
    try:
        query = db.table("user_tasks").select("quality_score").eq("user_id", user_id).eq("status", "completed")
        if goal_id:
            query = query.eq("goal_id", goal_id)
        result = query.execute()
        tasks = result.data or []
        scores = [t["quality_score"] for t in tasks if t.get("quality_score") is not None]
        if not scores:
            return 2.5  # neutral
        return sum(scores) / len(scores)
    except Exception as e:
        logger.warning(f"Quality avg failed: {e}")
        return 2.5


async def _recent_outcomes(
    user_id: str, task_events: TaskEventRepository | None
) -> list[TaskEvent]:
    """The user's outcome events of the last FOCUS_WINDOW_DAYS, through the repository.

    No repository (no composition root: tests, scripts) is no history. A failed read
    is logged and read as no history too: the penalty is then 0, as before.
    """
    if task_events is None:
        return []
    try:
        return await task_events.list_for_user(
            user_id,
            since=utc_now() - timedelta(days=config.FOCUS_WINDOW_DAYS),
            types=set(OUTCOME_TYPES),
        )
    except Exception as e:
        logger.warning(f"Reschedule count query failed: {type(e).__name__}")
        return []


def _skips(events: list[TaskEvent], goal_id: Optional[str]) -> int:
    """Slots of the goal (all goals when None) whose latest outcome is a skip."""
    return sum(
        1
        for event in _latest_outcomes(events)
        if event.event_type is TaskEventType.SKIPPED
        and (goal_id is None or event.metadata.get("goal_id") == goal_id)
    )


async def _get_reschedule_count(
    user_id: str, goal_id: Optional[str], task_events: TaskEventRepository | None
) -> int:
    """How many of the goal's slots were set aside by a skip (all goals when None).

    Read from the user's outcome events of the last FOCUS_WINDOW_DAYS. A slot counts
    once, as what happened last: skipped, then completed after all, is not a push.
    Slots that passed unattended are not counted (nobody pushed them). An unreadable
    history is logged and counts 0.
    """
    return _skips(await _recent_outcomes(user_id, task_events), goal_id)


def _get_mastery_target(user_id: str, goal_id: Optional[str], db) -> Optional[int]:
    """Get mastery_level_target (1-5) from goal_metadata stored in context_snippet."""
    if not goal_id:
        return None
    try:
        result = db.table("user_plan_updates").select("context_snippet").eq("user_id", user_id).eq("goal_id", goal_id).limit(1).execute()
        rows = result.data or []
        if not rows:
            return None
        snippet = rows[0].get("context_snippet")
        if not snippet:
            return None
        import json
        if isinstance(snippet, str):
            snippet = json.loads(snippet)
        meta = snippet.get("goal_metadata") if isinstance(snippet, dict) else None
        if meta and isinstance(meta, dict):
            return meta.get("mastery_level_target")
    except Exception:
        pass
    return None


def compute_mastery(user_id: str, goal_id: Optional[str], db, *, reschedule_count: int) -> float:
    """Lightweight mastery score (0.0 to 1.0).

    3 data sources:
    1. Quality scores (SM-2 input 0-5) → normalized to 0-1, weight 0.5
    2. Success ratio (completed / total) → weight 0.3
    3. Reschedule penalty (-0.1 per push, capped at -0.3) → weight 0.2

    ``reschedule_count`` comes from the outcome history (``_get_reschedule_count``);
    it is passed in because that history is read through an async repository.
    Returns progress toward mastery_level_target if available.
    Same interface as future DKT — only internals change.
    """
    avg_quality = _get_avg_quality(user_id, goal_id, db)
    sr = _calculate_sr(user_id, goal_id, db)

    raw_mastery = (
        (avg_quality / 5.0) * 0.5
        + sr * 0.3
        + max(0.0, 1.0 - reschedule_count * 0.1) * 0.2
    )

    # mastery_level_target (1-5) sets the bar for 100%
    # target=3 means raw_mastery of 0.6 (3/5) = 100% progress toward YOUR goal
    # target=5 means you need raw_mastery of 1.0 for 100%
    # Formula: progress = raw_mastery / (target / 5.0), capped at 1.0
    # BUT raw_mastery rarely exceeds 0.7 in practice (quality 3.5/5, SR 0.8, 0 reschedules = 0.59)
    # So we scale: how much of the target quality have you achieved?
    target = _get_mastery_target(user_id, goal_id, db)
    if target and target > 0:
        # Target quality threshold: what avg_quality you need for your target
        # target=3 → need avg_quality ~3.0/5; target=5 → need ~5.0/5
        target_quality = target / 5.0  # 0.6 for target=3, 1.0 for target=5
        actual_quality = avg_quality / 5.0
        quality_progress = min(actual_quality / target_quality, 1.0) if target_quality > 0 else 1.0
        # Blend: quality progress toward target (0.5) + SR (0.3) + reschedule (0.2)
        return (
            quality_progress * 0.5
            + sr * 0.3
            + max(0.0, 1.0 - reschedule_count * 0.1) * 0.2
        )
    return raw_mastery


def _goal_ids(user_id: str, db) -> list[str]:
    result = db.table("user_tasks").select("goal_id").eq("user_id", user_id).execute()
    return list({t["goal_id"] for t in result.data or [] if t.get("goal_id")})


async def get_mastery_summary(
    user_id: str, db, task_events: TaskEventRepository | None
) -> dict[str, float]:
    """Mastery scores for all active goals. Returns {goal_id: mastery_score}.

    The outcome history is read once (through ``task_events``) for every goal's
    reschedule penalty; the task-table reads run off the event loop.
    """
    try:
        goal_ids = await asyncio.to_thread(_goal_ids, user_id, db)
        events = await _recent_outcomes(user_id, task_events) if goal_ids else []
        summary = {}
        for gid in goal_ids:
            summary[gid] = await asyncio.to_thread(
                compute_mastery, user_id, gid, db, reschedule_count=_skips(events, gid)
            )
        return summary
    except Exception as e:
        logger.warning(f"Mastery summary failed: {type(e).__name__}")
        return {}


def compute_quality_trend(user_id: str, db) -> str:
    """Compare recent 5 completions quality vs previous 5. Returns improving/stable/declining."""
    try:
        result = (
            db.table("user_tasks")
            .select("quality_score, completed_at")
            .eq("user_id", user_id)
            .eq("status", "completed")
            .not_.is_("quality_score", "null")
            .order("completed_at", desc=True)
            .limit(10)
            .execute()
        )
        tasks = result.data or []
        if len(tasks) < 4:
            return "stable"
        recent = tasks[:5]
        older = tasks[5:10]
        if not older:
            return "stable"
        recent_avg = sum(t["quality_score"] for t in recent) / len(recent)
        older_avg = sum(t["quality_score"] for t in older) / len(older)
        diff = recent_avg - older_avg
        if diff > 0.5:
            return "improving"
        elif diff < -0.5:
            return "declining"
        return "stable"
    except Exception:
        return "stable"


def compute_streak_from_events(events: list[TaskEvent], zone: ZoneInfo, today: date) -> int:
    """Consecutive local dates, ending ``today``, with at least one completion.

    ``events`` is the outcome history; each slot counts as its latest outcome, and a
    completion's day is the date it happened on in ``zone`` (00:30 IST is that IST day,
    not the previous UTC one). No completion today is a streak of 0.
    """
    days = {
        _aware(event.occurred_at).astimezone(zone).date()
        for event in _latest_outcomes(events)
        if event.event_type is TaskEventType.COMPLETED
    }
    streak = 0
    day = today
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak
