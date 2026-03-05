"""SuperMemo-2 spaced repetition engine for habit persistence."""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from supabase import create_client


def calculate_sm2(
    quality: int,
    repetitions: int,
    previous_ef: float,
    previous_interval: int,
) -> dict[str, Any]:
    """
    SuperMemo-2 algorithm.

    Quality: 0-5 (0 = blackout, 5 = perfect recall/execution).
    Returns: repetitions, ef, next_interval_days, next_review_date.

    EF floor 1.3: behavioral persistence research suggests if a habit becomes
    "harder" than that, it should be broken down further by the Socratic Chunker.
    """
    if quality < 3:
        repetitions = 0
        interval = 1
    else:
        repetitions += 1
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 6
        else:
            interval = math.ceil(previous_interval * previous_ef)

    new_ef = previous_ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ef = max(1.3, new_ef)  # EF floor 1.3

    return {
        "repetitions": repetitions,
        "ef": round(new_ef, 3),
        "next_interval_days": int(interval),
        "next_review_date": datetime.now(timezone.utc) + timedelta(days=interval),
    }


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def record_completion(
    tracker_id: str,
    quality: int,
    user_id: Optional[str] = None,
    supabase_client: Any = None,
) -> dict[str, Any]:
    """Record a completion and update tracker. Returns next_review_date and next_interval_days."""
    client = supabase_client or _get_supabase()
    if not client:
        return {"status": "error", "reason": "Supabase not configured"}

    try:
        result = (
            client.table("habit_trackers")
            .select("*")
            .eq("id", tracker_id)
            .execute()
        )
        if not result.data or len(result.data) == 0:
            return {"status": "error", "reason": "not found"}

        row = result.data[0]
        if user_id and row.get("user_id") != user_id:
            return {"status": "error", "reason": "forbidden"}

        reps = row.get("repetitions") or 0
        ef = float(row.get("ef") or 2.5)
        interval = int(row.get("next_interval_days") or 1)

        sm2_result = calculate_sm2(quality, reps, ef, interval)

        client.table("habit_trackers").update(
            {
                "repetitions": sm2_result["repetitions"],
                "quality_last": quality,
                "ef": sm2_result["ef"],
                "next_interval_days": sm2_result["next_interval_days"],
                "last_done_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", tracker_id).execute()

        return {
            "status": "ok",
            "next_review_date": sm2_result["next_review_date"].isoformat(),
            "next_interval_days": sm2_result["next_interval_days"],
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def get_due_trackers(
    user_id: str,
    as_of_date: Optional[datetime] = None,
    supabase_client: Any = None,
) -> list[dict[str, Any]]:
    """Return list of habit trackers due for review on or before as_of_date."""
    client = supabase_client or _get_supabase()
    if not client:
        return []

    ref = as_of_date or datetime.now(timezone.utc)
    ref_date = ref.date() if hasattr(ref, "date") else ref

    try:
        result = (
            client.table("habit_trackers")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        due = []
        for row in result.data or []:
            last = row.get("last_done_at")
            interval = int(row.get("next_interval_days") or 1)
            if not last:
                # Never done: treat as due
                due.append(row)
                continue
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_dt.tzinfo:
                    last_dt = last_dt.astimezone(timezone.utc)
                next_due = (last_dt + timedelta(days=interval)).date()
                if next_due <= ref_date:
                    due.append(row)
            except (ValueError, TypeError):
                due.append(row)
        return due
    except Exception:
        return []
