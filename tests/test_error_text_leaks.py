"""Exception text never reaches the wire.

Each route below used to put ``str(exc)`` (or an f-string of it) into its HTTP
response. Every test plants a recognisable secret in the exception the route meets
and checks the response: a fixed message, the right status, no secret.
"""

import pytest
from fastapi.testclient import TestClient

from app.domain import errors
from tests.fakes import FakeDBClient, FakeSupabase

SECRET = "postgres://admin:hunter2@db.internal"


def _planted() -> RuntimeError:
    return RuntimeError(f"connection to {SECRET} refused")


@pytest.fixture
def app(monkeypatch):
    from app.main import app

    monkeypatch.setattr(app.state, "db_client", FakeDBClient(FakeSupabase()), raising=False)
    monkeypatch.setattr(app.state, "memory_store", None, raising=False)
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class _BrokenTable:
    """A table whose every query fails with the planted error."""

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise _planted()

        return fail


class _BrokenSupabase(FakeSupabase):
    def __init__(self, broken: str) -> None:
        super().__init__()
        self._broken = broken

    def table(self, name: str):
        return _BrokenTable() if name == self._broken else super().table(name)


def _assert_no_leak(resp) -> None:
    assert SECRET not in resp.text
    assert "hunter2" not in resp.text


# --- ingestion ---------------------------------------------------------------------


def test_ingest__classifier_fails__502_fixed_message_no_exception_text(client, monkeypatch):
    async def failing(**_kwargs):
        raise _planted()

    monkeypatch.setattr("app.core.model_router.route_llm_call", failing)

    resp = client.post("/api/v1/ingestion/ingest", json={"payload": "Lectures Mon 9-11"})

    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == "Could not classify the payload. Try again shortly."
    _assert_no_leak(resp)


@pytest.mark.parametrize("path", ["/api/v1/ingestion/ingest", "/api/v1/ingestion/process"])
def test_ingestion__invalid_base64__400_fixed_message_no_decoder_text(client, path):
    resp = client.post(path, json={"file_base64": "abc", "media_type": "pdf"})

    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "file_base64 is not valid base64."
    assert "padding" not in resp.text.lower() and "multiple of 4" not in resp.text


def test_process__preference_cannot_be_stored__result_carries_no_exception_text(
    app, client, monkeypatch
):
    from app.schemas.context import IntentClassification, IntentType

    async def classify(_text):
        return IntentClassification(
            intent=IntentType.BEHAVIORAL_CONSTRAINT, confidence=0.9, summary="a preference"
        )

    monkeypatch.setattr("app.services.extraction.orchestrator._classify", classify)
    monkeypatch.setattr(
        app.state, "db_client", FakeDBClient(_BrokenSupabase("behavioral_constraints"))
    )

    resp = client.post(
        "/api/v1/ingestion/process", json={"payload": "No study after 10 PM", "user_id": "u1"}
    )

    assert resp.status_code == 200
    assert resp.json()["behavioral_result"]["status"] == "error"
    _assert_no_leak(resp)


def test_delete_constraint__database_fails__500_fixed_message_no_exception_text(
    app, client, monkeypatch
):
    monkeypatch.setattr(
        app.state, "db_client", FakeDBClient(_BrokenSupabase("behavioral_constraints"))
    )

    resp = client.delete(
        "/api/v1/ingestion/behavioral-constraint/c1", params={"user_id": "u1"}
    )

    assert resp.status_code == 500
    assert resp.json()["error"]["message"] == "Could not delete the constraint."
    _assert_no_leak(resp)


# --- reasoning ---------------------------------------------------------------------


def test_decompose_goal__unparseable_reply__502_fixed_message_no_exception_text(
    client, monkeypatch
):
    async def unparseable(**_kwargs):
        raise ValueError(f"bad graph near {SECRET}")

    monkeypatch.setattr("app.core.model_router.route_llm_call", unparseable)

    resp = client.post("/api/v1/reasoning/decompose-goal", json={"user_prompt": "Learn DSA"})

    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == (
        "LLM Reasoning Error: the model returned content that "
        "could not be parsed into a valid ExecutionGraph."
    )
    _assert_no_leak(resp)


# --- workspace (its planted-text 500 is pinned in test_data_isolation.py) -----------


def test_workspace__jarvis_error__keeps_its_own_status_and_message(client, monkeypatch):
    async def unreachable(**_kwargs):
        raise errors.PersistenceError("Could not reach the database.")

    monkeypatch.setattr(
        "app.api.v1.endpoints.workspace.get_or_build_task_workspace", unreachable
    )

    resp = client.get("/api/v1/tasks/t1/workspace", params={"user_id": "u1"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "persistence_unavailable"



# --- chat ----------------------------------------------------------------------------


def test_accept_schedule__invalid_tasks__fixed_message_no_validation_text(client):
    resp = client.post(
        "/api/v1/chat/accept-schedule",
        json={"user_id": "u1", "tasks": [{"task_id": "t1", "title": SECRET}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error" and body["task_count"] == 0
    _assert_no_leak(resp)


class _ExplodingGraph:
    async def aget_state(self, config):
        return None

    async def astream(self, state, config=None, **kwargs):
        raise _planted()
        yield  # pragma: no cover — makes this an async generator


@pytest.mark.asyncio
async def test_chat_v2__graph_crashes__error_frame_is_the_envelope_without_exception_text(
    no_llm, monkeypatch
):
    import json
    from types import SimpleNamespace

    from app.api.v1.endpoints.chat import ChatRequest, chat_stream_v2

    async def _session(*_a, **_k):
        return "sess-1"

    async def _nothing(*_a, **_k):
        return None

    async def _history(*_a, **_k):
        return []

    monkeypatch.setattr("app.services.chat_history.get_or_create_session", _session)
    monkeypatch.setattr("app.services.chat_history.save_user_message", _nothing)
    monkeypatch.setattr("app.services.chat_history.save_assistant_message", _nothing)
    monkeypatch.setattr("app.services.chat_history.build_context_messages", _history)
    state = SimpleNamespace(jarvis_graph=_ExplodingGraph(), db_client=None, memory_store=None)

    response = await chat_stream_v2(
        ChatRequest(user_prompt="hi", user_id="u1"),
        SimpleNamespace(app=SimpleNamespace(state=state)),
    )
    raw = ""
    async for chunk in response.body_iterator:
        raw += chunk if isinstance(chunk, str) else chunk.decode()

    assert SECRET not in raw
    (frame,) = [b for b in raw.split("\n\n") if b.startswith("event: error")]
    error = json.loads(frame.split("data: ", 1)[1])["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "internal_error"
    assert error["message"] == "Something went wrong on our side."
