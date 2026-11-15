"""SuperMemo-2 spaced repetition: how long a tracker waits before its next review.

Trackers are read and written through the ``HabitTrackerRepository`` port: an outage
raises ``PersistenceError`` (503), never "nothing due", and another user's tracker is
``NotFoundError``, exactly like a missing one, so its existence does not leak. The
Habits page reaches this through ``RuleCheckIns`` (each tracker follows one rule).
"""

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.entities import SM2_EF_FLOOR, HabitTracker
from app.domain.errors import NotFoundError
from app.domain.ports import HabitTrackerRepository
from app.services.planning.local_time import local_date


def calculate_sm2(
    quality: int,
    repetitions: int,
    previous_ef: float,
    previous_interval: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    SuperMemo-2 algorithm.

    Quality: 0-5 (0 = blackout, 5 = perfect recall/execution).
    Returns: repetitions, ef, next_interval_days, next_review_date (``now``, default
    the current instant, plus the interval).

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
    new_ef = max(SM2_EF_FLOOR, new_ef)  # EF floor 1.3

    now = now if now is not None else datetime.now(timezone.utc)
    return {
        "repetitions": repetitions,
        "ef": round(new_ef, 3),
        "next_interval_days": int(interval),
        "next_review_date": now + timedelta(days=interval),
    }


async def record_completion(
    trackers: HabitTrackerRepository,
    user_id: str,
    tracker_id: str,
    quality: int,
    *,
    now: datetime | None = None,
) -> HabitTracker:
    """Record a review of the user's tracker, done at ``now`` (default: the current
    instant); return the tracker as stored after it.

    Raises NotFoundError for a tracker the user does not have (another user's
    included) and PersistenceError when the store cannot be reached.
    """
    tracker = await trackers.get(user_id, tracker_id)
    if tracker is None:
        raise NotFoundError("That tracker does not exist.", details={"tracker_id": tracker_id})

    done_at = now if now is not None else datetime.now(timezone.utc)
    sm2_result = calculate_sm2(
        quality, tracker.repetitions, tracker.ef, tracker.next_interval_days or 1, now=done_at
    )
    return await trackers.record_review(
        user_id,
        tracker_id,
        repetitions=sm2_result["repetitions"],
        ef=sm2_result["ef"],
        next_interval_days=sm2_result["next_interval_days"],
        quality=quality,
        done_at=done_at,
    )


def next_due_on(tracker: HabitTracker, zone: ZoneInfo, *, today: date) -> date:
    """The day the tracker's next review is due, on the user's clock (``zone``).

    The day it was last reviewed (or started) there, plus its interval: a review done
    at 01:30 local counts for that local day, whatever the UTC date. A tracker with
    neither is due ``today``.
    """
    if tracker.last_done_at is None:
        return today
    return local_date(tracker.last_done_at, zone) + timedelta(
        days=tracker.next_interval_days or 1
    )
