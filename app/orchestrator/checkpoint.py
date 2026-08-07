"""SQLite checkpointer wiring — drops the live per-turn objects before write.

Several state keys hold objects that exist only for the duration of one request
and are not msgpack-serializable. They span the parent graph *and* the module
sub-graphs, because a sub-graph compiled without its own checkpointer inherits
the parent's (module_framework.py:226) — its channels are written under a
nested ``checkpoint_ns`` through this same saver:

* ``user_model``      — a facade holding asyncio locks and the Supabase client
* ``progress_callback`` — a closure over the SSE queue
* ``progress_queue``  — the ``asyncio.Queue`` itself
* ``db_client``       — the live ``DatabaseClient`` (KnowledgeState)
* ``file_bytes``      — the decoded upload; re-read from the request each turn,
  and it has no business sitting unencrypted in a local SQLite file

Without scrubbing, LangGraph's serializer raises
``TypeError: Type is not msgpack serializable: …`` and the whole turn dies.
They are replaced with ``None`` on the way into SQLite; ``_load_context``
rebuilds ``user_model`` from the checkpointed ``user_id``, and every other one
is re-supplied per turn (``knowledge_state_in`` re-reads ``db_client`` from
``user_model._db`` and re-decodes ``file_bytes`` from ``file_base64``).

The key list is a fast path, not the safety net: any *new* live object added to
any state class would silently reintroduce the crash. So the saver also catches
what the serde rejects, nulls that value, and logs the key name.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_log = logging.getLogger(__name__)

_TRANSIENT_KEYS = (
    "user_model",
    "progress_callback",
    "progress_queue",
    "db_client",
    "file_bytes",
)


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

    def _make_serializable(self, key: str, value: Any) -> Any:
        """Null whatever the serde refuses, naming it in the log.

        The last line of defence behind ``_TRANSIENT_KEYS``: a live object added
        to any state class tomorrow degrades to a dropped channel instead of a
        500 on the user's turn. Mappings are salvaged item-by-item first, so one
        bad entry does not take the whole ``__start__`` input with it.
        """
        try:
            self.serde.dumps_typed(value)
            return value
        except Exception:
            if isinstance(value, Mapping):
                salvaged = {k: self._make_serializable(k, v) for k, v in value.items()}
                try:
                    self.serde.dumps_typed(salvaged)
                    return salvaged
                except Exception:
                    pass
            _log.warning(
                "Checkpoint: dropping non-serializable state key %r (%s) — "
                "add it to _TRANSIENT_KEYS if it is a live per-turn object",
                key,
                type(value).__name__,
            )
            return None

    def _sanitize(self, values: Mapping[str, Any]) -> dict:
        return {key: self._make_serializable(key, value) for key, value in values.items()}

    async def aput(self, config, checkpoint, metadata, new_versions):
        if "channel_values" in checkpoint:
            checkpoint = {
                **checkpoint,
                "channel_values": scrub_transients(checkpoint["channel_values"]),
            }
        try:
            return await super().aput(config, checkpoint, metadata, new_versions)
        except TypeError:
            # dumps_typed() runs before the INSERT, so nothing was written yet.
            if "channel_values" not in checkpoint:
                raise
            checkpoint = {
                **checkpoint,
                "channel_values": self._sanitize(checkpoint["channel_values"]),
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
        try:
            return await super().aput_writes(config, scrubbed, task_id, task_path)
        except TypeError:
            # The INSERTs are OR REPLACE / OR IGNORE on the same keys, so a retry
            # after a partial executemany is idempotent.
            sanitized = [
                (channel, self._make_serializable(channel, value)) for channel, value in scrubbed
            ]
            return await super().aput_writes(config, sanitized, task_id, task_path)


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
