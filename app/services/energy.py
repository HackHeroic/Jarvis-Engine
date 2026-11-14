"""The focus profile: how well the user gets things done at each hour of their day.

``HeuristicEnergyEstimator`` is the prior, a fixed table of typical focus by hour.
``HistoryEnergyEstimator`` learns from the outcome history (``task_events``). Each
completed, skipped or missed slot of the last ``FOCUS_WINDOW_DAYS`` counts for the
local hour it was scheduled at. Each hour's score is smoothed toward the prior with
``FOCUS_PRIOR_STRENGTH`` pseudo-outcomes, so an hour with little history stays near
the prior. Below ``MIN_FOCUS_OBSERVATIONS`` outcomes the profile is labelled
"prior", and only a "learned" profile steers the solver.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import FOCUS_PRIOR_STRENGTH, FOCUS_WINDOW_DAYS, MIN_FOCUS_OBSERVATIONS
from app.domain.entities import FocusProfile, HourFocus, TaskEventType
from app.domain.ports import EnergyEstimator, TaskEventRepository

HOURS_PER_DAY = 24

# The outcomes a slot can have. Skipped and missed (the slot passed) both count as
# "not done"; a deadline miss is about a date, not an hour, so it is not one of them.
_OUTCOMES = frozenset({TaskEventType.COMPLETED, TaskEventType.SKIPPED, TaskEventType.MISSED})


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

    async def estimate(self, user_id: str, zone: ZoneInfo, *, now: datetime) -> FocusProfile:
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

    async def estimate(self, user_id: str, zone: ZoneInfo, *, now: datetime) -> FocusProfile:
        events = await self._task_events.list_for_user(
            user_id, since=now - timedelta(days=self._window_days), types=set(_OUTCOMES)
        )
        done = [0] * HOURS_PER_DAY
        not_done = [0] * HOURS_PER_DAY
        for event in events:
            start = event.scheduled_start
            if start is None:  # no slot, no hour to learn about
                continue
            if start.tzinfo is None:  # stored instants are UTC
                start = start.replace(tzinfo=timezone.utc)
            hour = start.astimezone(zone).hour
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
