"""The focus profile: how well the user gets things done at each hour of their day.

``HistoryEnergyEstimator`` reads the outcome history (``task_events``): every
completed, skipped or missed slot counts for the local hour it was scheduled at,
and each hour's score is smoothed toward the heuristic prior. With too few
outcomes the profile is the prior, labelled as such.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain.entities import TaskEvent, TaskEventType
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=IST)


def _event(user_id: str, event_type: TaskEventType, slot: datetime) -> TaskEvent:
    """An outcome for a 25-minute slot starting at ``slot``, recorded when it ended."""
    return TaskEvent(
        user_id=user_id,
        task_id=f"t_{slot.isoformat()}_{event_type.value}",
        event_type=event_type,
        occurred_at=slot + timedelta(minutes=25),
        scheduled_start=slot,
        scheduled_end=slot + timedelta(minutes=25),
    )


def _slot(days_ago: int, hour: int, minute: int = 0) -> datetime:
    """A slot at ``hour``:``minute`` IST, ``days_ago`` days before NOW."""
    day = (NOW - timedelta(days=days_ago)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


async def _estimator_with(*events: TaskEvent) -> HistoryEnergyEstimator:
    repo = RepositoryFactory.in_memory().task_events
    for event in events:
        await repo.append(event.user_id, event)
    return HistoryEnergyEstimator(repo, HeuristicEnergyEstimator())


@pytest.mark.asyncio
async def test_history_estimator__no_events__prior_and_source_prior():
    prior = HeuristicEnergyEstimator()

    profile = await (await _estimator_with()).estimate("alice", IST, now=NOW)

    assert profile.source == "prior"
    assert profile.observations == 0
    assert profile.scores() == [prior.score(h) for h in range(24)]
    assert [h.hour for h in profile.hours] == list(range(24))


@pytest.mark.asyncio
async def test_history_estimator__evening_done_morning_skipped__learned_and_evening_higher():
    events = [_event("alice", TaskEventType.COMPLETED, _slot(d, 19)) for d in range(1, 7)]
    events += [_event("alice", TaskEventType.SKIPPED, _slot(d, 9)) for d in range(1, 7)]

    profile = await (await _estimator_with(*events)).estimate("alice", IST, now=NOW)

    assert profile.source == "learned"
    assert profile.observations == 12
    assert profile.hours[19].score > profile.hours[9].score
    assert (profile.hours[19].done, profile.hours[19].not_done) == (6, 0)
    assert (profile.hours[9].done, profile.hours[9].not_done) == (0, 6)


@pytest.mark.asyncio
async def test_history_estimator__buckets_by_local_hour_of_slot():
    # 03:30 UTC is 09:00 in India: the user's hour, not the server's or UTC's.
    slot = datetime(2026, 11, 10, 3, 30, tzinfo=timezone.utc)

    profile = await (
        await _estimator_with(_event("alice", TaskEventType.COMPLETED, slot))
    ).estimate("alice", IST, now=NOW)

    assert profile.hours[9].done == 1
    assert sum(h.done + h.not_done for h in profile.hours) == 1


@pytest.mark.asyncio
async def test_history_estimator__other_users_events__ignored():
    bobs = [_event("bob", TaskEventType.COMPLETED, _slot(d, 19)) for d in range(1, 13)]
    prior = HeuristicEnergyEstimator()

    profile = await (await _estimator_with(*bobs)).estimate("alice", IST, now=NOW)

    assert profile.source == "prior"
    assert profile.observations == 0
    assert profile.scores() == [prior.score(h) for h in range(24)]


@pytest.mark.asyncio
async def test_history_estimator__missed_then_completed_same_slot__counts_once_as_done():
    # The sweep set the slot aside, then the user said they did it after all:
    # one slot, one outcome, and the latest one is what happened.
    slot = _slot(3, 19)
    missed = _event("alice", TaskEventType.MISSED, slot)
    done = _event("alice", TaskEventType.COMPLETED, slot).model_copy(
        update={"task_id": missed.task_id, "occurred_at": slot + timedelta(hours=2)}
    )

    profile = await (await _estimator_with(missed, done)).estimate("alice", IST, now=NOW)

    assert (profile.hours[19].done, profile.hours[19].not_done) == (1, 0)
    assert profile.observations == 1


@pytest.mark.asyncio
async def test_history_estimator__sparse_hour__pulled_toward_prior():
    prior = HeuristicEnergyEstimator()

    profile = await (
        await _estimator_with(_event("alice", TaskEventType.COMPLETED, _slot(2, 14)))
    ).estimate("alice", IST, now=NOW)

    assert prior.score(14) < profile.hours[14].score < 1.0


# Today's UserModel.get_estimated_energy table, hour by hour, as it was written.
_USER_MODEL_TABLE = (
    [0.3] * 7            # 00-06
    + [0.7] * 2          # 07-08
    + [0.9] * 4          # 09-12
    + [0.5] * 2          # 13-14
    + [0.85] * 3         # 15-17
    + [0.6] * 4          # 18-21
    + [0.3] * 2          # 22-23
)


@pytest.mark.asyncio
async def test_heuristic__matches_user_model_table_for_every_hour(clock):
    import app.core.user_model as user_model_module
    from app.core.user_model import UserModel
    from app.infrastructure.persistence.factory import RepositoryFactory

    heuristic = HeuristicEnergyEstimator()
    repos = RepositoryFactory.in_memory()
    user_model = UserModel(
        "alice", profiles=repos.profiles, constraints=repos.constraints, tasks=repos.tasks
    )

    for hour in range(24):
        clock.freeze(datetime(2026, 11, 16, hour, 0, tzinfo=IST), user_model_module)
        assert heuristic.score(hour) == _USER_MODEL_TABLE[hour], hour
        assert await user_model.get_estimated_energy() == _USER_MODEL_TABLE[hour], hour
