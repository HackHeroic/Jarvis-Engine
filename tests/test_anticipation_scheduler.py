"""AnticipationScheduler: the in-process loop that runs sweeps and triggers.

A tick runs each sweep whose interval has passed, then each user's due triggers,
one user at a time. Nothing that goes wrong inside a tick stops the loop or the app.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import pytest

from app.domain import errors
from app.domain.anticipation import ConsentPolicy
from app.domain.ports import ProfileRepository, Sweep
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.anticipation.scheduler import AnticipationScheduler
from app.services.anticipation.service import ActionFactory, AnticipationService
from tests.anticipation_fakes import CountingTrigger

NOW = datetime(2026, 11, 14, 3, 30, tzinfo=timezone.utc)  # 09:00 in India
SCHEDULER_LOGGER = "app.services.anticipation.scheduler"


class _CountingSweep(Sweep):
    name: ClassVar[str] = "counting_sweep"
    interval: ClassVar[timedelta] = timedelta(minutes=5)

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.runs: list[tuple[list[str], datetime]] = []

    async def run(self, user_ids: list[str], now: datetime) -> int:
        self.runs.append((list(user_ids), now))
        if self.error is not None:
            raise self.error
        return len(user_ids)


class _BlockingSweep(Sweep):
    """Waits until released, recording how many runs are in flight at once."""

    name: ClassVar[str] = "blocking_sweep"
    interval: ClassVar[timedelta] = timedelta(0)

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.in_flight = 0
        self.most_in_flight = 0

    async def run(self, user_ids: list[str], now: datetime) -> int:
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1
        return 0


class _FlakyProfiles(ProfileRepository):
    """Profiles whose user listing fails ``failures`` times before it answers."""

    def __init__(self, inner: ProfileRepository, failures: int) -> None:
        self._inner = inner
        self.failures = failures

    async def get_or_create(self, user_id: str):
        return await self._inner.get_or_create(user_id)

    async def update(self, user_id: str, **fields: Any):
        return await self._inner.update(user_id, **fields)

    async def list_user_ids(self) -> list[str]:
        if self.failures:
            self.failures -= 1
            raise errors.PersistenceError("Could not reach the database.")
        return await self._inner.list_user_ids()


async def _repos_with_users(*user_ids: str):
    repos = RepositoryFactory.in_memory()
    for user_id in user_ids:
        await repos.profiles.get_or_create(user_id)
    return repos


def _scheduler(repos, trigger, sweeps, *, profiles=None, **kwargs) -> AnticipationScheduler:
    service = AnticipationService(
        {trigger.name: trigger},
        ActionFactory(),
        ConsentPolicy.default(),
        repos.suggestions,
        repos.profiles,
        repos.constraints,
    )
    return AnticipationScheduler(service, profiles or repos.profiles, sweeps, **kwargs)


async def _until(condition, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


# --- tick ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick__sweep_runs_once_per_interval():
    repos = await _repos_with_users("u1", "u2")
    sweep = _CountingSweep()
    scheduler = _scheduler(repos, CountingTrigger(due=False), [sweep])

    reports = [
        await scheduler.tick(NOW + offset)
        for offset in (
            timedelta(0),
            timedelta(minutes=1),
            timedelta(minutes=4, seconds=59),
            timedelta(minutes=5),
        )
    ]

    assert [now for _, now in sweep.runs] == [NOW, NOW + timedelta(minutes=5)]
    assert sweep.runs[0][0] == ["u1", "u2"]
    assert [r["sweeps"] for r in reports] == [
        {"counting_sweep": 2}, {}, {}, {"counting_sweep": 2}
    ]
    assert all(r["errors"] == 0 for r in reports)


@pytest.mark.asyncio
async def test_tick__one_users_trigger_raises__others_still_run(caplog):
    repos = await _repos_with_users("u1", "u2", "u3")
    scheduler = _scheduler(repos, CountingTrigger(fail_for=frozenset({"u2"})), [])

    with caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER):
        report = await scheduler.tick(NOW)

    assert (report["outcomes"], report["errors"]) == (2, 1)
    assert len(await repos.suggestions.list("u1")) == 1
    assert await repos.suggestions.list("u2") == []
    assert len(await repos.suggestions.list("u3")) == 1
    (record,) = [r for r in caplog.records if r.name == SCHEDULER_LOGGER]
    assert "u2" in record.getMessage() and "RuntimeError" in record.getMessage()
    assert "secret" not in record.getMessage() and record.exc_info is None


@pytest.mark.asyncio
async def test_tick__sweep_raises__logged_and_triggers_still_run(caplog):
    repos = await _repos_with_users("u1")
    scheduler = _scheduler(repos, CountingTrigger(), [_CountingSweep(error=RuntimeError("x"))])

    with caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER):
        report = await scheduler.tick(NOW)

    assert (report["outcomes"], report["errors"]) == (1, 1)
    assert any("counting_sweep" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_tick__profiles_unreachable__logged_and_next_tick_runs(caplog):
    repos = await _repos_with_users("u1")
    sweep = _CountingSweep()
    profiles = _FlakyProfiles(repos.profiles, failures=1)
    scheduler = _scheduler(repos, CountingTrigger(), [sweep], profiles=profiles)

    with caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER):
        down = await scheduler.tick(NOW)
    back = await scheduler.tick(NOW + timedelta(minutes=1))

    assert down == {"sweeps": {}, "outcomes": 0, "errors": 1}
    assert any("PersistenceError" in r.getMessage() for r in caplog.records)
    assert back == {"sweeps": {"counting_sweep": 1}, "outcomes": 1, "errors": 0}


@pytest.mark.asyncio
async def test_tick__background_llm_calls_attributed_to_user(recording_llm):
    from app.core.model_router import current_user_var

    repos = await _repos_with_users("u1", "u2")
    scheduler = _scheduler(repos, CountingTrigger(ask_model=True), [])

    report = await scheduler.tick(NOW)

    assert report["outcomes"] == 2
    assert [(c.user_id, c.session_id) for c in recording_llm.calls] == [
        ("u1", None), ("u2", None)
    ]
    assert current_user_var.get() is None  # attribution undone after each user


@pytest.mark.asyncio
async def test_ticks__called_concurrently__never_overlap():
    repos = await _repos_with_users("u1")
    sweep = _BlockingSweep()
    scheduler = _scheduler(repos, CountingTrigger(due=False), [sweep])

    first = asyncio.create_task(scheduler.tick(NOW))
    second = asyncio.create_task(scheduler.tick(NOW + timedelta(minutes=1)))
    await sweep.entered.wait()
    await asyncio.sleep(0.02)
    sweep.release.set()
    await asyncio.gather(first, second)

    assert sweep.most_in_flight == 1


# --- start / stop -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop__clean_cancel():
    repos = await _repos_with_users("u1")
    sweep = _BlockingSweep()
    scheduler = _scheduler(repos, CountingTrigger(due=False), [sweep], tick_s=0.01)

    scheduler.start()
    await asyncio.sleep(0)
    assert not sweep.entered.is_set()  # the first tick waits a full tick_s
    await asyncio.wait_for(sweep.entered.wait(), 2.0)  # a tick is now mid-sweep
    await asyncio.wait_for(scheduler.stop(), 1.0)

    assert sweep.in_flight == 0  # the tick in flight was cancelled, not left running
    await scheduler.stop()  # stopping twice is harmless


@pytest.mark.asyncio
async def test_start__tick_raises__loop_keeps_ticking(caplog):
    repos = await _repos_with_users("u1")
    sweep = _CountingSweep()
    readings = iter([RuntimeError("clock broke")])

    def clock() -> datetime:
        reading = next(readings, None)
        if reading is not None:
            raise reading
        return NOW + timedelta(minutes=10 * len(sweep.runs))

    scheduler = _scheduler(repos, CountingTrigger(due=False), [sweep], tick_s=0.01, clock=clock)

    with caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER):
        scheduler.start()
        try:
            await _until(lambda: len(sweep.runs) >= 2)
        finally:
            await scheduler.stop()

    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


# --- the lifespan ---------------------------------------------------------------------------


def _offline_lifespan(monkeypatch, tmp_path):
    """The real lifespan with no database and no local model: in-memory repositories."""
    import app.core.config as cfg
    import app.main as main

    class _NoDatabase:
        def __init__(self) -> None:
            raise ValueError("SUPABASE_URL is not set")

    monkeypatch.setattr(main, "DatabaseClient", _NoDatabase)
    monkeypatch.setattr(cfg, "CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setattr(cfg, "detect_loaded_models", lambda: {"loaded": [], "warning": "offline"})
    monkeypatch.setattr(cfg, "GEMINI_PRIMARY", cfg.GEMINI_PRIMARY)  # the lifespan sets it
    return main


def _record_calls(monkeypatch, calls: list[str]) -> None:
    from app.services.usage_ledger import UsageLedger

    start, stop, shutdown = (
        AnticipationScheduler.start, AnticipationScheduler.stop, UsageLedger.shutdown
    )

    def recording_start(self) -> None:
        calls.append("scheduler.start")
        start(self)

    async def recording_stop(self) -> None:
        calls.append("scheduler.stop")
        await stop(self)

    async def recording_shutdown(self, *args, **kwargs):
        calls.append("ledger.shutdown")
        return await shutdown(self, *args, **kwargs)

    monkeypatch.setattr(AnticipationScheduler, "start", recording_start)
    monkeypatch.setattr(AnticipationScheduler, "stop", recording_stop)
    monkeypatch.setattr(UsageLedger, "shutdown", recording_shutdown)


@pytest.mark.asyncio
async def test_lifespan__enabled__scheduler_started_and_stopped_before_the_ledger(
    monkeypatch, tmp_path
):
    import app.core.config as cfg
    from fastapi import FastAPI

    main = _offline_lifespan(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "ANTICIPATION_ENABLED", True)
    calls: list[str] = []
    _record_calls(monkeypatch, calls)
    app = FastAPI()

    async with main.lifespan(app):
        assert isinstance(app.state.anticipation_scheduler, AnticipationScheduler)
        assert calls == ["scheduler.start"]

    assert calls == ["scheduler.start", "scheduler.stop", "ledger.shutdown"]


@pytest.mark.asyncio
async def test_lifespan__disabled__scheduler_never_started(monkeypatch, tmp_path):
    from fastapi import FastAPI

    main = _offline_lifespan(monkeypatch, tmp_path)  # conftest turns anticipation off
    calls: list[str] = []
    _record_calls(monkeypatch, calls)

    async with main.lifespan(FastAPI()):
        pass

    assert "scheduler.start" not in calls
