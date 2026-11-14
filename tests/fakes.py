"""In-memory stand-ins for external services, so tests never touch the network.

`FakeSupabase` implements the slice of the supabase-py chained query builder
that the stores actually call: ``table(name).insert(row).execute()``,
``.select("*").eq(k, v)...execute()``, ``.update(d).eq(...).execute()``,
``.delete().eq(...).execute()``, plus ``.order()`` / ``.limit()``, the
``.neq()`` / ``.in_()`` / ``.gte()`` / ``.lte()`` / ``.lt()`` / ``.gt()`` filters,
``.is_(column, "null")``, and
``rpc("accept_draft" | "match_memories", params).execute()``. It records the
columns every ``select`` names (``FakeSupabase.selects``) and enforces the
unique keys a repository maps to an error on a plain write: user_memories' primary
key and its ``(user_id, pattern_key) where pattern_key is not null`` index.

It is deliberately a real (if tiny) implementation rather than a MagicMock:
a MagicMock returns a canned payload regardless of the filters passed, so it
cannot catch a missing ``.eq("user_id", ...)`` — exactly the class of bug the
house security rule ("every Supabase query must filter by user_id") exists to
prevent. Filtering here is real, so user-isolation assertions have teeth.
"""

import copy
import hashlib
import json
import math
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.domain.ports import TextEmbedder
from app.utils.embedding import cosine_similarity

# Canned LLM output shared by every test that mocks the model boundary.
# Asserting this sentinel (rather than merely `is not None`) matters because
# each module swallows LLM exceptions and substitutes a generic fallback
# string — so a weaker assertion passes even when the call blew up.
CANNED_LLM_REPLY = "Canned LLM reply, sir."


class _Result:
    """Mirrors the supabase-py APIResponse surface the stores read."""

    def __init__(self, data: Any):
        self.data = data


def _comparable(value: Any) -> Any:
    """Postgres compares timestamptz by instant, not by text: parse ISO strings.

    A naive timestamp reads as UTC, as it does in a Supabase (UTC) session.
    Anything that is not a datetime is compared as-is.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _compare(op: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    """A SQL comparison: NULL on either side never matches."""

    def check(cell: Any, value: Any) -> bool:
        if cell is None or value is None:
            return False
        return op(_comparable(cell), _comparable(value))

    return check


# Unique keys the fake enforces on table().insert/update, as {table: ((name, columns), ...)},
# checked in order (Postgres checks the primary key first). A key only binds rows
# where every column is non-NULL (a partial index on "where <column> is not null"
# behaves the same here).
_UNIQUE_INDEXES = {
    "user_memories": (
        ("user_memories_pkey", ("id",)),
        # 20261115000002_memory_lifecycle_and_vectors.sql
        ("idx_user_memories_user_pattern_key", ("user_id", "pattern_key")),
    ),
}


def _unique_violation(index: str) -> Exception:
    return Exception(f'duplicate key value violates unique constraint "{index}"')


class _Query:
    def __init__(
        self,
        rows: list[dict],
        table_name: str,
        selects: list[tuple[str, tuple[str, ...]]] | None = None,
        max_rows: int | None = None,
    ):
        self._rows = rows
        self._table = table_name
        self._selects = selects if selects is not None else []
        self._max_rows = max_rows
        self._op: str | None = None
        self._payload: Any = None
        self._filters: list[tuple[str, Any]] = []
        self._predicates: list[Callable[[dict], bool]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    # ── query construction ──────────────────────────────────
    def insert(self, payload: dict | list[dict]) -> "_Query":
        self._op, self._payload = "insert", payload
        return self

    def select(self, *columns: str) -> "_Query":
        self._op = "select"
        names = tuple(name.strip() for spec in columns for name in spec.split(","))
        self._selects.append((self._table, names))
        return self

    def update(self, payload: dict) -> "_Query":
        self._op, self._payload = "update", payload
        return self

    def delete(self) -> "_Query":
        self._op = "delete"
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self._filters.append((column, value))
        return self

    def _where(self, column: str, value: Any, check: Callable[[Any, Any], bool]) -> "_Query":
        self._predicates.append(lambda row: check(row.get(column), value))
        return self

    def neq(self, column: str, value: Any) -> "_Query":
        return self._where(column, value, _compare(lambda a, b: a != b))

    def in_(self, column: str, values: list) -> "_Query":
        return self._where(
            column, list(values), lambda cell, vals: cell is not None and cell in vals
        )

    def gte(self, column: str, value: Any) -> "_Query":
        return self._where(column, value, _compare(lambda a, b: a >= b))

    def lte(self, column: str, value: Any) -> "_Query":
        return self._where(column, value, _compare(lambda a, b: a <= b))

    def lt(self, column: str, value: Any) -> "_Query":
        return self._where(column, value, _compare(lambda a, b: a < b))

    def gt(self, column: str, value: Any) -> "_Query":
        return self._where(column, value, _compare(lambda a, b: a > b))

    def is_(self, column: str, value: Any) -> "_Query":
        """PostgREST ``is``: only the SQL NULL test is modelled."""
        if value not in ("null", None):
            raise NotImplementedError(f"FakeSupabase models is_(..., 'null') only, not {value!r}")
        return self._where(column, None, lambda cell, _value: cell is None)

    def order(self, column: str, desc: bool = False) -> "_Query":
        self._order = (column, desc)
        return self

    def limit(self, count: int) -> "_Query":
        self._limit = count
        return self

    # ── execution ───────────────────────────────────────────
    def _check_unique(self, candidate: dict, others: list[dict]) -> None:
        """Raise as Postgres would if ``candidate`` repeats a unique key held in ``others``."""
        for index, columns in _UNIQUE_INDEXES.get(self._table, ()):
            key = tuple(candidate.get(column) for column in columns)
            if None in key:
                continue
            if any(tuple(row.get(column) for column in columns) == key for row in others):
                raise _unique_violation(index)

    def _matches(self, row: dict) -> bool:
        return all(row.get(col) == val for col, val in self._filters) and all(
            predicate(row) for predicate in self._predicates
        )

    def execute(self) -> _Result:
        if self._op == "insert":
            incoming = (
                self._payload if isinstance(self._payload, list) else [self._payload]
            )
            inserted = []
            for row in incoming:
                self._check_unique(row, self._rows)
                # The real table defaults created_at; emulate so .order() works.
                # Deep copies: a real client serialises the payload, so a caller
                # mutating its dict afterwards cannot reach the stored row.
                stored = {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **copy.deepcopy(row),
                }
                self._rows.append(stored)
                inserted.append(copy.deepcopy(stored))
            return _Result(inserted)

        matched = [r for r in self._rows if self._matches(r)]

        if self._op == "update":
            for row in matched:
                others = [r for r in self._rows if r is not row]
                self._check_unique({**row, **self._payload}, others)
            for row in matched:
                row.update(copy.deepcopy(self._payload))
        elif self._op == "delete":
            for row in matched:
                self._rows.remove(row)

        if self._order:
            column, desc = self._order
            matched.sort(key=lambda r: r.get(column) or "", reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        if self._op == "select" and self._max_rows is not None:
            # PostgREST's cap: a read silently answers at most max_rows rows.
            matched = matched[: self._max_rows]

        # Return copies so callers mutating results can't corrupt the store.
        return _Result([copy.deepcopy(r) for r in matched])


class _RpcCall:
    def __init__(self, run: Callable[[], _Result]):
        self._run = run

    def execute(self) -> _Result:
        return self._run()


class FakeSupabase:
    """Chainable in-memory Supabase stand-in. No network, no MagicMock."""

    def __init__(self, seed: dict[str, list[dict]] | None = None):
        self.rows: dict[str, list[dict]] = {
            name: list(rows) for name, rows in (seed or {}).items()
        }
        # Stands in for the row lock (`select ... for update`) the real
        # function takes: repositories run calls on worker threads.
        self._rpc_lock = threading.Lock()
        # (table, column names) for every select, in call order.
        self.selects: list[tuple[str, tuple[str, ...]]] = []
        # The most rows one read returns, after order and limit, as [api] max_rows
        # in supabase/config.toml makes PostgREST do; None means no cap.
        self.max_rows: int | None = 1000

    def table(self, name: str) -> _Query:
        return _Query(self.rows.setdefault(name, []), name, self.selects, self.max_rows)

    def rpc(self, name: str, params: dict) -> _RpcCall:
        # The real client sends params as JSON; round-trip so a value the
        # real call could not serialise fails here too.
        params = json.loads(json.dumps(params))
        if name == "accept_draft":
            return _RpcCall(lambda: self._accept_draft(**params))
        if name == "match_memories":
            return _RpcCall(lambda: self._match_memories(**params))
        raise NotImplementedError(f"FakeSupabase has no rpc {name!r}")

    def _match_memories(
        self,
        p_user_id: str,
        p_query: list[float],
        p_statuses: list[str],
        p_types: list[str] | None,
        p_limit: int,
        p_min_similarity: float,
    ) -> _Result:
        """Mirror of the match_memories SQL function: cosine over the stored lists.

        Same filters (user, status, type when given, has an embedding, not superseded),
        most similar first, at most ``p_limit``; every column but ``embedding``, plus
        ``similarity``.
        """
        found = []
        for row in self.rows.setdefault("user_memories", []):
            if (
                row.get("user_id") != p_user_id
                or row.get("status") not in p_statuses
                or (p_types is not None and row.get("memory_type") not in p_types)
                or row.get("embedding") is None
                or row.get("superseded_by") is not None
            ):
                continue
            similarity = cosine_similarity(row["embedding"], p_query)
            if similarity >= p_min_similarity:
                result = {k: copy.deepcopy(v) for k, v in row.items() if k != "embedding"}
                found.append({**result, "similarity": similarity})
        found.sort(key=lambda r: r["similarity"], reverse=True)
        return _Result(found[:p_limit])

    def _accept_draft(self, p_draft_id: str, p_user_id: str, p_rows: list[dict]) -> _Result:
        """Mirror of the accept_draft SQL function (v2): one transaction, all or nothing.

        It replaces the user's pending rows, plus their missed rows whose task_id
        appears in ``p_rows`` (20261115000001_accept_draft_reslots_missed.sql).

        Also enforces the migrated table's ``UNIQUE (user_id, task_id)``: a row whose
        task_id one of the user's surviving rows, or an earlier row of the same call,
        already holds fails the whole call, as the real insert would.
        Only this path checks it; ``table().insert`` does not.
        """
        with self._rpc_lock:
            draft = next(
                (
                    r
                    for r in self.rows.setdefault("draft_schedules", [])
                    if r.get("id") == p_draft_id and r.get("user_id") == p_user_id
                ),
                None,
            )
            if draft is None:
                raise Exception("DRAFT_NOT_FOUND")
            now = datetime.now(timezone.utc)
            expires_at = draft.get("expires_at")
            if draft.get("status") not in ("pending", "modified") or (
                expires_at is not None and _comparable(expires_at) < now
            ):
                raise Exception("DRAFT_NOT_OPEN")

            tasks = self.rows.setdefault("user_tasks", [])
            reslotted = {row.get("task_id") for row in p_rows}
            remaining = [
                r
                for r in tasks
                if not (
                    r.get("user_id") == p_user_id
                    and (
                        r.get("status") == "pending"
                        or (r.get("status") == "missed" and r.get("task_id") in reslotted)
                    )
                )
            ]
            # Checked before anything is written, so a violation changes nothing.
            taken = {r.get("task_id") for r in remaining if r.get("user_id") == p_user_id}
            for row in p_rows:
                if row.get("task_id") in taken:
                    # The constraint's name in 20261114000001_capstone_lld.sql.
                    raise Exception(
                        'duplicate key value violates unique constraint '
                        '"user_tasks_user_id_task_id_key"'
                    )
                taken.add(row.get("task_id"))
            tasks[:] = remaining
            for row in p_rows:
                # Table defaults the explicit column list leaves to Postgres.
                tasks.append({"id": str(uuid.uuid4()), "created_at": now.isoformat(), **row})
            draft["status"] = "accepted"
            # The accepted plan replaces every other plan the user had open.
            for other in self.rows["draft_schedules"]:
                if (
                    other.get("user_id") == p_user_id
                    and other is not draft
                    and other.get("status") in ("pending", "modified")
                ):
                    other["status"] = "superseded"
            return _Result(len(p_rows))


def bag_of_words_embedding(text: str, dims: int = 128) -> list[float]:
    """Deterministic stand-in for the sentence embedder: hashed word counts."""
    vector = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class BagOfWordsEmbedder(TextEmbedder):
    """A ``TextEmbedder`` with no model: texts sharing words point the same way.

    Defaults to the production embedder's 384 dimensions, the width of the
    user_memories.embedding column.
    """

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [bag_of_words_embedding(text, self.dimensions) for text in texts]


class FakeDBClient:
    """Stand-in for ``DatabaseClient`` — only the ``.supabase`` attribute is read."""

    def __init__(self, supabase: FakeSupabase | None = None):
        self.supabase = supabase if supabase is not None else FakeSupabase()

    async def check_connection(self) -> bool:
        return True


def make_jarvis_state(**overrides: Any) -> dict:
    """A complete ``JarvisState`` with sane defaults; override any key.

    Every JarvisState key is present so tests exercise the same shape the
    endpoint builds (LangGraph only checkpoints channels it has seen).
    """
    from app.orchestrator.state import ConversationPhase, NegotiationPhase

    base: dict = {
        "user_id": "test_user",
        "user_model": None,
        "user_message": "test",
        "file_base64": None,
        "file_media_type": None,
        "file_name": None,
        "brain_dump": None,
        "intent": "PLAN_DAY",
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_store": None,
        "draft_id": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "anti_guilt_message": None,
        "missed_deadlines": None,
        "thinking_process": None,
        "response_message": None,
        "conversation_phase": ConversationPhase.CHAT,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "needs_consent": None,
        "error": None,
        "conversation_history": [],
        "memory_context": "",
        "progress_callback": None,
        "progress_queue": None,
        "trivial_input": None,
        "force_cloud_request": None,
    }
    base.update(overrides)
    return base
