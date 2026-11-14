"""The chat turn as an application service: a router that routes, ``ChatTurnService``
that owns the turn, and a stream that frees its conversation the moment the client is gone.
"""

import ast
import asyncio
import gc
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import app.core.config as config
from app.schemas.chat_stream import ChatStreamRequest
from app.services import turn_locks
from tests.fakes import build_test_container

CHAT_ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "endpoints" / "chat.py"
# What the turn reaches; the router reaches only the service.
_TURN_INTERNALS = ("app.orchestrator", "app.core.observation", "app.services.memory",
                   "app.core.user_model")


class _Snapshot:
    def __init__(self, values: dict) -> None:
        self.values = values


def _stub_chat_io(monkeypatch, session_id: str = "sess-1") -> None:
    async def _session(*_a, **_k):
        return session_id

    async def _nothing(*_a, **_k):
        return None

    async def _history(*_a, **_k):
        return []

    monkeypatch.setattr("app.services.chat_history.get_or_create_session", _session)
    monkeypatch.setattr("app.services.chat_history.save_user_message", _nothing)
    monkeypatch.setattr("app.services.chat_history.save_assistant_message", _nothing)
    monkeypatch.setattr("app.services.chat_history.build_context_messages", _history)


# --- the router -------------------------------------------------------------------------


def _imported_modules(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_chat_router__routing_and_parsing_only():
    source = CHAT_ROUTER.read_text()
    tree = ast.parse(source)

    assert len(source.splitlines()) <= 80
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    for fn in functions:
        statements = sum(isinstance(n, ast.stmt) for n in ast.walk(fn)) - 1
        assert statements <= 12, f"{fn.name} has {statements} statements"
    reached = [m for m in _imported_modules(tree) if m.startswith(_TURN_INTERNALS)]
    assert reached == []


# --- the container rides in the turn ----------------------------------------------------


class _RecordingGraph:
    def __init__(self) -> None:
        self.states: list[dict] = []

    async def aget_state(self, config):
        return _Snapshot({})

    async def astream(self, state, config=None, **kwargs):
        self.states.append(dict(state))
        return
        yield  # pragma: no cover — makes this an async generator


@pytest.mark.asyncio
async def test_chat_turn_service__turn_state__carries_the_container(no_llm, monkeypatch):
    from app.services.chat_turn import ChatTurnService

    _stub_chat_io(monkeypatch)
    container = build_test_container()
    graph = _RecordingGraph()
    service = ChatTurnService(graph, container=container)

    turn = await service.open_turn(
        ChatStreamRequest(user_prompt="hi", user_id="u1"), request_id="req-1"
    )
    frames = [frame async for frame in turn.frames()]
    await turn.finish()

    assert turn.session_id == "sess-1"
    assert frames[-1].startswith("event: complete\n")
    (state,) = graph.states
    assert state["container"] is container


def test_chat_turn_state__container__never_checkpointed():
    from app.orchestrator.checkpoint import scrub_transients
    from app.orchestrator.state import JarvisState

    assert "container" in JarvisState.__annotations__
    assert scrub_transients({"container": object(), "user_id": "u1"}) == {
        "container": None, "user_id": "u1",
    }


# --- a closed tab frees its conversation, garbage collector or not ----------------------


class _BlockingGraph:
    """Two node events, then a run that never ends on its own; records its cancellation."""

    def __init__(self) -> None:
        self.cancelled = False

    async def aget_state(self, config):
        return _Snapshot({})

    async def astream(self, state, config=None, **kwargs):
        yield {"load_context": {}}
        yield {"classify_intent": {"intent": "CHAT"}}
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_chat_stream__client_gone_while_a_frame_waits_to_send__lock_released_without_gc(
    no_llm, monkeypatch
):
    """The client stops reading (the second frame's send never returns) and then leaves.

    Starlette cancels the send, not the stream's generator, which stays parked at its
    ``yield``: only closing it runs its cleanup. Left to the garbage collector, the
    conversation stays locked until a collection happens to run.
    """
    from app.api.v1.endpoints.chat import chat_stream

    _stub_chat_io(monkeypatch)
    graph = _BlockingGraph()
    first_sent = asyncio.Event()
    sent: list[bytes] = []

    async def send(message):
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        sent.append(message["body"])
        if len(sent) == 1:
            first_sent.set()
        else:
            await asyncio.Event().wait()  # the client stopped reading

    async def receive():
        await first_sent.wait()
        return {"type": "http.disconnect"}

    state = SimpleNamespace(jarvis_graph=graph, db_client=None)
    http_request = SimpleNamespace(app=SimpleNamespace(state=state))
    gc.disable()
    try:
        response = await chat_stream(ChatStreamRequest(user_prompt="hi", user_id="u1"),
                                     http_request)
        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        await asyncio.wait_for(response(scope, receive, send), timeout=2)

        assert len(sent) == 2
        assert turn_locks._locks == {}
        assert graph.cancelled
    finally:
        gc.enable()


# --- request limits -----------------------------------------------------------------------


def _decoded_bytes_base64(size: int) -> str:
    """A base64 string that decodes to exactly ``size`` bytes."""
    full, rest = divmod(size, 3)
    return "A" * (4 * full) + {0: "", 1: "AA==", 2: "AAA="}[rest]


def test_chat_stream_request__at_the_limits__accepted():
    request = ChatStreamRequest(
        user_prompt="x" * config.MAX_CHAT_PROMPT_CHARS,
        user_id="u1",
        file_base64=_decoded_bytes_base64(config.MAX_CHAT_UPLOAD_BYTES),
        media_type="pdf",
    )

    assert len(request.user_prompt) == config.MAX_CHAT_PROMPT_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "body"),
    [
        ("user_prompt", {"user_prompt": "x" * (config.MAX_CHAT_PROMPT_CHARS + 1)}),
        ("file_base64", {
            "user_prompt": "Here is my syllabus",
            "file_base64": _decoded_bytes_base64(config.MAX_CHAT_UPLOAD_BYTES + 1),
            "media_type": "pdf",
        }),
    ],
)
async def test_chat_stream__request_over_a_limit__422_envelope_and_no_turn(
    monkeypatch, field, body
):
    from app.main import app

    graph = _RecordingGraph()
    monkeypatch.setattr(app.state, "jarvis_graph", graph, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/chat/stream", json={"user_id": "u1", **body})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [e["loc"] for e in error["details"]["errors"]] == [["body", field]]
    assert len(response.content) < 2000  # the oversized input is not echoed back
    assert graph.states == []
