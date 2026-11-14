"""The focus profile: how well the user gets things done at each hour of their day.

``HeuristicEnergyEstimator`` is the prior, a fixed table of typical focus by hour.
``HistoryEnergyEstimator`` learns from the outcome history (``task_events``). Each
completed, skipped or missed slot of the last ``FOCUS_WINDOW_DAYS`` counts for the
local hour it was scheduled at. Each hour's score is smoothed toward the prior with
``FOCUS_PRIOR_STRENGTH`` pseudo-outcomes, so an hour with little history stays near
the prior. Below ``MIN_FOCUS_OBSERVATIONS`` outcomes the profile is labelled
"prior", and only a "learned" profile steers the solver.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app.core.config as config
from app.core.config import FOCUS_PRIOR_STRENGTH, FOCUS_WINDOW_DAYS, MIN_FOCUS_OBSERVATIONS
from app.domain.entities import (
    HOURS_PER_DAY,
    FocusProfile,
    HourFocus,
    TaskEvent,
    TaskEventType,
)
from app.domain.ports import EnergyEstimator, TaskEventRepository
from app.services.task_service import SLOT_OUTCOMES, as_aware, latest_outcome_per_slot

logger = logging.getLogger(__name__)


class HeuristicEnergyEstimator(EnergyEstimator):
    """The prior: typical focus by hour, the same for everyone, learned from nothing."""

    def score(self, hour: int) -> float:
        """Focus at local ``hour``, from 0 to 1."""
        if 9 <= hour <= 12:
            return 0.9
        elif 15 <= hour <= 17:
            return 0.85
        elif 13 <= hour <= 14:
            return 0.5
        elif 7 <= hour <= 9:
            return 0.7
        elif 17 < hour <= 21:
            return 0.6
        else:
            return 0.3

    async def estimate(
        self,
        user_id: str,
        zone: ZoneInfo,
        *,
        now: datetime,
        events: list[TaskEvent] | None = None,
    ) -> FocusProfile:
        return FocusProfile(
            source="prior",
            observations=0,
            hours=[
                HourFocus(hour=h, score=self.score(h), done=0, not_done=0)
                for h in range(HOURS_PER_DAY)
            ],
        )


class HistoryEnergyEstimator(EnergyEstimator):
    """The user's own focus by hour, read off what happened to their scheduled slots."""

    def __init__(
        self,
        task_events: TaskEventRepository,
        prior: HeuristicEnergyEstimator,
        *,
        window_days: int = FOCUS_WINDOW_DAYS,
        prior_strength: float = FOCUS_PRIOR_STRENGTH,
        min_observations: int = MIN_FOCUS_OBSERVATIONS,
    ) -> None:
        self._task_events = task_events
        self._prior = prior
        self._window_days = window_days
        self._prior_strength = prior_strength
        self._min_observations = min_observations

    async def estimate(
        self,
        user_id: str,
        zone: ZoneInfo,
        *,
        now: datetime,
        events: list[TaskEvent] | None = None,
    ) -> FocusProfile:
        """The profile from ``events`` (the user's outcomes of the last ``window_days``)
        when given, else from the history read here."""
        # Skipped and missed (the slot passed) both count as "not done". A deadline miss
        # is about a date, not an hour, so it is not an outcome.
        if events is None:
            events = await self._task_events.list_for_user(
                user_id, since=now - timedelta(days=self._window_days), types=set(SLOT_OUTCOMES)
            )
        done = [0] * HOURS_PER_DAY
        not_done = [0] * HOURS_PER_DAY
        for event in latest_outcome_per_slot(events):
            if event.scheduled_start is None:  # no slot, no hour to learn about
                continue
            hour = as_aware(event.scheduled_start).astimezone(zone).hour
            if event.event_type == TaskEventType.COMPLETED:
                done[hour] += 1
            else:
                not_done[hour] += 1

        hours = [
            HourFocus(hour=h, score=self._smoothed(h, done[h], not_done[h]), done=done[h],
                      not_done=not_done[h])
            for h in range(HOURS_PER_DAY)
        ]
        observations = sum(done) + sum(not_done)
        return FocusProfile(
            source="learned" if observations >= self._min_observations else "prior",
            observations=observations,
            hours=hours,
        )

    def _smoothed(self, hour: int, done: int, not_done: int) -> float:
        """(done + k·prior) / (done + not_done + k): the hour's completion rate, pulled toward the prior.

        An hour with no outcomes is the prior itself. The formula gives the same value
        there, but in floats k·p/k can miss p in the last digit.
        """
        prior = self._prior.score(hour)
        if done + not_done == 0:
            return prior
        k = self._prior_strength
        return (done + k * prior) / (done + not_done + k)


async def solver_focus(
    estimator: EnergyEstimator | None, user_id: str, zone: ZoneInfo, *, now: datetime
) -> list[float] | None:
    """The focus scores a solve may lean on: the user's learned profile, or None.

    None when there is no estimator (no composition root: tests, scripts), when the
    energy term is off, or while the profile is still the prior: a prior never steers
    the solver. A failed read is None too. The plan is then made on TMT alone, never
    lost. Every solve for the user (a plan, a draft edit) reads its profile here.
    """
    if estimator is None or not config.ENERGY_OBJECTIVE_ENABLED:
        return None
    try:
        profile = await estimator.estimate(user_id, zone, now=now)
    except Exception as exc:
        logger.warning("Focus profile read failed (planning on TMT alone): %r", exc)
        return None
    return profile.scores() if profile.source == "learned" else None
