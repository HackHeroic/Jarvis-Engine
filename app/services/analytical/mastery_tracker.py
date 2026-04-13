"""Lightweight mastery tracker — statistical bridge until DKT arrives.

3 signals: quality scores (SM-2), success ratio, reschedule penalty.
Same output interface as future DKT: compute_mastery(user_id, topic) → float.
"""

import asyncio
from typing import Optional

from app.core.jarvis_logger import JARVIS_LOGGER as logger


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


def _get_reschedule_count(user_id: str, goal_id: Optional[str], db) -> int:
    """Total reschedule count for tasks in a goal."""
    try:
        query = db.table("user_tasks").select("reschedule_count").eq("user_id", user_id)
        if goal_id:
            query = query.eq("goal_id", goal_id)
        result = query.execute()
        tasks = result.data or []
        return sum(t.get("reschedule_count", 0) for t in tasks)
    except Exception as e:
        logger.warning(f"Reschedule count query failed: {e}")
        return 0


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


def compute_mastery(user_id: str, goal_id: Optional[str], db) -> float:
    """Lightweight mastery score (0.0 to 1.0).

    3 data sources:
    1. Quality scores (SM-2 input 0-5) → normalized to 0-1, weight 0.5
    2. Success ratio (completed / total) → weight 0.3
    3. Reschedule penalty (-0.1 per push, capped at -0.3) → weight 0.2

    Returns progress toward mastery_level_target if available.
    Same interface as future DKT — only internals change.
    """
    avg_quality = _get_avg_quality(user_id, goal_id, db)
    sr = _calculate_sr(user_id, goal_id, db)
    reschedule_count = _get_reschedule_count(user_id, goal_id, db)

    raw_mastery = (
        (avg_quality / 5.0) * 0.5
        + sr * 0.3
        + max(0.0, 1.0 - reschedule_count * 0.1) * 0.2
    )

    # mastery_level_target (1-5) sets the bar — higher target = harder to reach 100%
    # target=5 means raw_mastery IS the score (full scale)
    # target=3 means you need raw_mastery of 0.6 to hit 100% — NO, that inflates scores
    # Correct: target just labels ambition. Raw mastery is the actual score.
    # Don't normalize by target — it distorts the signal.
    return raw_mastery


def get_mastery_summary(user_id: str, db) -> dict[str, float]:
    """Mastery scores for all active goals. Returns {goal_id: mastery_score}."""
    try:
        result = db.table("user_tasks").select("goal_id").eq("user_id", user_id).neq("status", "archived").execute()
        tasks = result.data or []
        goal_ids = list({t["goal_id"] for t in tasks if t.get("goal_id")})
        summary = {}
        for gid in goal_ids:
            summary[gid] = compute_mastery(user_id, gid, db)
        return summary
    except Exception as e:
        logger.warning(f"Mastery summary failed: {e}")
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


def compute_streak(user_id: str, db) -> int:
    """Count consecutive days with at least 1 task completion."""
    try:
        from datetime import date, timedelta
        result = (
            db.table("user_tasks")
            .select("completed_at")
            .eq("user_id", user_id)
            .eq("status", "completed")
            .not_.is_("completed_at", "null")
            .order("completed_at", desc=True)
            .limit(60)
            .execute()
        )
        tasks = result.data or []
        if not tasks:
            return 0
        dates = set()
        for t in tasks:
            try:
                d = date.fromisoformat(t["completed_at"][:10])
                dates.add(d)
            except (ValueError, TypeError):
                continue
        if not dates:
            return 0
        today = date.today()
        streak = 0
        check = today
        while check in dates:
            streak += 1
            check -= timedelta(days=1)
        return streak
    except Exception:
        return 0
