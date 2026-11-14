"""SuperMemo-2 spaced repetition engine for habit persistence.

Trackers are read and written through the ``HabitTrackerRepository`` port: an outage
raises ``PersistenceError`` (503), never "nothing due", and another user's tracker is
``NotFoundError``, exactly like a missing one, so its existence does not leak.
"""

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.entities import SM2_EF_FLOOR
from app.domain.errors import NotFoundError
from app.domain.ports import HabitTrackerRepository


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
    new_ef = max(SM2_EF_FLOOR, new_ef)  # EF floor 1.3

    return {
        "repetitions": repetitions,
        "ef": round(new_ef, 3),
        "next_interval_days": int(interval),
        "next_review_date": datetime.now(timezone.utc) + timedelta(days=interval),
    }


async def record_completion(
    trackers: HabitTrackerRepository, user_id: str, tracker_id: str, quality: int
) -> dict[str, Any]:
    """Record a review of the user's tracker; return next_review_date and next_interval_days.

    Raises NotFoundError for a tracker the user does not have (another user's
    included) and PersistenceError when the store cannot be reached.
    """
    tracker = await trackers.get(user_id, tracker_id)
    if tracker is None:
        raise NotFoundError("That tracker does not exist.", details={"tracker_id": tracker_id})

    sm2_result = calculate_sm2(
        quality, tracker.repetitions, tracker.ef, tracker.next_interval_days or 1
    )
    await trackers.record_review(
        user_id,
        tracker_id,
        repetitions=sm2_result["repetitions"],
        ef=sm2_result["ef"],
        next_interval_days=sm2_result["next_interval_days"],
        quality=quality,
        done_at=datetime.now(timezone.utc),
    )
    return {
        "next_review_date": sm2_result["next_review_date"].isoformat(),
        "next_interval_days": sm2_result["next_interval_days"],
    }


def _local_date(moment: datetime | date, zone: ZoneInfo) -> date:
    """The date of ``moment`` on the user's clock.

    A naive datetime is taken as the user's wall time; a bare date is already local.
    """
    if not isinstance(moment, datetime):
        return moment
    return moment.astimezone(zone).date() if moment.tzinfo is not None else moment.date()


async def get_due_trackers(
    trackers: HabitTrackerRepository,
    user_id: str,
    zone: ZoneInfo,
    *,
    as_of_date: datetime | date | None = None,
) -> list[dict[str, Any]]:
    """The user's trackers due for review on or before today (or ``as_of_date``), as rows.

    Both sides of the comparison are dates on the user's clock (``zone``): the day the
    habit was last done plus its interval, against the day it is now there. A tracker
    never done is due. Newest tracker first, as the repository lists them; an outage
    raises PersistenceError, never "nothing due".
    """
    today = _local_date(as_of_date or datetime.now(timezone.utc), zone)
    due = []
    for tracker in await trackers.list_for_user(user_id):
        if tracker.last_done_at is not None:
            interval = tracker.next_interval_days or 1
            if tracker.last_done_at.astimezone(zone).date() + timedelta(days=interval) > today:
                continue
        due.append(tracker.model_dump(mode="json"))
    return due
