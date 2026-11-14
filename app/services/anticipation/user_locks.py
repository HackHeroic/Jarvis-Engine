"""One lock per user, so a user's anticipation work never runs twice at once.

In memory and process-wide, which is right for the single server process Jarvis
runs as. A user's lock is dropped as soon as nothing holds or awaits it, so the
registry never outgrows the users with work in flight.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class _Entry:
    """A user's lock and how many callers hold or await it."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class UserLocks:
    """A registry of per-user locks; ``hold(user_id)`` serialises one user's callers."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def hold(self, user_id: str) -> AsyncIterator[None]:
        """Hold the user's lock for the block; another caller for them waits, in order.

        Released however the block ends, cancellation included. Not re-entrant.
        """
        entry = self._entries.get(user_id)
        if entry is None:
            entry = self._entries[user_id] = _Entry()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._entries[user_id]
