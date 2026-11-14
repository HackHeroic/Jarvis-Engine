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
* ``draft_store``     — the live ``DraftStore`` wrapping the Supabase client
  (JarvisState and PlanningState). Only ``draft_id`` — a plain string — needs
  to survive the turn; the store itself is re-supplied from ``app.state``.
* ``file_base64`` / ``file_bytes`` / ``file_name`` — the uploaded document in
  both its encoded and decoded forms, plus its name. Not a serialization
  problem, a privacy one: the upload turn's rows would hold the whole document —
  trivially recoverable with ``b64decode`` — in an unencrypted local SQLite file.
* ``conversation_history`` / ``memory_context`` — the turn's window of earlier
  messages and its context note (memories, plate, time). Both are read fresh
  under the turn lock every turn, so a stored copy buys nothing, and it would
  keep the user's words and memories (forgotten ones included) on disk.

Without scrubbing, LangGraph's serializer raises
``TypeError: Type is not msgpack serializable: …`` and the whole turn dies.
They are replaced with ``None`` on the way into SQLite — which touches the write
path only, never the live channel values the running turn reads. ``_load_context``
rebuilds ``user_model`` from the checkpointed ``user_id``; every other one is
re-supplied per turn, the file fields from the request (chat.py) and
``db_client`` by ``knowledge_state_in`` from ``user_model._db``.

The key list is a fast path, not the safety net: any *new* live object added to
any state class would silently reintroduce the crash. So the saver also catches
what the serde rejects, nulls that value, and logs the key name.

Checkpoints are written every step of every turn and nothing reads past the newest,
so a daily ``CheckpointRetentionSweep`` keeps the newest ``CHECKPOINT_KEEP_PER_THREAD``
per thread and deletes the rest (``prune_checkpoints``).
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar, Mapping, Sequence

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import CHECKPOINT_KEEP_PER_THREAD
from app.domain.ports import Sweep

_log = logging.getLogger(__name__)

_TRANSIENT_KEYS = (
    "user_model",
    "progress_callback",
    "progress_queue",
    "db_client",
    "draft_store",
    "file_base64",
    "file_bytes",
    "file_name",
    "conversation_history",
    "memory_context",
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
            # Every value is serialized while building executemany's argument
            # list, so the failure precedes the INSERTs entirely — nothing was
            # written and the retry starts from a clean slate.
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


# Every checkpoint past the newest ``keep`` of its (thread, namespace). Checkpoint ids
# are time-ordered (uuid6), so the newest sort last.
_STALE_CHECKPOINTS = """
    SELECT thread_id, checkpoint_ns, checkpoint_id FROM (
        SELECT thread_id, checkpoint_ns, checkpoint_id,
               ROW_NUMBER() OVER (
                   PARTITION BY thread_id, checkpoint_ns ORDER BY checkpoint_id DESC
               ) AS newest_first
        FROM checkpoints
    ) WHERE newest_first > ?
"""


async def prune_checkpoints(saver: AsyncSqliteSaver, keep_per_thread: int) -> int:
    """Keep the newest ``keep_per_thread`` checkpoints of each (thread, namespace).

    Every older checkpoint is deleted with its ``writes`` rows, in one transaction,
    under the saver's lock. A thread resumes from its newest checkpoint, which stays
    whole. Returns how many checkpoints were deleted. ``keep_per_thread`` below 1
    raises ValueError: it would delete the state a conversation resumes from.
    """
    if keep_per_thread < 1:
        raise ValueError("keep_per_thread must be at least 1")
    await saver.setup()
    async with saver.lock, saver.conn.cursor() as cursor:
        await cursor.execute(
            "DELETE FROM writes WHERE (thread_id, checkpoint_ns, checkpoint_id)"
            f" IN ({_STALE_CHECKPOINTS})",
            (keep_per_thread,),
        )
        await cursor.execute(
            "DELETE FROM checkpoints WHERE (thread_id, checkpoint_ns, checkpoint_id)"
            f" IN ({_STALE_CHECKPOINTS})",
            (keep_per_thread,),
        )
        deleted = cursor.rowcount
        await saver.conn.commit()
    return deleted


class CheckpointRetentionSweep(Sweep):
    """Runs ``prune_checkpoints`` over the whole checkpoint file, once a day.

    Checkpoints are keyed by thread, not read per user, so ``user_ids`` is unused.
    A failure propagates to the scheduler, which logs it by type.
    """

    name: ClassVar[str] = "checkpoint_retention"
    interval: ClassVar[timedelta] = timedelta(hours=24)

    def __init__(
        self, saver: AsyncSqliteSaver, keep_per_thread: int = CHECKPOINT_KEEP_PER_THREAD
    ) -> None:
        self._saver = saver
        self._keep_per_thread = keep_per_thread

    async def run(self, user_ids: list[str], now: datetime) -> int:
        """Prune every thread to its newest checkpoints; return how many were deleted."""
        return await prune_checkpoints(self._saver, self._keep_per_thread)


def make_thread_id(user_id: str, session_id: str) -> str:
    """Checkpoint thread key — user-scoped, never session-only.

    ``_load_context`` trusts the checkpointed ``user_id`` to rebuild the facade,
    so a bare session id would let a resumed turn read (and rebuild) another
    user's state if two users ever shared or guessed a conversation id.
    """
    return f"{user_id}:{session_id}"
