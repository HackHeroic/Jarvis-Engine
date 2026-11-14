"""Background work, at most one run per user at a time.

``PerUserSingleFlight`` is shared by every listener that reacts to a user's events in
the background (PEARL after an outcome, the pattern proposal after PEARL finds a
pattern): whoever published the event never waits for the run; requests that arrive
while a run is in flight ask for one more run after it, however many there were; a
failed run is logged by its type, never its text; and on shutdown the runs in flight
get a grace period, then are cancelled.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class PerUserSingleFlight:
    """Runs ``run(user_id)`` in the background, one run per user at a time."""

    def __init__(self, run: Callable[[str], Awaitable[None]], *, label: str) -> None:
        """``label`` names the work in the log line a failed run leaves."""
        self._run_for = run
        self._label = label
        self._running: dict[str, asyncio.Task] = {}
        self._dirty: set[str] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether ``shutdown`` was called: no request starts a run any more."""
        return self._closed

    def request(self, user_id: str) -> None:
        """Run for ``user_id`` now, or once more after the run in flight finishes."""
        if self._closed:
            return
        if user_id in self._running:
            self._dirty.add(user_id)
            return
        self._running[user_id] = asyncio.create_task(self._loop(user_id))

    async def settle(self) -> None:
        """Wait until no run is in flight."""
        while self._running:
            await asyncio.wait(set(self._running.values()))

    async def shutdown(self, grace_s: float) -> None:
        """Take no more requests; give the runs in flight ``grace_s``, then cancel the rest
        and wait for them to end."""
        self._closed = True
        runs = set(self._running.values())
        if not runs:
            return
        _, unfinished = await asyncio.wait(runs, timeout=grace_s)
        for run in unfinished:
            run.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)

    async def _loop(self, user_id: str) -> None:
        try:
            while True:
                self._dirty.discard(user_id)
                try:
                    await self._run_for(user_id)
                except Exception as exc:  # the next request tries again
                    logger.warning(
                        "%s for user %s could not finish (%s)",
                        self._label,
                        user_id,
                        type(exc).__name__,
                    )
                if user_id not in self._dirty:
                    return
        finally:
            self._running.pop(user_id, None)
