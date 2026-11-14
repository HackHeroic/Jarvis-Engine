"""One turn at a time per conversation.

Two browser tabs on one chat session share a checkpoint thread. Run at the same
time, both turns start from the same checkpoint and the one that finishes last
overwrites what the other wrote — a draft review the first turn opened simply
vanishes. ``thread_lock`` makes the second turn wait until the first one's graph
run is over, then run against what it left.

The registry is process-wide and in memory, which is right for the single server
process Jarvis runs as. A thread's lock is dropped as soon as no turn holds or
awaits it, so the registry never outgrows the conversations in flight.

Waiting is bounded: a turn still queued after ``TURN_LOCK_WAIT_SECONDS`` gives up
with a ``ConflictError`` rather than hang its stream indefinitely.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.domain.errors import ConflictError

# How long a turn waits for the one before it on the same conversation.
TURN_LOCK_WAIT_SECONDS = 120.0


class _TurnLock:
    """A thread's lock and how many turns hold or await it."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_locks: dict[str, _TurnLock] = {}


@asynccontextmanager
async def thread_lock(thread_id: str) -> AsyncIterator[None]:
    """Hold ``thread_id``'s turn lock for the block; another turn on it waits, in arrival order.

    Released however the block ends — normally, by an error, or by cancellation
    (the client disconnected). Not re-entrant: nothing inside a turn may take its
    own thread's lock again.

    Raises ``ConflictError`` if the lock is still held after
    ``TURN_LOCK_WAIT_SECONDS``; the block then never runs.
    """
    entry = _locks.get(thread_id)
    if entry is None:
        entry = _locks[thread_id] = _TurnLock()
    entry.users += 1
    try:
        try:
            await asyncio.wait_for(entry.lock.acquire(), timeout=TURN_LOCK_WAIT_SECONDS)
        except TimeoutError:
            raise ConflictError(
                "Your previous message in this conversation is still being answered. "
                "Send this one again once that reply has arrived."
            ) from None
        try:
            yield
        finally:
            entry.lock.release()
    finally:
        entry.users -= 1
        if entry.users == 0:
            del _locks[thread_id]
