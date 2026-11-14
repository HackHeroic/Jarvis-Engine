"""The anticipation scheduler: one in-process loop that runs the sweeps, then the triggers.

The lifespan starts it when ``ANTICIPATION_ENABLED``. Every ``tick_s`` seconds (the
first after a full ``tick_s``) a tick runs each sweep whose interval has passed over
every user, then each user's due triggers, one user at a time, with that user's
model calls attributed to them. Nothing raised inside a tick reaches the loop: a
sweep's or a user's failure is logged by exception type (never its text, which can
carry a connection string) and the tick carries on. Ticks never overlap.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

import app.core.config as config
from app.core.model_router import attribute_llm_calls
from app.domain.ports import ProfileRepository, Sweep
from app.services.anticipation.service import AnticipationService
from app.services.task_service import utc_now

logger = logging.getLogger(__name__)


class AnticipationScheduler:
    """Ticks the anticipation work on the app's event loop until stopped."""

    def __init__(
        self,
        service: AnticipationService,
        profiles: ProfileRepository,
        sweeps: list[Sweep],
        *,
        tick_s: float = config.ANTICIPATION_TICK_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._service = service
        self._profiles = profiles
        self._sweeps = list(sweeps)
        self._tick_s = tick_s
        self._clock = clock
        self._last_swept: dict[str, datetime] = {}
        self._tick_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def tick(self, now: datetime) -> dict:
        """Run the sweeps that are due, then every user's due triggers, as of ``now``.

        Returns ``{"sweeps": {name: items changed}, "outcomes": n, "errors": n}``:
        the sweeps that ran, how many trigger runs finished, and how many failures
        were logged (a sweep, a trigger, or a user who could not be read). A call
        made while a tick runs waits for it to finish.
        """
        async with self._tick_lock:
            report: dict = {"sweeps": {}, "outcomes": 0, "errors": 0}
            try:
                user_ids = await self._profiles.list_user_ids()
            except Exception as exc:  # the store is down: try again next tick
                logger.warning(
                    "Anticipation tick skipped: could not list users (%s)", type(exc).__name__
                )
                report["errors"] += 1
                return report
            await self._run_sweeps(user_ids, now, report)
            await self._run_triggers(user_ids, now, report)
            return report

    async def _run_sweeps(self, user_ids: list[str], now: datetime, report: dict) -> None:
        for sweep in self._sweeps:
            last = self._last_swept.get(sweep.name)
            if last is not None and now - last < sweep.interval:
                continue
            self._last_swept[sweep.name] = now
            try:
                report["sweeps"][sweep.name] = await sweep.run(user_ids, now)
            except Exception as exc:  # one sweep's failure never stops the others
                logger.warning("Sweep %s could not finish (%s)", sweep.name, type(exc).__name__)
                report["errors"] += 1

    async def _run_triggers(self, user_ids: list[str], now: datetime, report: dict) -> None:
        for user_id in user_ids:
            end_attribution = attribute_llm_calls(user_id, None)
            try:
                outcomes = await self._service.run_due(user_id, now)
                failed = sum(1 for outcome in outcomes if outcome.status == "failed")
                report["outcomes"] += len(outcomes) - failed
                report["errors"] += failed
            except Exception as exc:  # one user's failure never stops the others
                logger.warning(
                    "Anticipation for user %s could not finish (%s)", user_id, type(exc).__name__
                )
                report["errors"] += 1
            finally:
                end_attribution()

    def start(self) -> None:
        """Start ticking on the running event loop; a no-op while already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(
            self._loop(), name="anticipation-scheduler"
        )
        logger.info("Anticipation scheduler started: a tick every %ss", self._tick_s)

    async def stop(self) -> None:
        """Cancel the loop, and any tick in progress, and wait for it to end."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # stop() itself is being cancelled: let that through

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                await self.tick(self._clock())
            except Exception as exc:  # tick() catches its own; nothing may end the loop
                logger.error("Anticipation tick failed (%s)", type(exc).__name__)
