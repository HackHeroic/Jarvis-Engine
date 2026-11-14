"""SuperMemo-2 spaced repetition engine for habit persistence."""

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.domain.errors import PersistenceError
from app.services.planning.local_time import user_zone
from supabase import create_client

logger = logging.getLogger(__name__)


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
    user_id: str,
    supabase_client: Any = None,
) -> dict[str, Any]:
    """Record a completion and update tracker. Returns next_review_date and next_interval_days.

    Scoped to ``user_id``: another user's tracker is reported as ``"not found"``,
    exactly like a missing one, so its existence does not leak.
    """
    client = supabase_client or _get_supabase()
    if not client:
        return {"status": "error", "reason": "Supabase not configured"}

    try:
        result = (
            client.table("habit_trackers")
            .select("*")
            .eq("id", tracker_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not result.data or len(result.data) == 0:
            return {"status": "error", "reason": "not found"}

        row = result.data[0]
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
        ).eq("id", tracker_id).eq("user_id", user_id).execute()

        return {
            "status": "ok",
            "next_review_date": sm2_result["next_review_date"].isoformat(),
            "next_interval_days": sm2_result["next_interval_days"],
        }
    except Exception:
        logger.warning("record_completion failed for tracker %s", tracker_id, exc_info=True)
        return {"status": "error", "reason": "database error"}


def _local_date(moment: datetime | date, zone: ZoneInfo) -> date:
    """The date of ``moment`` on the user's clock.

    A naive datetime is taken as the user's wall time; a bare date is already local.
    """
    if not isinstance(moment, datetime):
        return moment
    return moment.astimezone(zone).date() if moment.tzinfo is not None else moment.date()


async def get_due_trackers(
    user_id: str,
    supabase_client: Any = None,
    as_of_date: datetime | None = None,
    zone: ZoneInfo | None = None,
) -> list[dict[str, Any]]:
    """Return the user's habit trackers due for review on or before ``as_of_date``'s local date.

    Both sides of the comparison are dates on the user's clock (``zone``, default
    ``user_zone()``): the day the habit was last done plus its interval, against the
    day it is now (or ``as_of_date``) there. A tracker never done is due.
    """
    client = supabase_client or _get_supabase()
    if not client:
        return []

    zone = zone or user_zone()
    today = _local_date(as_of_date or datetime.now(timezone.utc), zone)

    try:
        result = (
            client.table("habit_trackers")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as exc:
        # Not []: "nothing is due" would be a false answer to a database outage.
        raise PersistenceError("Could not reach the database.") from exc
    due = []
    for row in result.data or []:
        last = row.get("last_done_at")
        if not last:
            # Never done: treat as due
            due.append(row)
            continue
        try:
            interval = int(row.get("next_interval_days") or 1)
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:  # stored instants are UTC
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            next_due = last_dt.astimezone(zone).date() + timedelta(days=interval)
            if next_due <= today:
                due.append(row)
        except (ValueError, TypeError):
            due.append(row)
    return due
