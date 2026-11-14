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


@pytest.mark.asyncio
async def test_ingest_knowledge__vector_store_fails__result_carries_a_fixed_code(
    monkeypatch, caplog
):
    from app.services.extraction.knowledge_ingester import ingest_knowledge

    monkeypatch.setattr("app.utils.chroma_client.get_chroma_client", _broken_chroma)

    result = await ingest_knowledge(extracted_text="Chapter 1: graphs. " * 20, user_id="u1")

    assert result.stored_chunk_count == 0
    assert result.metadata == {"error": "vector_store_unavailable"}
    assert SECRET not in repr(result)
    assert SECRET in caplog.text


def test_process__vector_store_fails__body_carries_no_exception_text(client, monkeypatch):
    """The ingester's result reaches the /ingestion/process body (and the v2 complete frame)."""
    from app.schemas.context import IntentClassification, IntentType

    async def classify(_text):
        return IntentClassification(
            intent=IntentType.KNOWLEDGE_INGESTION, confidence=0.9, summary="notes"
        )

    monkeypatch.setattr("app.services.extraction.orchestrator._classify", classify)
    monkeypatch.setattr("app.utils.chroma_client.get_chroma_client", _broken_chroma)

    resp = client.post(
        "/api/v1/ingestion/process",
        json={"payload": "Chapter 1: graphs and trees. " * 20, "user_id": "u1"},
    )

    assert resp.status_code == 200
    assert resp.json()["knowledge_result"]["metadata"] == {"error": "vector_store_unavailable"}
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

    from app.api.v1.endpoints.chat import chat_stream
    from app.schemas.chat_stream import ChatStreamRequest

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
    state = SimpleNamespace(jarvis_graph=_ExplodingGraph(), db_client=None)

    response = await chat_stream(
        ChatStreamRequest(user_prompt="hi", user_id="u1"),
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


# --- documents and memories ------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "message"),
    [
        ("get", "/api/v1/documents/", "Could not load your documents."),
        ("delete", "/api/v1/documents/src-1", "Could not delete the document."),
    ],
)
def test_documents__database_fails__503_fixed_message_secret_only_in_the_log(
    app, client, monkeypatch, caplog, method, path, message
):
    monkeypatch.setattr(
        app.state, "db_client", FakeDBClient(_BrokenSupabase("ingested_documents"))
    )
    monkeypatch.setattr("app.utils.chroma_client.get_chroma_client", _broken_chroma)

    resp = getattr(client, method)(path, params={"user_id": "u1"})

    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == message
    _assert_no_leak(resp)
    assert SECRET in caplog.text


def _broken_chroma():
    raise _planted()


def test_document_chunks__store_fails__503_fixed_message_secret_only_in_the_log(
    client, monkeypatch, caplog
):
    monkeypatch.setattr("app.utils.chroma_client.get_chroma_client", _broken_chroma)

    resp = client.get("/api/v1/documents/src-1/chunks", params={"user_id": "u1"})

    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == "Could not load the document's chunks."
    _assert_no_leak(resp)
    assert SECRET in caplog.text


def test_edit_memory__database_fails__503_fixed_message_secret_only_in_the_log(
    app, client, monkeypatch, caplog
):
    import app.core.config as cfg
    from app.container import AppContainer
    from app.orchestrator.hooks import ActionHooks
    from tests.fakes import BagOfWordsEmbedder

    container = AppContainer.build(
        supabase_client=_BrokenSupabase("user_memories"),
        cfg=cfg,
        hooks=ActionHooks(),
        embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )
    monkeypatch.setattr(app.state, "container", container, raising=False)

    resp = client.patch(
        "/api/v1/memories/00000000-0000-4000-8000-000000000001",
        params={"user_id": "u1"},
        json={"content": "x"},
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == "Could not reach the database."
    _assert_no_leak(resp)
    assert SECRET in caplog.text


# --- module steps, module errors and the reply that narrates them ----------------------


@pytest.mark.asyncio
async def test_module_step__handler_raises__tool_use_frame_carries_a_fixed_text(caplog):
    import asyncio
    import json

    from app.core.module_framework import ModuleStep, _wrap_step

    async def explode(_state):
        raise _planted()

    step = ModuleStep(name="ingest_document", handler=explode, module_name="knowledge_module")
    queue: asyncio.Queue = asyncio.Queue()

    with pytest.raises(RuntimeError):
        await _wrap_step(step)({"progress_queue": queue})

    frames = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    (failed,) = [f for f in frames if f["status"] == "error"]
    assert failed["detail"] == {"error": "This step failed."}
    assert SECRET not in json.dumps(frames)
    assert SECRET in caplog.text


@pytest.mark.asyncio
async def test_module_step__jarvis_error__tool_use_frame_carries_its_own_message():
    import asyncio
    import json

    from app.core.module_framework import ModuleStep, _wrap_step

    async def refuse(_state):
        raise errors.PersistenceError("Could not reach the database.")

    step = ModuleStep(name="fetch", handler=refuse, module_name="planning_module")
    queue: asyncio.Queue = asyncio.Queue()

    with pytest.raises(errors.PersistenceError):
        await _wrap_step(step)({"progress_queue": queue})

    frames = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    (failed,) = [f for f in frames if f["status"] == "error"]
    assert failed["detail"] == {"error": "Could not reach the database."}


@pytest.mark.asyncio
async def test_knowledge_ingest__ingestion_raises__state_error_is_a_fixed_text(
    monkeypatch, caplog
):
    from app.modules.knowledge_graph import ingest_document

    async def failing(**_kwargs):
        raise _planted()

    monkeypatch.setattr("app.services.extraction.orchestrator.process_ingestion", failing)

    out = await ingest_document({"content": "notes", "user_id": "u1"})

    assert out["error"] == "the document could not be read"
    assert SECRET in caplog.text


@pytest.mark.asyncio
async def test_research_search__search_raises__state_error_is_a_fixed_text(monkeypatch, caplog):
    from app.modules.research_graph import execute_search

    async def failing(*_args, **_kwargs):
        raise _planted()

    monkeypatch.setattr(
        "app.services.analytical.workspace_builder.perform_learning_style_search", failing
    )

    out = await execute_search({"query": "graphs", "search_results": [], "iteration_count": 0})

    assert out["error"] == "the search could not run"
    assert SECRET in caplog.text


@pytest.mark.asyncio
async def test_planning_decompose__chunker_raises__state_error_is_a_fixed_text(
    monkeypatch, caplog
):
    from app.modules.planning_graph import decompose_goal

    async def failing(**_kwargs):
        raise _planted()

    monkeypatch.setattr("app.core.model_router.route_llm_call", failing)

    out = await decompose_goal({"user_message": "Learn graph algorithms this week", "user_id": "u1"})

    assert out["error"] == "the goal could not be broken into steps"
    assert out["task_chunks"] == []
    assert SECRET in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_reply__module_error__narrates_only_the_stored_text(monkeypatch, streamed):
    """End to end from the module to the words: what the module stores is all the reply says."""
    from app.modules.research_graph import execute_search
    from app.services.analytical import voice_of_jarvis as voj

    async def failing(*_args, **_kwargs):
        raise _planted()

    monkeypatch.setattr(
        "app.services.analytical.workspace_builder.perform_learning_style_search", failing
    )
    out = await execute_search({"query": "graphs", "search_results": [], "iteration_count": 0})
    summary = {"error": out["error"]}

    if streamed:
        reply = "".join([tok async for _kind, tok in voj.synthesize_jarvis_response_stream(summary)])
    else:
        reply, _thinking = await voj.synthesize_jarvis_response(summary)

    assert reply == "Sir, something hiccupped on my end: the search could not run. Shall we try again?"
    assert SECRET not in reply
