"""The usage ledger: every language-model call, recorded for cost and privacy receipts.

``UsageLedger`` listens for ``LLMCallCompleted`` and stores each call's
``LLMUsageEvent``; ``receipt`` answers what ran on the device, what went to the
cloud, and how many PII spans were redacted on the way.

Recording never holds up or breaks the call it describes. ``handle`` hands the
write to a background task and returns at once, and a write that fails because
storage is unreachable is dropped with a single warning — the ledger is
accounting, and an answer is never traded for it.
"""

import asyncio
import logging
from datetime import datetime

from app.domain.entities import LLMUsageEvent
from app.domain.errors import PersistenceError
from app.domain.events import DomainEvent, EventListener, LLMCallCompleted
from app.domain.ports import UsageRepository

logger = logging.getLogger(__name__)


class UsageLedger(EventListener):
    """Records each ``LLMCallCompleted`` through a ``UsageRepository``.

    The composition root builds one per process, so "warned once" is once per process.
    """

    def __init__(self, usage: UsageRepository) -> None:
        self._usage = usage
        # The event loop keeps only weak references to tasks; these keep writes alive.
        self._writes: set[asyncio.Task[None]] = set()
        self._warned_unreachable = False

    async def handle(self, event: DomainEvent) -> None:
        """Start recording an ``LLMCallCompleted``'s usage in the background; ignore other events."""
        if not isinstance(event, LLMCallCompleted):
            return
        write = asyncio.create_task(self._record(event.usage))
        self._writes.add(write)
        write.add_done_callback(self._writes.discard)

    async def drain(self) -> None:
        """Wait until every write started so far has finished."""
        while self._writes:
            await asyncio.wait(set(self._writes))

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
        except PersistenceError:
            if not self._warned_unreachable:
                self._warned_unreachable = True
                logger.warning(
                    "Usage ledger cannot reach storage; LLM calls continue but are not "
                    "being recorded (this warning is not repeated)"
                )
        except Exception:
            logger.exception("Usage ledger failed to record a call")
