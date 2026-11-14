"""In-memory stand-ins for external services, so tests never touch the network.

`FakeSupabase` implements the slice of the supabase-py chained query builder
that the stores actually call: ``table(name).insert(row).execute()``,
``.select("*").eq(k, v)...execute()``, ``.update(d).eq(...).execute()``,
``.delete().eq(...).execute()``, plus ``.order()`` / ``.limit()``, the
``.neq()`` / ``.in_()`` / ``.gte()`` / ``.lte()`` / ``.lt()`` / ``.gt()`` filters,
``.is_(column, "null")``, and
``rpc("accept_draft", params).execute()``.

It is deliberately a real (if tiny) implementation rather than a MagicMock:
a MagicMock returns a canned payload regardless of the filters passed, so it
cannot catch a missing ``.eq("user_id", ...)`` — exactly the class of bug the
house security rule ("every Supabase query must filter by user_id") exists to
prevent. Filtering here is real, so user-isolation assertions have teeth.
"""

import copy
import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

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


class _Query:
    def __init__(self, rows: list[dict], table_name: str):
        self._rows = rows
        self._table = table_name
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

    def select(self, *_columns: str) -> "_Query":
        self._op = "select"
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
                row.update(copy.deepcopy(self._payload))
        elif self._op == "delete":
            for row in matched:
                self._rows.remove(row)

        if self._order:
            column, desc = self._order
            matched.sort(key=lambda r: r.get(column) or "", reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]

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

    def table(self, name: str) -> _Query:
        return _Query(self.rows.setdefault(name, []), name)

    def rpc(self, name: str, params: dict) -> _RpcCall:
        # The real client sends params as JSON; round-trip so a value the
        # real call could not serialise fails here too.
        params = json.loads(json.dumps(params))
        if name == "accept_draft":
            return _RpcCall(lambda: self._accept_draft(**params))
        raise NotImplementedError(f"FakeSupabase has no rpc {name!r}")

    def _accept_draft(self, p_draft_id: str, p_user_id: str, p_rows: list[dict]) -> _Result:
        """Mirror of the accept_draft SQL function: one transaction, all or nothing.

        Also enforces the migrated table's ``UNIQUE (user_id, task_id)``: a row whose
        task_id one of the user's surviving (non-pending) rows, or an earlier row of
        the same call, already holds fails the whole call, as the real insert would.
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
            remaining = [
                r
                for r in tasks
                if not (r.get("user_id") == p_user_id and r.get("status") == "pending")
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
