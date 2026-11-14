"""The usage ledger: every language-model call, recorded for cost and privacy receipts.

``UsageLedger`` listens for ``LLMCallCompleted`` and stores each call's
``LLMUsageEvent``; ``receipt`` answers what ran on the device, what went to the
cloud, and how many PII spans were redacted on the way.

Recording never holds up or breaks the call it describes. ``handle`` hands the
write to a background task and returns at once, and a write that fails because
storage is unreachable is dropped with a single warning — the ledger is
accounting, and an answer is never traded for it.

At most ``MAX_IN_FLIGHT_WRITES`` writes run at once; a call recorded while all of
them are busy (storage hanging) is dropped and counted, so a slow database cannot
pile up unbounded tasks. ``shutdown`` waits a bounded time for the writes in
flight and logs, once, how many records were lost.
"""

import asyncio
import logging
from datetime import datetime

from app.domain.entities import LLMUsageEvent
from app.domain.errors import PersistenceError
from app.domain.events import DomainEvent, EventListener, LLMCallCompleted
from app.domain.ports import UsageRepository

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT_WRITES = 8
SHUTDOWN_DRAIN_TIMEOUT_S = 2.0


class UsageLedger(EventListener):
    """Records each ``LLMCallCompleted`` through a ``UsageRepository``.

    The composition root builds one per process, so "warned once" is once per process.
    """

    def __init__(self, usage: UsageRepository) -> None:
        self._usage = usage
        # The event loop keeps only weak references to tasks; these keep writes alive.
        self._writes: set[asyncio.Task[None]] = set()
        # Never awaited: a write starts only when a slot is free, else it is dropped.
        self._slots = asyncio.Semaphore(MAX_IN_FLIGHT_WRITES)
        self._warned_unreachable = False
        self._reported_loss = False
        self.dropped = 0

    async def handle(self, event: DomainEvent) -> None:
        """Start recording an ``LLMCallCompleted``'s usage in the background; ignore other events."""
        if not isinstance(event, LLMCallCompleted):
            return
        if self._slots.locked():
            self.dropped += 1
            return
        await self._slots.acquire()  # a slot is free, so this does not wait
        write = asyncio.create_task(self._record(event.usage))
        self._writes.add(write)
        write.add_done_callback(self._write_finished)

    def _write_finished(self, write: asyncio.Task[None]) -> None:
        self._writes.discard(write)
        self._slots.release()

    async def drain(self) -> None:
        """Wait until every write started so far has finished."""
        while self._writes:
            await asyncio.wait(set(self._writes))

    async def shutdown(self, timeout_s: float = SHUTDOWN_DRAIN_TIMEOUT_S) -> None:
        """Wait up to ``timeout_s`` for the writes in flight, then log once what was lost.

        Lost means dropped over the in-flight cap, or still unwritten when the wait
        ended (the process is going away and takes those writes with it).
        """
        try:
            await asyncio.wait_for(self.drain(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        lost = self.dropped + len(self._writes)
        if lost and not self._reported_loss:
            self._reported_loss = True
            logger.warning(
                "Usage ledger lost %d LLM call records (%d dropped over the %d-write "
                "limit, %d unwritten at shutdown)",
                lost, self.dropped, MAX_IN_FLIGHT_WRITES, len(self._writes),
            )

    async def receipt(self, user_id: str, since: datetime | None = None) -> dict:
        """The user's calls since ``since`` (all time if None), split on-device / cloud."""
        totals = await self._usage.summary(user_id, since=since)
        return {
            "on_device_calls": totals["local_calls"],
            "cloud_calls": totals["cloud_calls"],
            "pii_redactions": totals["pii_redactions"],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
        }

    async def _record(self, usage: LLMUsageEvent) -> None:
        try:
            await self._usage.record(usage)
        except PersistenceError as exc:
            if not self._warned_unreachable:
                self._warned_unreachable = True
                logger.warning(
                    "Usage ledger cannot reach storage (%s); LLM calls continue but are "
                    "not being recorded (this warning is not repeated)",
                    type(exc).__name__,
                )
        except Exception:
            logger.exception("Usage ledger failed to record a call")
