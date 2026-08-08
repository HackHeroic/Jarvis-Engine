"""In-memory stand-ins for external services, so tests never touch the network.

`FakeSupabase` implements the slice of the supabase-py chained query builder
that the stores actually call: ``table(name).insert(row).execute()``,
``.select("*").eq(k, v)...execute()``, ``.update(d).eq(...).execute()``,
``.delete().eq(...).execute()``, plus ``.order()`` / ``.limit()``.

It is deliberately a real (if tiny) implementation rather than a MagicMock:
a MagicMock returns a canned payload regardless of the filters passed, so it
cannot catch a missing ``.eq("user_id", ...)`` — exactly the class of bug the
house security rule ("every Supabase query must filter by user_id") exists to
prevent. Filtering here is real, so user-isolation assertions have teeth.
"""

from datetime import datetime, timezone
from typing import Any

# Canned LLM output shared by every test that mocks the model boundary.
# Asserting this sentinel (rather than merely `is not None`) matters because
# each module swallows LLM exceptions and substitutes a generic fallback
# string — so a weaker assertion passes even when the call blew up.
CANNED_LLM_REPLY = "Canned LLM reply, sir."


class _Result:
    """Mirrors the supabase-py APIResponse surface the stores read."""

    def __init__(self, data: list[dict]):
        self.data = data


class _Query:
    def __init__(self, rows: list[dict], table_name: str):
        self._rows = rows
        self._table = table_name
        self._op: str | None = None
        self._payload: Any = None
        self._filters: list[tuple[str, Any]] = []
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

    def order(self, column: str, desc: bool = False) -> "_Query":
        self._order = (column, desc)
        return self

    def limit(self, count: int) -> "_Query":
        self._limit = count
        return self

    # ── execution ───────────────────────────────────────────
    def _matches(self, row: dict) -> bool:
        return all(row.get(col) == val for col, val in self._filters)

    def execute(self) -> _Result:
        if self._op == "insert":
            incoming = (
                self._payload if isinstance(self._payload, list) else [self._payload]
            )
            inserted = []
            for row in incoming:
                # The real table defaults created_at; emulate so .order() works.
                stored = {"created_at": datetime.now(timezone.utc).isoformat(), **row}
                self._rows.append(stored)
                inserted.append(dict(stored))
            return _Result(inserted)

        matched = [r for r in self._rows if self._matches(r)]

        if self._op == "update":
            for row in matched:
                row.update(self._payload)
        elif self._op == "delete":
            for row in matched:
                self._rows.remove(row)

        if self._order:
            column, desc = self._order
            matched.sort(key=lambda r: r.get(column) or "", reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]

        # Return copies so callers mutating results can't corrupt the store.
        return _Result([dict(r) for r in matched])


class FakeSupabase:
    """Chainable in-memory Supabase stand-in. No network, no MagicMock."""

    def __init__(self, seed: dict[str, list[dict]] | None = None):
        self.rows: dict[str, list[dict]] = {
            name: list(rows) for name, rows in (seed or {}).items()
        }

    def table(self, name: str) -> _Query:
        return _Query(self.rows.setdefault(name, []), name)


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
