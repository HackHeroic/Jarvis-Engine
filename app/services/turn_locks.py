"""One turn at a time per conversation.

Two browser tabs on one chat session share a checkpoint thread. Run at the same
time, both turns start from the same checkpoint and the one that finishes last
overwrites what the other wrote — a draft review the first turn opened simply
vanishes. ``thread_lock`` makes the second turn wait until the first one's graph
run is over, then run against what it left.

The registry is process-wide and in memory, which is right for the single server
process Jarvis runs as. A thread's lock is dropped as soon as no turn holds or
awaits it, so the registry never outgrows the conversations in flight.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


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
    """
    entry = _locks.get(thread_id)
    if entry is None:
        entry = _locks[thread_id] = _TurnLock()
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0:
            del _locks[thread_id]
