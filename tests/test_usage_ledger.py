"""UsageLedger: every LLM call recorded; storage trouble never reaches the call."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.entities import LLMLocation, LLMUsageEvent
from app.domain.errors import PersistenceError
from app.domain.events import DraftAccepted, EventBus, LLMCallCompleted
from app.domain.ports import UsageRepository
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.usage_ledger import UsageLedger

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _usage(
    location: LLMLocation = LLMLocation.LOCAL,
    *,
    user_id: str = "u1",
    tokens: tuple[int, int] = (100, 20),
    redactions: int = 0,
    at: datetime = NOW,
) -> LLMUsageEvent:
    return LLMUsageEvent(
        user_id=user_id,
        session_id="s1",
        task="voice_of_jarvis",
        role="fast",
        provider="local" if location is LLMLocation.LOCAL else "gemini",
        model="m",
        location=location,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        latency_ms=50,
        pii_redactions=redactions,
        created_at=at,
    )


def _completed(usage: LLMUsageEvent) -> LLMCallCompleted:
    return LLMCallCompleted(occurred_at=usage.created_at, user_id=usage.user_id, usage=usage)


class _UnreachableUsage(UsageRepository):
    """The live database before the capstone migration: the table is not there."""

    def __init__(self) -> None:
        self.attempts = 0

    async def record(self, event: LLMUsageEvent) -> None:
        self.attempts += 1
        raise PersistenceError("Could not reach the database.")

    async def summary(self, user_id: str, *, since: datetime | None = None) -> dict:
        raise PersistenceError("Could not reach the database.")


class _SlowUsage(_UnreachableUsage):
    """A database that answers only when released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def record(self, event: LLMUsageEvent) -> None:
        await self.release.wait()


@pytest.mark.asyncio
async def test_usage_ledger__receipt__splits_local_and_cloud():
    repos = RepositoryFactory.in_memory()
    ledger = UsageLedger(repos.usage)
    for usage in (
        _usage(LLMLocation.LOCAL, tokens=(100, 20)),
        _usage(LLMLocation.LOCAL, tokens=(50, 10)),
        _usage(LLMLocation.CLOUD, tokens=(30, 5), redactions=2),
        _usage(LLMLocation.CLOUD, user_id="u2", tokens=(999, 999)),  # somebody else
    ):
        await ledger.handle(_completed(usage))
    await ledger.drain()

    assert await ledger.receipt("u1") == {
        "on_device_calls": 2,
        "cloud_calls": 1,
        "pii_redactions": 2,
        "input_tokens": 180,
        "output_tokens": 35,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_usage_ledger__receipt_since__counts_only_later_calls():
    repos = RepositoryFactory.in_memory()
    ledger = UsageLedger(repos.usage)
    await ledger.handle(_completed(_usage(at=NOW - timedelta(days=2))))
    await ledger.handle(_completed(_usage(LLMLocation.CLOUD, at=NOW)))
    await ledger.drain()

    receipt = await ledger.receipt("u1", since=NOW - timedelta(hours=1))

    assert (receipt["on_device_calls"], receipt["cloud_calls"]) == (0, 1)


@pytest.mark.asyncio
async def test_usage_ledger__other_events__ignored():
    repos = RepositoryFactory.in_memory()
    ledger = UsageLedger(repos.usage)

    await ledger.handle(DraftAccepted(occurred_at=NOW, user_id="u1", draft_id="d", task_count=2))
    await ledger.drain()

    assert (await ledger.receipt("u1"))["on_device_calls"] == 0


@pytest.mark.asyncio
async def test_usage_ledger__storage_unreachable__call_unharmed_and_warned_once(caplog):
    usage = _UnreachableUsage()
    ledger = UsageLedger(usage)
    bus = EventBus()
    bus.subscribe(LLMCallCompleted, ledger)

    with caplog.at_level(logging.WARNING, logger="app.services.usage_ledger"):
        for _ in range(3):
            await bus.publish(_completed(_usage()))  # would raise into the caller if unhandled
        await ledger.drain()

    assert usage.attempts == 3  # carries on recording, call after call
    warnings = [r for r in caplog.records if r.name == "app.services.usage_ledger"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].exc_info is None  # one line, no traceback


@pytest.mark.asyncio
async def test_usage_ledger__storage_hangs__handle_returns_at_once():
    """The ledger is accounting: a slow write must never delay the answer it describes."""
    usage = _SlowUsage()
    ledger = UsageLedger(usage)

    await asyncio.wait_for(ledger.handle(_completed(_usage())), timeout=0.5)

    usage.release.set()
    await ledger.drain()


class _CountingSlowUsage(_SlowUsage):
    """A database that answers only when released, and counts what it stored."""

    def __init__(self) -> None:
        super().__init__()
        self.stored = 0

    async def record(self, event: LLMUsageEvent) -> None:
        await self.release.wait()
        self.stored += 1


@pytest.mark.asyncio
async def test_usage_ledger__more_than_8_writes_in_flight__extras_dropped_and_counted():
    usage = _CountingSlowUsage()
    ledger = UsageLedger(usage)

    for _ in range(11):
        await ledger.handle(_completed(_usage()))
    usage.release.set()
    await ledger.drain()

    assert usage.stored == 8
    assert ledger.dropped == 3
    # A slot frees up once a write finishes: recording carries on.
    await ledger.handle(_completed(_usage()))
    await ledger.drain()
    assert usage.stored == 9


@pytest.mark.asyncio
async def test_usage_ledger__shutdown__waits_at_most_its_timeout_and_logs_the_loss_once(caplog):
    usage = _CountingSlowUsage()  # never released before shutdown
    ledger = UsageLedger(usage)
    for _ in range(10):
        await ledger.handle(_completed(_usage()))

    with caplog.at_level(logging.WARNING, logger="app.services.usage_ledger"):
        await asyncio.wait_for(ledger.shutdown(timeout_s=0.05), timeout=1.0)
        await ledger.shutdown(timeout_s=0.05)

    lines = [r.getMessage() for r in caplog.records if r.name == "app.services.usage_ledger"]
    assert len(lines) == 1
    assert "10" in lines[0]  # 2 over the cap + 8 still unwritten at shutdown
    usage.release.set()
    await ledger.drain()


@pytest.mark.asyncio
async def test_usage_ledger__shutdown__nothing_lost__logs_nothing(caplog):
    ledger = UsageLedger(RepositoryFactory.in_memory().usage)
    await ledger.handle(_completed(_usage()))

    with caplog.at_level(logging.WARNING, logger="app.services.usage_ledger"):
        await ledger.shutdown()

    assert [r for r in caplog.records if r.name == "app.services.usage_ledger"] == []


@pytest.mark.asyncio
async def test_usage_ledger__storage_unreachable__the_warning_names_the_exception_class(caplog):
    ledger = UsageLedger(_UnreachableUsage())

    with caplog.at_level(logging.WARNING, logger="app.services.usage_ledger"):
        await ledger.handle(_completed(_usage()))
        await ledger.drain()

    (warning,) = [r for r in caplog.records if r.name == "app.services.usage_ledger"]
    assert "PersistenceError" in warning.getMessage()


@pytest.mark.asyncio
async def test_lifespan__shutdown__waits_for_the_ledger_writes_in_flight(monkeypatch, tmp_path):
    """The real lifespan, offline: a usage record still being written when the app
    shuts down lands before the process goes; it is not dropped mid-write."""
    from fastapi import FastAPI

    import app.core.config as cfg
    import app.main as main
    from app.infrastructure.persistence.in_memory import InMemoryUsageRepository

    class _NoDatabase:
        def __init__(self) -> None:
            raise ValueError("SUPABASE_URL is not set")  # -> the stub client, in-memory repos

    monkeypatch.setattr(main, "DatabaseClient", _NoDatabase)
    monkeypatch.setattr(cfg, "CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setattr(cfg, "detect_loaded_models", lambda: {"loaded": [], "warning": "offline"})
    monkeypatch.setattr(cfg, "GEMINI_PRIMARY", cfg.GEMINI_PRIMARY)  # the lifespan sets it

    record = InMemoryUsageRepository.record

    async def slow_record(self, event):
        await asyncio.sleep(0.05)  # storage is a round-trip away
        await record(self, event)

    monkeypatch.setattr(InMemoryUsageRepository, "record", slow_record)
    app = FastAPI()

    async with main.lifespan(app):
        container = app.state.container
        await container.events.publish(_completed(_usage(user_id="u1")))
        assert (await container.repos.usage.summary("u1"))["local_calls"] == 0  # in flight

    assert (await container.repos.usage.summary("u1"))["local_calls"] == 1
