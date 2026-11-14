"""Lightweight mastery tracker — statistical bridge until DKT arrives.

3 signals: completion quality, success ratio, reschedule penalty.
Same output interface as future DKT: a mastery score per goal, 0 to 1.

Every signal reads the outcome history through the ``TaskEventRepository`` port, never
task statuses or the never-written ``user_tasks.quality_score``. Each slot (task,
scheduled start) counts once, as its latest outcome (``task_service``'s
``latest_outcome_per_slot``), and quality is the rating the completed event carries.
Goal ids come from the user's tasks (``TaskRepository``), a goal's mastery target from
its newest deadline update (``GoalRepository``). Every history read stays inside a
time window, under the store's row cap. A streak's days are dates on the user's clock.
"""

import json
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import app.core.config as config
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain.clock import utc_now
from app.domain.entities import TaskEvent, TaskEventType
from app.domain.ports import GoalRepository, TaskEventRepository, TaskRepository
from app.services.task_service import SLOT_OUTCOMES, as_aware, latest_outcome_per_slot

# The outcomes a slot can come to (task_service's, the one definition): scheduled and
# rescheduled events are not outcomes.
OUTCOME_TYPES = SLOT_OUTCOMES
# How far back a streak is counted: a longer run reads as this many days.
STREAK_WINDOW_DAYS = 60
# How far back the success ratio and completion quality look.
SUCCESS_WINDOW_DAYS = 60
# What a goal with no outcomes yet reads as: a neutral prior, not inflated confidence.
NEUTRAL_SUCCESS_RATIO = 0.5
NEUTRAL_QUALITY = 2.5
# The quality trend compares the newest TREND_SAMPLE ratings with the TREND_SAMPLE
# before them; under TREND_MIN_RATINGS there is no trend to call.
TREND_SAMPLE = 5
TREND_MIN_RATINGS = 4
TREND_THRESHOLD = 0.5


async def _outcomes(
    user_id: str, task_events: TaskEventRepository | None, days: int, reader: str
) -> list[TaskEvent]:
    """The user's outcome events of the last ``days`` days, through the repository.

    No repository (no composition root: tests, scripts) is no history. A failed read
    is logged (naming ``reader``) and read as no history too: every signal then takes
    its neutral value.
    """
    if task_events is None:
        return []
    try:
        return await task_events.list_for_user(
            user_id, since=utc_now() - timedelta(days=days), types=set(OUTCOME_TYPES)
        )
    except Exception as e:
        logger.warning(f"{reader} query failed: {type(e).__name__}")
        return []


def _since(events: list[TaskEvent], since: datetime) -> list[TaskEvent]:
    return [e for e in events if as_aware(e.occurred_at) >= since]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _slots_of(events: list[TaskEvent], goal_id: Optional[str]) -> list[TaskEvent]:
    """The latest outcome of each slot of the goal (every goal when None), oldest first."""
    return [
        event
        for event in latest_outcome_per_slot(events)
        if goal_id is None or event.metadata.get("goal_id") == goal_id
    ]


def success_ratio_of(events: list[TaskEvent], goal_id: Optional[str] = None) -> float:
    """Completed slots / all slots that came to an outcome (completed, skipped, missed).

    Each slot counts once, as its latest outcome: missed, then completed after all, is
    one completed slot. NEUTRAL_SUCCESS_RATIO when the goal has no outcomes.
    """
    slots = _slots_of(events, goal_id)
    if not slots:
        return NEUTRAL_SUCCESS_RATIO
    done = sum(1 for event in slots if event.event_type is TaskEventType.COMPLETED)
    return done / len(slots)


def _ratings(events: list[TaskEvent], goal_id: Optional[str] = None) -> list[float]:
    """The quality ratings (0-5) completed slots carry, newest first."""
    return [
        float(event.metadata["quality"])
        for event in reversed(_slots_of(events, goal_id))
        if event.event_type is TaskEventType.COMPLETED and _is_number(event.metadata.get("quality"))
    ]


def average_quality_of(events: list[TaskEvent], goal_id: Optional[str] = None) -> float:
    """The goal's average completion rating (0-5); NEUTRAL_QUALITY with none rated."""
    ratings = _ratings(events, goal_id)
    return sum(ratings) / len(ratings) if ratings else NEUTRAL_QUALITY


def quality_trend_of(events: list[TaskEvent]) -> str:
    """The newest TREND_SAMPLE ratings against the TREND_SAMPLE before them.

    "improving" or "declining" past ±TREND_THRESHOLD; "stable" otherwise, and with
    fewer than TREND_MIN_RATINGS ratings or none before the newest sample.
    """
    ratings = _ratings(events)
    if len(ratings) < TREND_MIN_RATINGS:
        return "stable"
    recent, older = ratings[:TREND_SAMPLE], ratings[TREND_SAMPLE:2 * TREND_SAMPLE]
    if not older:
        return "stable"
    diff = sum(recent) / len(recent) - sum(older) / len(older)
    if diff > TREND_THRESHOLD:
        return "improving"
    if diff < -TREND_THRESHOLD:
        return "declining"
    return "stable"


async def calculate_success_ratio(
    user_id: str, goal_id: Optional[str], task_events: TaskEventRepository
) -> float:
    """Success ratio of the goal (every goal when None) over the last SUCCESS_WINDOW_DAYS.

    Feeds into: TMT Expectancy, Mastery score, Coach messages. NEUTRAL_SUCCESS_RATIO
    when there are no outcomes, or the history could not be read (logged).
    """
    events = await _outcomes(user_id, task_events, SUCCESS_WINDOW_DAYS, "Success ratio")
    return success_ratio_of(events, goal_id)


async def compute_quality_trend(user_id: str, task_events: TaskEventRepository) -> str:
    """improving / stable / declining, from the ratings of the last SUCCESS_WINDOW_DAYS."""
    return quality_trend_of(
        await _outcomes(user_id, task_events, SUCCESS_WINDOW_DAYS, "Quality trend")
    )


def _skips(events: list[TaskEvent], goal_id: Optional[str]) -> int:
    """Slots of the goal (all goals when None) whose latest outcome is a skip."""
    return sum(
        1 for event in _slots_of(events, goal_id) if event.event_type is TaskEventType.SKIPPED
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
    events = await _outcomes(
        user_id, task_events, config.FOCUS_WINDOW_DAYS, "Reschedule count"
    )
    return _skips(events, goal_id)


def _mastery_target(context: Optional[str]) -> Optional[float]:
    """mastery_level_target (1-5) from the goal_metadata a deadline update's context holds."""
    if not context:
        return None
    try:
        snippet = json.loads(context)
    except (TypeError, ValueError):
        return None
    meta = snippet.get("goal_metadata") if isinstance(snippet, dict) else None
    target = meta.get("mastery_level_target") if isinstance(meta, dict) else None
    return target if _is_number(target) else None


def compute_mastery(
    avg_quality: float, success_ratio: float, *, reschedule_count: int, target: Optional[float]
) -> float:
    """Lightweight mastery score (0.0 to 1.0).

    3 data sources:
    1. Completion quality (0-5) → normalized to 0-1, weight 0.5
    2. Success ratio (completed / all outcomes) → weight 0.3
    3. Reschedule penalty (-0.1 per push, capped at -0.3) → weight 0.2

    With a mastery_level_target (1-5) the quality term is progress toward it.
    Same interface as future DKT — only internals change.
    """
    pacing = max(0.0, 1.0 - reschedule_count * 0.1) * 0.2
    if target and target > 0:
        # target=3 means an average rating of 3/5 is 100% of the quality term.
        target_quality = target / 5.0
        quality_progress = min((avg_quality / 5.0) / target_quality, 1.0)
        return quality_progress * 0.5 + success_ratio * 0.3 + pacing
    return (avg_quality / 5.0) * 0.5 + success_ratio * 0.3 + pacing


async def get_mastery_summary(
    user_id: str,
    tasks: TaskRepository,
    task_events: TaskEventRepository,
    goals: GoalRepository,
) -> dict[str, float]:
    """Mastery scores for the goals of the user's tasks. Returns {goal_id: mastery_score}.

    The outcome history is read once, for every goal and every signal (each keeps to
    its own window). A failed read is logged and the summary is empty (or, for the
    history, every signal neutral): the coach then speaks without numbers.
    """
    try:
        goal_ids = sorted({t.goal_id for t in await tasks.list_for_user(user_id) if t.goal_id})
        if not goal_ids:
            return {}
        days = max(SUCCESS_WINDOW_DAYS, config.FOCUS_WINDOW_DAYS)
        history = await _outcomes(user_id, task_events, days, "Mastery history")
        now = utc_now()
        outcomes = _since(history, now - timedelta(days=SUCCESS_WINDOW_DAYS))
        recent = _since(history, now - timedelta(days=config.FOCUS_WINDOW_DAYS))
        summary = {}
        for gid in goal_ids:
            summary[gid] = compute_mastery(
                average_quality_of(outcomes, gid),
                success_ratio_of(outcomes, gid),
                reschedule_count=_skips(recent, gid),
                target=_mastery_target(await goals.latest_context(user_id, gid)),
            )
        return summary
    except Exception as e:
        logger.warning(f"Mastery summary failed: {type(e).__name__}")
        return {}


def compute_streak_from_events(events: list[TaskEvent], zone: ZoneInfo, today: date) -> int:
    """Consecutive local dates with at least one completion, ending today or yesterday.

    The one streak: the coach's and ``GET /you``'s. ``events`` is the outcome history;
    each slot counts as its latest outcome, and a completion's day is the date it
    happened on in ``zone`` (00:30 IST is that IST day, not the previous UTC one). A
    streak is not broken until the local day ends: with nothing completed yet today it
    counts back from yesterday, and only a day gone by with nothing done ends it.
    """
    days = {
        as_aware(event.occurred_at).astimezone(zone).date()
        for event in latest_outcome_per_slot(events)
        if event.event_type is TaskEventType.COMPLETED
    }
    streak = 0
    day = today if today in days else today - timedelta(days=1)
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def streak_as_of(events: list[TaskEvent], zone: ZoneInfo, now: datetime) -> int:
    """The completion streak at ``now``: ``compute_streak_from_events`` over the outcomes
    of the last STREAK_WINDOW_DAYS, ending on today's date in ``zone``.

    ``events`` may reach further back; only the window counts.
    """
    window = _since(events, now - timedelta(days=STREAK_WINDOW_DAYS))
    return compute_streak_from_events(window, zone, now.astimezone(zone).date())


async def completion_streak(
    user_id: str, task_events: TaskEventRepository, zone: ZoneInfo, *, now: datetime
) -> int:
    """The user's completion streak at ``now``, read from their outcome history.

    A failed read raises (PersistenceError): a streak of 0 would be a false answer.
    """
    events = await task_events.list_for_user(
        user_id, since=now - timedelta(days=STREAK_WINDOW_DAYS), types=set(OUTCOME_TYPES)
    )
    return streak_as_of(events, zone, now)
