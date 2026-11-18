"""In-memory stand-ins for external services, so tests never touch the network.

`FakeSupabase` implements the slice of the supabase-py chained query builder
that the stores actually call: ``table(name).insert(row).execute()``,
``.upsert(rows, on_conflict="a,b").execute()``,
``.select("*").eq(k, v)...execute()``, ``.update(d).eq(...).execute()``,
``.delete().eq(...).execute()``, plus ``.order()`` / ``.limit()``, the
``.neq()`` / ``.in_()`` / ``.gte()`` / ``.lte()`` / ``.lt()`` / ``.gt()`` filters,
``.is_(column, "null")``, and
``rpc("accept_draft" | "match_memories", params).execute()``. It records the
columns every ``select`` names (``FakeSupabase.selects``) and enforces the
unique keys a repository maps to an error on a plain write: user_memories' primary
key and its ``(user_id, pattern_key) where pattern_key is not null`` index.
``FakeSupabase.storage`` is a ``FakeStorage``: the slice of storage3's sync API the
file store calls (buckets, upload, download, info, remove, list), raising storage3's
own ``StorageApiError`` with the status strings the live service sends.

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
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from storage3.exceptions import StorageApiError

from app.domain.clock import utc_now
from app.domain.entities import LLMLocation
from app.domain.errors import LLMUnavailableError
from app.domain.ports import LLMProvider, LLMRequest, TextEmbedder
from app.infrastructure.llm.transport import strip_fences, translated_errors
from app.utils.embedding import cosine_similarity

if TYPE_CHECKING:
    from app.container import AppContainer

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


# NOT NULL column defaults the fake fills on insert when a row leaves them out, as {table:
# {column: value}}, so a row written without them reads back through a repository as it
# would from the real table. user_tasks: 20261114000001_capstone_lld.sql; user_memories:
# 20261115000002_memory_lifecycle_and_vectors.sql (rows a test inserts without them).
_COLUMN_DEFAULTS = {
    "user_tasks": {"duration_minutes": 25, "difficulty_weight": 0.5},
    "user_memories": {"status": "active", "provenance": "said"},
}


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
        self._orders: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None

    # ── query construction ──────────────────────────────────
    def insert(self, payload: dict | list[dict]) -> "_Query":
        self._op, self._payload = "insert", payload
        return self

    def upsert(self, payload: dict | list[dict], on_conflict: str = "") -> "_Query":
        """Insert each row, or merge it into the row holding the same ``on_conflict``
        columns (PostgREST's merge-duplicates): the columns it names replace that row's."""
        self._op, self._payload = "upsert", payload
        self._conflict = tuple(c.strip() for c in on_conflict.split(",") if c.strip())
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
        """Each call adds a sort key after the earlier ones, as PostgREST's order does."""
        self._orders.append((column, desc))
        return self

    def limit(self, count: int) -> "_Query":
        self._limit = count
        return self

    def range(self, start: int, end: int) -> "_Query":
        """Rows ``start`` to ``end`` inclusive (offset and limit), before the max_rows cap."""
        self._range = (start, end)
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

    def _upsert(self) -> _Result:
        incoming = self._payload if isinstance(self._payload, list) else [self._payload]
        stored_rows = []
        for row in incoming:
            row = json.loads(json.dumps(row))  # sent as JSON, as the real client does
            held = next(
                (
                    r for r in self._rows
                    if self._conflict and all(r.get(c) == row.get(c) for c in self._conflict)
                ),
                None,
            )
            if held is None:
                held = {
                    "id": str(uuid.uuid4()),
                    "created_at": utc_now().isoformat(),
                    **_COLUMN_DEFAULTS.get(self._table, {}),
                }
                self._rows.append(held)
            held.update(copy.deepcopy(row))
            stored_rows.append(copy.deepcopy(held))
        return _Result(stored_rows)

    def execute(self) -> _Result:
        if self._op == "upsert":
            return self._upsert()
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
                    "created_at": utc_now().isoformat(),
                    **_COLUMN_DEFAULTS.get(self._table, {}),
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

        for column, desc in reversed(self._orders):  # stable: the first key decides first
            matched.sort(key=lambda r, column=column: r.get(column) or "", reverse=desc)
        if self._range is not None:
            start, end = self._range
            matched = matched[start : end + 1]
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


class _FakeBucketFiles:
    """One bucket's file API (storage3's ``SyncBucketProxy``)."""

    def __init__(self, storage: "FakeStorage", bucket: str) -> None:
        self._storage = storage
        self._bucket = bucket

    def _objects(self) -> dict[str, dict]:
        self._storage.check_up()
        if self._bucket not in self._storage.buckets:
            raise StorageApiError("Bucket not found", "Bucket not found", "404")
        return self._storage.objects.setdefault(self._bucket, {})

    def _held(self, path: str) -> dict:
        held = self._objects().get(path)
        if held is None:
            raise StorageApiError("Object not found", "not_found", "404")
        return held

    def upload(self, path: str, file: bytes, file_options: dict | None = None) -> Any:
        options = file_options or {}
        objects = self._objects()
        limit = self._storage.buckets[self._bucket].get("file_size_limit")
        if limit is not None and len(file) > limit:
            raise StorageApiError(
                "The object exceeded the maximum allowed size", "Payload too large", "413"
            )
        if path in objects and str(options.get("upsert", "false")).lower() != "true":
            raise StorageApiError("The resource already exists", "Duplicate", "409")
        objects[path] = {
            "content": bytes(file),
            "content_type": options.get("content-type", "text/plain;charset=UTF-8"),
            "id": str(uuid.uuid4()),
        }
        return SimpleNamespace(path=path, Key=f"{self._bucket}/{path}")

    def download(self, path: str) -> bytes:
        return self._held(path)["content"]

    def info(self, path: str) -> dict:
        held = self._held(path)
        return {
            "id": held["id"], "name": path, "bucket_id": self._bucket,
            "content_type": held["content_type"], "size": len(held["content"]),
            "metadata": {"mimetype": held["content_type"], "size": len(held["content"])},
        }

    def remove(self, paths: list[str]) -> list[dict]:
        objects = self._objects()
        return [{"name": p} for p in paths if objects.pop(p, None) is not None]

    def list(self, path: str | None = None, options: dict | None = None) -> list[dict]:
        """The folder's direct entries by name: files with an id, sub-folders without."""
        options = options or {}
        self._storage.listings += 1
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries: dict[str, dict] = {}
        for key, held in self._objects().items():
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            name, _, deeper = rest.partition("/")
            entries[name] = {"name": name, "id": None if deeper else held["id"]}
        ordered = [entries[name] for name in sorted(entries)]
        offset = options.get("offset", 0)
        return ordered[offset : offset + options.get("limit", 100)]


class FakeStorage:
    """Supabase Storage (storage3's ``SyncStorageClient``) over dicts.

    ``down = True`` makes every call fail as an unreachable store does. ``calls`` counts
    the folder listings, so a test can see one listing serve a whole page.
    """

    def __init__(self) -> None:
        self.buckets: dict[str, dict] = {}
        self.objects: dict[str, dict[str, dict]] = {}
        self.down = False
        self.listings = 0

    def check_up(self) -> None:
        if self.down:
            raise ConnectionError("[SSL] could not reach storage.internal")

    def get_bucket(self, id: str) -> SimpleNamespace:
        self.check_up()
        if id not in self.buckets:
            raise StorageApiError("Bucket not found", "Bucket not found", "404")
        return SimpleNamespace(**self.buckets[id])

    def create_bucket(self, id: str, name: str | None = None, options: dict | None = None) -> dict:
        self.check_up()
        if id in self.buckets:
            raise StorageApiError("The resource already exists", "Duplicate", "409")
        options = options or {}
        self.buckets[id] = {
            "id": id, "name": name or id, "public": bool(options.get("public", False)),
            "file_size_limit": options.get("file_size_limit"),
        }
        return {"name": id}

    def update_bucket(self, id: str, options: dict) -> dict:
        self.check_up()
        if id not in self.buckets:
            raise StorageApiError("Bucket not found", "Bucket not found", "404")
        self.buckets[id].update(options)
        return {"message": "Successfully updated"}

    def from_(self, id: str) -> _FakeBucketFiles:
        return _FakeBucketFiles(self, id)


class ScriptedModel(LLMProvider):
    """A model that answers each task from a script and records every request.

    A reply is a value (text, a dict or a schema instance), an exception to raise,
    or a list of those consumed one per call. A task without a reply is unavailable.
    """

    name = "scripted"

    def __init__(self, replies: dict[str, Any], location: LLMLocation = LLMLocation.CLOUD) -> None:
        self.location = location
        self.calls: list[LLMRequest] = []
        self._replies = {task: list(r) if isinstance(r, list) else r for task, r in replies.items()}

    @property
    def tasks(self) -> list[str]:
        return [c.task for c in self.calls]

    def is_available(self) -> bool:
        return True

    def _answer(self, req: LLMRequest) -> Any:
        self.calls.append(req)
        reply = self._replies.get(req.task)
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else None
        if reply is None:
            raise LLMUnavailableError(f"no scripted reply for {req.task!r}")
        if isinstance(reply, BaseException):
            raise reply
        schema = req.response_schema
        if schema is None or isinstance(reply, schema):
            return reply
        with translated_errors():  # unparseable output is a model failure, as in the adapters
            if isinstance(reply, dict):
                return schema.model_validate(reply)
            return schema.model_validate_json(strip_fences(reply))

    async def complete(self, req: LLMRequest) -> Any:
        return self._answer(req)

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        text = self._answer(req)
        for i in range(0, len(text), 4):  # mid-word chunks, as a real stream arrives
            yield ("content", text[i : i + 4])


def install_scripted_model(**replies: Any) -> ScriptedModel:
    """Install ``ScriptedModel(replies)`` as the whole provider chain and return it.

    ``route_llm_call`` reaches it and nothing else; conftest's ``_fresh_llm_chain``
    removes it after the test.
    """
    from app.infrastructure.llm.chain import FallbackChainProvider
    from app.infrastructure.llm.factory import set_chain

    model = ScriptedModel(replies)
    set_chain(FallbackChainProvider([model]))
    return model


# The user_tasks columns accept_draft writes: its v_columns list as the latest
# migration that defines it has it (tests/test_migrations.py checks the two agree).
ACCEPT_DRAFT_COLUMNS = frozenset({
    "user_id", "plan_id", "task_id", "goal_id", "title", "status",
    "duration_minutes", "difficulty_weight", "dependencies", "deadline_hint",
    "completion_criteria", "implementation_intention", "topic_keywords",
    "scheduled_start", "scheduled_end", "created_at", "not_before",
})


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
        # Supabase Storage, for the uploaded files.
        self.storage = FakeStorage()

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
        """Mirror of the accept_draft SQL function (v3): one transaction, all or nothing.

        It replaces the user's pending rows, plus their missed rows whose task_id
        appears in ``p_rows`` (20261115000001_accept_draft_reslots_missed.sql), and
        writes only the columns its list names, for the draft's owner
        (``ACCEPT_DRAFT_COLUMNS``; v3, 20261115000007_user_tasks_not_before.sql, added
        ``not_before``).

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
            now = utc_now()
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
                # As the function does: only the listed keys, written for the draft's owner.
                listed = {
                    key: value
                    for key, value in {**row, "user_id": p_user_id}.items()
                    if key in ACCEPT_DRAFT_COLUMNS
                }
                # Table defaults the explicit column list leaves to Postgres.
                tasks.append({"id": str(uuid.uuid4()), "created_at": now.isoformat(), **listed})
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


class FakeKnowledgeCollection:
    """ChromaDB's knowledge collection: chunks with metadata, no embedder.

    ``query`` honours ``where`` (including ``$and``) the way Chroma does and reports each
    chunk's fixed distance, closest first; ``get`` and ``delete`` work on the same
    chunks. Every query and get is recorded. Install it with ``install(monkeypatch)``.
    """

    def __init__(self, chunks: list[dict], space: str = "cosine") -> None:
        self.chunks = chunks  # {"id", "document", "metadata", "distance"}
        self.metadata = {"hnsw:space": space}
        self.queries: list[dict] = []
        self.gets: list[dict] = []

    @staticmethod
    def _matches(meta: dict, where: dict | None) -> bool:
        if not where:
            return True
        if "$and" in where:
            return all(FakeKnowledgeCollection._matches(meta, clause) for clause in where["$and"])
        return all(meta.get(k) == v for k, v in where.items())

    def query(self, query_texts, n_results=10, where=None, include=None):
        self.queries.append({"query_texts": query_texts, "n_results": n_results, "where": where})
        hits = sorted(
            (c for c in self.chunks if self._matches(c["metadata"], where)),
            key=lambda c: c["distance"],
        )[:n_results]
        return {
            "ids": [[c["id"] for c in hits]],
            "documents": [[c["document"] for c in hits]],
            "metadatas": [[c["metadata"] for c in hits]],
            "distances": [[c["distance"] for c in hits]],
        }

    def get(self, where=None, **_kwargs):
        self.gets.append({"where": where})
        hits = [c for c in self.chunks if self._matches(c["metadata"], where)]
        return {"ids": [c["id"] for c in hits], "documents": [c["document"] for c in hits]}

    def delete(self, ids):
        self.chunks = [c for c in self.chunks if c["id"] not in ids]

    def install(self, monkeypatch) -> "FakeKnowledgeCollection":
        """Make it the collection ``app.utils.chroma_client`` hands out; returns itself."""
        import app.utils.chroma_client as chroma

        client = SimpleNamespace(get_or_create_collection=lambda *_a, **_k: self)
        monkeypatch.setattr(chroma, "get_chroma_client", lambda: client)
        return self


def knowledge_chunk(cid: str, user: str, text: str, distance: float, source: str = "src-a") -> dict:
    """A chunk for ``FakeKnowledgeCollection``: ``user``'s, from document ``source``, at
    ``distance`` from every query."""
    return {
        "id": cid,
        "document": text,
        "metadata": {"user_id": user, "source_id": source},
        "distance": distance,
    }


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


def build_test_container(supabase: FakeSupabase | None = None) -> "AppContainer":
    """The composition root as the lifespan builds it, for a test: no model, no network.

    Over ``supabase`` it uses the Supabase repositories; ``None`` means the InMemory
    family. Memories are embedded by ``BagOfWordsEmbedder``, and the LLM chain is built
    but not installed, so a test keeps whichever chain it scripted. (Not ``test_…``:
    pytest would collect it in every module that imports it.)
    """
    import app.core.config as cfg
    from app.container import AppContainer
    from app.orchestrator.hooks import ActionHooks

    return AppContainer.build(
        supabase_client=supabase,
        cfg=cfg,
        hooks=ActionHooks(),
        embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )


async def accept_tasks(repos: Any, user_id: str, rows: list[dict]) -> None:
    """Give the user these tasks the only way user_tasks is ever written: by accepting a
    draft (``repos`` is either repository family). A row needs at least ``task_id`` and
    ``title``; the rest defaults to a pending 25-minute task of middling difficulty."""
    draft = await repos.drafts.create(
        user_id, tasks=[], horizon_start=None, goal_id=None, schedule=None
    )
    await repos.drafts.accept_atomic(user_id, draft.id, [
        {"status": "pending", "duration_minutes": 25, "difficulty_weight": 0.5, **row}
        for row in rows
    ])


def planning_services(repos: Any, *, drafts: Any = None, events: Any = None) -> Any:
    """The planning sub-graph's ``PlanningServices`` over ``repos``, wired as the
    composition root wires them (``drafts`` defaults to a ``DraftService`` over the same
    repositories; ``events`` to a ``NullEventBus``). Memories are embedded by
    ``BagOfWordsEmbedder``."""
    from app.domain.events import NullEventBus
    from app.services.draft_service import DraftService
    from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator
    from app.services.memory.memory_service import MemoryService
    from app.services.planning.services import PlanningServices
    from app.services.task_service import TaskService

    events = events if events is not None else NullEventBus()
    if drafts is None:
        drafts = DraftService(
            repos.drafts, events, task_events=repos.task_events, tasks=repos.tasks
        )
    return PlanningServices(
        drafts=drafts,
        tasks=TaskService(repos.tasks, repos.task_events, events),
        task_repo=repos.tasks,
        constraints=repos.constraints,
        profiles=repos.profiles,
        memory=MemoryService(repos.memories, BagOfWordsEmbedder()),
        energy=HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator()),
    )


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
        "draft_id": None,
        "draft_version": None,
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
        "container": None,
        "trivial_input": None,
        "force_cloud_request": None,
    }
    base.update(overrides)
    return base
