"""One turn at a time per conversation (spec §10b), and who a turn's LLM calls are for.

Two tabs on one chat session share a checkpoint thread. Run concurrently, the turn
that finishes last overwrites what the other wrote; ``thread_lock`` makes the
second turn wait for the first instead.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services import turn_locks
from app.services.turn_locks import thread_lock


async def _settle() -> None:
    """Give every runnable task a few turns of the loop."""
    for _ in range(10):
        await asyncio.sleep(0)


# --- thread_lock ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_lock__same_thread__second_turn_waits_for_first():
    log: list[str] = []
    first_in = asyncio.Event()
    finish_first = asyncio.Event()

    async def first() -> None:
        async with thread_lock("u1:s1"):
            log.append("first starts")
            first_in.set()
            await finish_first.wait()
            log.append("first ends")

    async def second() -> None:
        async with thread_lock("u1:s1"):
            log.append("second starts")

    one = asyncio.create_task(first())
    await first_in.wait()
    two = asyncio.create_task(second())
    await _settle()
    assert log == ["first starts"]  # queued, not rejected

    finish_first.set()
    await asyncio.wait_for(asyncio.gather(one, two), timeout=1)

    assert log == ["first starts", "first ends", "second starts"]


@pytest.mark.asyncio
async def test_thread_lock__different_threads__run_concurrently():
    both_in = asyncio.Barrier(2)

    async def turn(thread_id: str) -> None:
        async with thread_lock(thread_id):
            await both_in.wait()  # deadlocks unless both hold their lock at once

    await asyncio.wait_for(asyncio.gather(turn("u1:s1"), turn("u1:s2")), timeout=1)


@pytest.mark.asyncio
async def test_thread_lock__holder_cancelled__lock_released():
    """A client disconnect cancels the turn; the queued turn must not wait forever."""
    holding = asyncio.Event()

    async def hold() -> None:
        async with thread_lock("u1:s1"):
            holding.set()
            await asyncio.Event().wait()

    holder = asyncio.create_task(hold())
    await holding.wait()
    holder.cancel()

    async with asyncio.timeout(1):
        async with thread_lock("u1:s1"):
            pass


@pytest.mark.asyncio
async def test_thread_lock__holder_raises__lock_released():
    with pytest.raises(RuntimeError):
        async with thread_lock("u1:s1"):
            raise RuntimeError("graph blew up")

    async with asyncio.timeout(1):
        async with thread_lock("u1:s1"):
            pass


@pytest.mark.asyncio
async def test_thread_lock__nobody_holds_or_waits__registry_forgets_the_thread():
    holding = asyncio.Event()
    finish = asyncio.Event()

    async def hold() -> None:
        async with thread_lock("u1:s1"):
            holding.set()
            await finish.wait()

    async def queue_up() -> None:
        async with thread_lock("u1:s1"):
            pass

    holder = asyncio.create_task(hold())
    await holding.wait()
    waiter = asyncio.create_task(queue_up())
    await _settle()
    waiter.cancel()  # a queued turn whose client went away
    await asyncio.gather(waiter, return_exceptions=True)
    assert "u1:s1" in turn_locks._locks  # still held

    finish.set()
    await holder

    assert "u1:s1" not in turn_locks._locks


# --- the v2 chat stream: lock around the graph run, usage attributed ---------------


class _Snapshot:
    def __init__(self, values: dict) -> None:
        self.values = values


class _TwoTabGraph:
    """A graph whose run for the prompt "first" blocks until released.

    Its checkpoint is empty until a run finishes, as for a brand-new thread.
    """

    def __init__(self) -> None:
        self.log: list[str] = []
        self.states: dict[str, dict] = {}
        self.first_running = asyncio.Event()
        self.finish_first = asyncio.Event()
        self._checkpoint: dict = {}

    async def aget_state(self, config):
        return _Snapshot(dict(self._checkpoint))

    async def astream(self, state, config=None, **kwargs):
        prompt = state["user_message"]
        self.states[prompt] = dict(state)
        self.log.append(f"{prompt} starts")
        if prompt == "first":
            self.first_running.set()
            await self.finish_first.wait()
        self.log.append(f"{prompt} ends")
        self._checkpoint = {"negotiation_state": "reviewing", "user_id": state["user_id"]}
        return
        yield  # pragma: no cover — makes this an async generator


def _stub_chat_io(monkeypatch, session_id: str = "sess-1") -> None:
    async def _session(*args, **kwargs):
        return session_id

    async def _noop(*args, **kwargs):
        return None

    async def _history(*args, **kwargs):
        return []

    monkeypatch.setattr("app.services.chat_history.get_or_create_session", _session)
    monkeypatch.setattr("app.services.chat_history.save_user_message", _noop)
    monkeypatch.setattr("app.services.chat_history.save_assistant_message", _noop)
    monkeypatch.setattr("app.services.chat_history.build_context_messages", _history)


async def _open_turn(graph, prompt: str, user_id: str = "u1"):
    from app.api.v1.endpoints.chat import ChatRequest, chat_stream_v2

    app_state = SimpleNamespace(jarvis_graph=graph, db_client=None, memory_store=None)
    http_request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    return await chat_stream_v2(ChatRequest(user_prompt=prompt, user_id=user_id), http_request)


async def _drain(response) -> None:
    async for _chunk in response.body_iterator:
        pass


@pytest.mark.asyncio
async def test_chat_v2__two_tabs_one_session__turns_run_in_order(no_llm, monkeypatch):
    _stub_chat_io(monkeypatch)
    graph = _TwoTabGraph()
    first = asyncio.create_task(_drain(await _open_turn(graph, "first")))
    await graph.first_running.wait()

    second = asyncio.create_task(_drain(await _open_turn(graph, "second")))
    await _settle()
    assert graph.log == ["first starts"]

    graph.finish_first.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert graph.log == ["first starts", "first ends", "second starts", "second ends"]


@pytest.mark.asyncio
async def test_chat_v2__queued_turn__sees_the_checkpoint_the_first_turn_left(no_llm, monkeypatch):
    """Decided before the wait, the queued turn would seed NONE over the first's draft review."""
    _stub_chat_io(monkeypatch)
    graph = _TwoTabGraph()
    first = asyncio.create_task(_drain(await _open_turn(graph, "first")))
    await graph.first_running.wait()
    second = asyncio.create_task(_drain(await _open_turn(graph, "second")))
    await _settle()

    graph.finish_first.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert "negotiation_state" in graph.states["first"]  # brand-new thread: seeded
    assert "negotiation_state" not in graph.states["second"]  # resumed: inherited


class _SlowUnwindGraph:
    """The run for "first" blocks; cancelled, it takes a while to unwind, as a graph
    with a checkpoint write in flight does."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.first_running = asyncio.Event()

    async def aget_state(self, config):
        return _Snapshot({})

    async def astream(self, state, config=None, **kwargs):
        prompt = state["user_message"]
        self.log.append(f"{prompt} starts")
        if prompt == "first":
            self.first_running.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
                self.log.append("first cleanup done")
                raise
        self.log.append(f"{prompt} ends")
        return
        yield  # pragma: no cover — makes this an async generator


async def _serve_over_asgi(graph, prompt: str, disconnect: asyncio.Event) -> None:
    """One request as uvicorn serves it: the handler, then its response called with
    ASGI spec 2.3 — Starlette then streams inside an anyio task group and cancels the
    stream when ``http.disconnect`` arrives. Handler and response share one task, as
    under Starlette's ``request_response``."""
    from app.api.v1.endpoints.chat import ChatRequest, chat_stream_v2

    app_state = SimpleNamespace(jarvis_graph=graph, db_client=None, memory_store=None)
    http_request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    response = await chat_stream_v2(ChatRequest(user_prompt=prompt, user_id="u1"), http_request)

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)


@pytest.mark.asyncio
async def test_chat_v2__client_disconnects_mid_turn__lock_released_once_the_run_unwound(
    no_llm, monkeypatch
):
    """anyio re-delivers the cancellation at every await of the stream's cleanup; an
    unshielded cleanup is cancelled again and the lock is left to the garbage collector."""
    _stub_chat_io(monkeypatch)
    graph = _SlowUnwindGraph()
    gone = asyncio.Event()
    first = asyncio.create_task(_serve_over_asgi(graph, "first", gone))
    await graph.first_running.wait()

    gone.set()  # the tab closed
    await asyncio.wait_for(first, timeout=2)

    assert graph.log == ["first starts", "first cleanup done"]
    assert turn_locks._locks == {}


@pytest.mark.asyncio
async def test_chat_v2__client_disconnects_mid_turn__queued_turn_starts_after_the_cleanup(
    no_llm, monkeypatch
):
    _stub_chat_io(monkeypatch)
    graph = _SlowUnwindGraph()
    gone = asyncio.Event()
    first = asyncio.create_task(_serve_over_asgi(graph, "first", gone))
    await graph.first_running.wait()

    gone.set()
    await _settle()  # the cancellation has landed; the first run is unwinding
    second = asyncio.create_task(_serve_over_asgi(graph, "second", asyncio.Event()))
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert graph.log == ["first starts", "first cleanup done", "second starts", "second ends"]
    assert turn_locks._locks == {}


class _AttributionGraph:
    """Records who the turn's LLM calls would be metered to, from inside the graph run."""

    def __init__(self) -> None:
        self.seen: list[tuple[str | None, str | None]] = []

    async def aget_state(self, config):
        return _Snapshot({})

    async def astream(self, state, config=None, **kwargs):
        from app.core.model_router import current_session_var, current_user_var

        self.seen.append((current_user_var.get(), current_session_var.get()))
        return
        yield  # pragma: no cover — makes this an async generator


@pytest.mark.asyncio
async def test_chat_v2__graph_run__llm_calls_attributed_to_user_and_session(no_llm, monkeypatch):
    from app.core.model_router import current_session_var, current_user_var

    _stub_chat_io(monkeypatch, session_id="sess-9")
    graph = _AttributionGraph()

    response = await _open_turn(graph, "hi", user_id="u7")
    await _drain(response)
    assert graph.seen == [("u7", "sess-9")]

    # The stream is consumed after the handler returned; the response's background
    # task, run in the handler's context after the last frame, ends the attribution.
    await response.background()
    assert (current_user_var.get(), current_session_var.get()) == (None, None)


@pytest.mark.asyncio
async def test_chat_v2__through_starlette__attribution_ends_cleanly_after_the_stream(
    no_llm, monkeypatch
):
    """The real response path: the stream runs in a task after the handler returned.

    The attribution must reach the graph run there, and the background task that
    ends it must run in the handler's context — a reset anywhere else raises.
    """
    import httpx

    from app.main import app

    _stub_chat_io(monkeypatch, session_id="sess-3")
    graph = _AttributionGraph()
    for name, value in (("jarvis_graph", graph), ("db_client", None), ("memory_store", None)):
        monkeypatch.setattr(app.state, name, value, raising=False)

    transport = httpx.ASGITransport(app=app)  # re-raises anything the app raises
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat/v2/stream", json={"user_prompt": "hi", "user_id": "u5"}
        )

    assert response.status_code == 200
    assert "event: complete" in response.text
    assert graph.seen == [("u5", "sess-3")]


# --- waiting has a limit; a queued turn reads the conversation as it is now ---------


@pytest.mark.asyncio
async def test_thread_lock__held_past_the_wait_limit__raises_conflict_and_forgets_the_waiter(
    monkeypatch,
):
    from app.domain.errors import ConflictError

    monkeypatch.setattr(turn_locks, "TURN_LOCK_WAIT_SECONDS", 0.05)
    holding = asyncio.Event()
    finish = asyncio.Event()

    async def hold() -> None:
        async with thread_lock("u1:s1"):
            holding.set()
            await finish.wait()

    holder = asyncio.create_task(hold())
    await holding.wait()

    with pytest.raises(ConflictError) as refused:
        async with thread_lock("u1:s1"):
            pytest.fail("entered a lock somebody else holds")

    assert refused.value.code == "conflict"
    finish.set()
    await holder
    assert "u1:s1" not in turn_locks._locks


async def _frames(response) -> list[tuple[str, object]]:
    import json

    raw = ""
    async for chunk in response.body_iterator:
        raw += chunk if isinstance(chunk, str) else chunk.decode()
    frames = []
    for block in raw.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            frames.append((lines["event"], json.loads(lines.get("data", "null"))))
    return frames


@pytest.mark.asyncio
async def test_chat_v2__turn_queued_past_the_wait_limit__honest_conflict_frame(
    no_llm, monkeypatch
):
    monkeypatch.setattr(turn_locks, "TURN_LOCK_WAIT_SECONDS", 0.05)
    _stub_chat_io(monkeypatch)
    graph = _TwoTabGraph()
    first = asyncio.create_task(_drain(await _open_turn(graph, "first")))
    await graph.first_running.wait()

    frames = await asyncio.wait_for(_frames(await _open_turn(graph, "second")), timeout=2)

    (error,) = [data for event, data in frames if event == "error"]
    assert set(error) == {"error"}
    assert set(error["error"]) == {"code", "message", "details", "request_id"}
    assert error["error"]["code"] == "conflict"
    assert "still being answered" in error["error"]["message"]
    assert error["error"]["request_id"]
    assert "second starts" not in graph.log  # the queued turn never ran
    graph.finish_first.set()
    await asyncio.wait_for(first, timeout=2)


@pytest.mark.asyncio
async def test_chat_v2__queued_turn__history_includes_the_reply_it_waited_for(
    no_llm, monkeypatch
):
    saved: list[dict] = []

    async def _history(session_id, user_id, **kwargs):
        return [dict(m) for m in saved]

    async def _save_user(session_id, user_id, content, supabase):
        saved.append({"role": "user", "content": content})
        return "u-msg"

    async def _save_assistant(session_id, user_id, content, intent, metadata, supabase):
        saved.append({"role": "assistant", "content": content})
        return "a-msg"

    _stub_chat_io(monkeypatch)
    monkeypatch.setattr("app.services.chat_history.build_context_messages", _history)
    monkeypatch.setattr("app.services.chat_history.save_user_message", _save_user)
    monkeypatch.setattr("app.services.chat_history.save_assistant_message", _save_assistant)
    graph = _TwoTabGraph()
    first = asyncio.create_task(_drain(await _open_turn(graph, "first")))
    await graph.first_running.wait()
    second = asyncio.create_task(_drain(await _open_turn(graph, "second")))
    await _settle()

    graph.finish_first.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    first_reply = next(m for m in saved if m["role"] == "assistant")
    history = graph.states["second"]["conversation_history"]
    assert history[-2:] == [{"role": "user", "content": "first"}, first_reply]
    # Still built before the turn's own message is saved: it is not in its own history.
    assert {"role": "user", "content": "second"} not in history


class _ForceCloudGraph(_AttributionGraph):
    async def astream(self, state, config=None, **kwargs):
        from app.core.model_router import force_cloud_var

        self.seen.append(force_cloud_var.get())
        return
        yield  # pragma: no cover — makes this an async generator


@pytest.mark.asyncio
async def test_chat_v2__cloud_model_picked__force_cloud_ends_with_the_turn(no_llm, monkeypatch):
    from app.api.v1.endpoints.chat import ChatRequest, chat_stream_v2
    from app.core.model_router import force_cloud_var

    _stub_chat_io(monkeypatch)
    graph = _ForceCloudGraph()
    app_state = SimpleNamespace(jarvis_graph=graph, db_client=None, memory_store=None)
    http_request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    response = await chat_stream_v2(
        ChatRequest(user_prompt="hi", user_id="u1", model_mode="cloud"), http_request
    )
    await _drain(response)
    await response.background()

    assert graph.seen == [True]  # the turn itself ran on the cloud
    assert force_cloud_var.get() is False  # and nothing after it does


def test_project__python_floor__is_3_12():
    """The lock's bounded wait leans on 3.12's asyncio.wait_for (a timeout that lands as
    the lock is granted keeps it), and the Claude provider's served-model ContextVar on
    3.12 task-context semantics: the package must not claim to run on anything older."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())

    assert pyproject["project"]["requires-python"] == ">=3.12,<3.14"
