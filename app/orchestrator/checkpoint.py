"""SQLite checkpointer wiring — drops the live per-turn objects before write.

Three ``JarvisState`` keys hold objects that exist only for the duration of one
request and are not msgpack-serializable:

* ``user_model``      — a facade holding asyncio locks and the Supabase client
* ``progress_callback`` — a closure over the SSE queue
* ``progress_queue``  — the ``asyncio.Queue`` itself

Without scrubbing, LangGraph's serializer raises
``TypeError: Type is not msgpack serializable: function`` and the whole turn
dies. They are replaced with ``None`` on the way into SQLite; ``_load_context``
rebuilds ``user_model`` from the checkpointed ``user_id``, and the endpoint
supplies a fresh callback/queue on every turn.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_TRANSIENT_KEYS = ("user_model", "progress_callback", "progress_queue")


def _scrub_value(key: str, value: Any) -> Any:
    if key in _TRANSIENT_KEYS:
        return None
    # LangGraph's first checkpoint of a turn stores the whole input mapping
    # under a single ``__start__`` channel, so scrubbing top-level channel
    # names alone would leave the callable buried one level down.
    if isinstance(value, Mapping) and any(k in value for k in _TRANSIENT_KEYS):
        return scrub_transients(value)
    return value


def scrub_transients(values: Mapping[str, Any]) -> dict:
    """Return ``values`` with every transient replaced by ``None``."""
    return {key: _scrub_value(key, value) for key, value in values.items()}


class ScrubbingSqliteSaver(AsyncSqliteSaver):
    """``AsyncSqliteSaver`` that never tries to serialize the live objects."""

    async def aput(self, config, checkpoint, metadata, new_versions):
        if "channel_values" in checkpoint:
            checkpoint = {
                **checkpoint,
                "channel_values": scrub_transients(checkpoint["channel_values"]),
            }
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        # Node outputs land here channel-by-channel — `_load_context` returning a
        # freshly hydrated UserModel is exactly such a write.
        scrubbed = [(channel, _scrub_value(channel, value)) for channel, value in writes]
        return await super().aput_writes(config, scrubbed, task_id, task_path)


@asynccontextmanager
async def open_checkpointer(db_path: str) -> AsyncIterator[ScrubbingSqliteSaver]:
    """Open a scrubbing SQLite checkpointer at ``db_path``, closing it on exit.

    Parent directories are created; the app uses ``data/checkpoints.sqlite``
    (gitignored) and tests use a tmp_path.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        yield ScrubbingSqliteSaver(conn=conn)
    finally:
        await conn.close()


def make_thread_id(user_id: str, session_id: str) -> str:
    """Checkpoint thread key — user-scoped, never session-only.

    ``_load_context`` trusts the checkpointed ``user_id`` to rebuild the facade,
    so a bare session id would let a resumed turn read (and rebuild) another
    user's state if two users ever shared or guessed a conversation id.
    """
    return f"{user_id}:{session_id}"
