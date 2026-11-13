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
