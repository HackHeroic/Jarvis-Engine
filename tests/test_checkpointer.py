"""SQLite checkpointer — conversation/negotiation state survives across turns.

`check_negotiation_shortcut` (routing.py) reads `negotiation_state` at graph
entry, but every turn used to arrive with the field hardcoded to NONE at
request entry, so the shortcut was dead code. These tests pin the round-trip:
what a turn leaves on a thread is what the next turn on that thread reads.

The three live per-turn objects (`user_model`, `progress_callback`,
`progress_queue`) are not msgpack-serializable, so they must be scrubbed on
the way into SQLite and rehydrated by `_load_context` on the way out.
"""

import asyncio

import pytest

from app.orchestrator.routing import check_negotiation_shortcut
from app.orchestrator.state import ConversationPhase, NegotiationPhase
from tests.fakes import CANNED_LLM_REPLY, FakeDBClient, make_jarvis_state


def _live_state(msg: str, **overrides) -> dict:
    """A turn carrying the real non-serializable objects the endpoint passes.

    A real ``UserModel`` on purpose: it holds asyncio locks and a db client,
    which is exactly what msgpack refuses to serialize.
    """
    from app.core.user_model import UserModel

    queue: asyncio.Queue = asyncio.Queue()

    return make_jarvis_state(
        user_id="u1",
        user_message=msg,
        user_model=UserModel(user_id="u1", db=FakeDBClient()),
        progress_callback=lambda phase, **detail: queue.put_nowait(phase),
        progress_queue=queue,
        **overrides,
    )


@pytest.mark.asyncio
async def test_checkpointer__transient_objects_do_not_break_serialization(no_llm, tmp_path):
    """A live turn carries a callable, a Queue and a lock-holding facade."""
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        result = await graph.ainvoke(_live_state("hi"), config=cfg)

        # The turn itself still sees the live objects
        assert result["response_message"] == CANNED_LLM_REPLY
        # ...but nothing unserializable reached SQLite
        stored = (await graph.aget_state(cfg)).values
        assert stored["progress_callback"] is None
        assert stored["progress_queue"] is None
        assert stored["user_model"] is None
        assert stored["user_id"] == "u1"


@pytest.mark.asyncio
async def test_checkpointer__conversation_phase_survives_next_turn(no_llm, tmp_path):
    """Turn 2 omits the phase fields entirely — they must come from the checkpoint."""
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        await graph.ainvoke(
            _live_state("hi", conversation_phase=ConversationPhase.REVIEW), config=cfg
        )

        second_turn = _live_state("hi")
        second_turn.pop("conversation_phase")
        second_turn.pop("negotiation_state")
        result = await graph.ainvoke(second_turn, config=cfg)

        assert result["conversation_phase"] == ConversationPhase.REVIEW


@pytest.mark.asyncio
async def test_checkpointer__negotiation_state_persists_across_turns(no_llm, tmp_path):
    """ACCEPTED must not silently reset to NONE when the next turn omits it."""
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        await graph.ainvoke(
            _live_state("hi", negotiation_state=NegotiationPhase.ACCEPTED), config=cfg
        )

        second_turn = _live_state("hi")
        second_turn.pop("negotiation_state")
        result = await graph.ainvoke(second_turn, config=cfg)

        assert result["negotiation_state"] == NegotiationPhase.ACCEPTED


@pytest.mark.asyncio
async def test_checkpointer__revives_the_negotiation_shortcut(no_llm, tmp_path):
    """A mid-negotiation thread routes the next turn straight to planning."""
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        await graph.ainvoke(_live_state("hi"), config=cfg)
        # A planning turn that proposed a draft leaves the thread mid-review.
        await graph.aupdate_state(cfg, {"negotiation_state": NegotiationPhase.REVIEWING})

        resumed = (await graph.aget_state(cfg)).values

        assert resumed["negotiation_state"] == NegotiationPhase.REVIEWING
        assert check_negotiation_shortcut(resumed) == "negotiation_active"


@pytest.mark.asyncio
async def test_checkpointer__trivial_flag_does_not_leak_into_the_next_turn(no_llm, tmp_path):
    """Per-turn flags must be re-decided every turn, not inherited.

    `trivial_input` gates memory extraction (observation.py) and the
    conversation prompt. Once state persists, a greeting turn would otherwise
    keep marking every later turn on the thread as trivial.
    """
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        first = await graph.ainvoke(_live_state("hi"), config=cfg)
        assert first["trivial_input"] is True

        second = await graph.ainvoke(
            _live_state("tell me about the scheduler internals"), config=cfg
        )

        assert not second["trivial_input"]


@pytest.mark.asyncio
async def test_checkpointer__threads_are_isolated(no_llm, tmp_path):
    """State on one thread must never leak into another."""
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        mine = {"configurable": {"thread_id": "u1:sess-1"}}
        theirs = {"configurable": {"thread_id": "u2:sess-1"}}

        await graph.ainvoke(
            _live_state("hi", negotiation_state=NegotiationPhase.ACCEPTED), config=mine
        )

        assert (await graph.aget_state(theirs)).values == {}


@pytest.mark.asyncio
async def test_checkpointer__resumed_turn_rehydrates_the_facade(no_llm, tmp_path):
    """The scrubbed user_model comes back as a working facade from user_id alone."""
    from app.core.runtime import set_shared_clients
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import _load_context, build_jarvis_graph

    set_shared_clients(db=FakeDBClient())

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}
        await graph.ainvoke(_live_state("hi"), config=cfg)

        resumed = (await graph.aget_state(cfg)).values
        assert resumed["user_model"] is None

        hydrated = (await _load_context(resumed))["user_model"]
        assert hydrated.user_id == "u1"
        assert await hydrated.get_behavioral_constraints() == []


# ---------------------------------------------------------------------------
# Thread keys are user-scoped
#
# `_load_context` trusts the checkpointed user_id, so a session-only thread key
# would let a resumed turn rebuild another user's facade if two users ever
# shared (or guessed) a conversation id.
# ---------------------------------------------------------------------------


def test_thread_id__is_user_scoped():
    from app.orchestrator.checkpoint import make_thread_id

    assert make_thread_id("u1", "sess-1") == "u1:sess-1"
    assert make_thread_id("u2", "sess-1") != make_thread_id("u1", "sess-1")


class _CapturingGraph:
    """Records what chat_stream_v2 hands to the orchestrator."""

    def __init__(self, existing_values: dict | None = None):
        self.existing_values = existing_values or {}
        self.astream_calls: list[tuple[dict, dict]] = []
        self.aget_state_configs: list[dict] = []

    async def astream(self, state, config=None, **kwargs):
        self.astream_calls.append((state, config))
        return
        yield  # pragma: no cover — makes this an async generator

    async def aget_state(self, config):
        self.aget_state_configs.append(config)

        class _Snapshot:
            values = self.existing_values

        return _Snapshot()


async def _run_chat_v2(monkeypatch, graph, user_id="u1", session_id="sess-1"):
    """Drive chat_stream_v2 with every I/O boundary stubbed."""
    from types import SimpleNamespace

    from app.api.v1.endpoints.chat import ChatRequest, chat_stream_v2

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

    app_state = SimpleNamespace(jarvis_graph=graph, db_client=None, memory_store=None)
    http_request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    response = await chat_stream_v2(
        ChatRequest(user_prompt="hi", user_id=user_id), http_request
    )
    async for _chunk in response.body_iterator:  # drain the SSE stream
        pass
    return graph


@pytest.mark.asyncio
async def test_chat_v2__thread_id_is_scoped_to_the_user(no_llm, monkeypatch):
    graph = _CapturingGraph()

    await _run_chat_v2(monkeypatch, graph, user_id="u1", session_id="sess-1")

    _state, config = graph.astream_calls[0]
    assert config["configurable"]["thread_id"] == "u1:sess-1"
    assert all(c["configurable"]["thread_id"] == "u1:sess-1" for c in graph.aget_state_configs)


@pytest.mark.asyncio
async def test_chat_v2__fresh_thread__seeds_the_phase_fields(no_llm, monkeypatch):
    graph = _CapturingGraph(existing_values={})

    await _run_chat_v2(monkeypatch, graph)

    state, _config = graph.astream_calls[0]
    assert state["conversation_phase"] == ConversationPhase.GREETING
    assert state["negotiation_state"] == NegotiationPhase.NONE


@pytest.mark.asyncio
async def test_chat_v2__resumed_thread__does_not_reset_the_phase_fields(no_llm, monkeypatch):
    """The bug this task exists to fix: hardcoding NONE killed the shortcut."""
    graph = _CapturingGraph(
        existing_values={
            "negotiation_state": NegotiationPhase.REVIEWING,
            "conversation_phase": ConversationPhase.NEGOTIATION,
        }
    )

    await _run_chat_v2(monkeypatch, graph)

    state, _config = graph.astream_calls[0]
    assert "negotiation_state" not in state
    assert "conversation_phase" not in state
    # Per-turn flags still reset — only the phase fields are inherited.
    # (The negotiation shortcut skips extract_brain_dump, so nothing else clears this.)
    assert state["trivial_input"] is None
