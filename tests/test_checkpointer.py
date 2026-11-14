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
from tests.behaviour.conftest import (  # noqa: F401 - jarvis and llm are fixtures
    brain_dump,
    decomposition,
    jarvis,
    llm,
)
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


def test_scrub_transients__nulls_the_draft_store():
    """The allowlist itself must cover draft_store, not just the serde fallback.

    Asserting only on what lands in SQLite proves nothing about
    ``_TRANSIENT_KEYS``: ``_make_serializable`` nulls anything the serde
    rejects, so the turn-level test passes either way. This pins the fast path
    directly — remove ``draft_store`` from the tuple and only this fails.
    """
    from app.orchestrator.checkpoint import scrub_transients
    from app.services.draft_store import DraftStore

    store = DraftStore(supabase_client=object())

    assert scrub_transients({"draft_store": store})["draft_store"] is None
    # Nested one level down, the way LangGraph stores a turn's `__start__` input.
    nested = scrub_transients({"__start__": {"draft_store": store, "user_id": "u1"}})
    assert nested["__start__"]["draft_store"] is None
    assert nested["__start__"]["user_id"] == "u1"


@pytest.mark.asyncio
async def test_checkpointer__draft_store_is_scrubbed_but_draft_id_survives(
    no_llm, tmp_path, caplog
):
    """The live DraftStore holds a Supabase client; only the id may persist.

    ``draft_store`` is re-supplied per turn from ``app.state`` (chat.py), so
    checkpointing it buys nothing and costs a
    ``TypeError: Type is not msgpack serializable``. ``draft_id`` is a plain
    string and *must* survive — the accept turn resolves the draft by it.

    The caplog assertion is the load-bearing one: it proves the *allowlist*
    handled the store. Without it the test passes on the serde fallback alone,
    which logs a warning and would fire for any unlisted live object.
    """
    import logging

    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph
    from app.services.draft_store import DraftStore

    caplog.set_level(logging.WARNING, logger="app.orchestrator.checkpoint")

    class _LiveClient:  # stands in for the Supabase client: not serializable
        def __init__(self):
            self.lock = asyncio.Lock()

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        state = _live_state("hi", draft_store=DraftStore(supabase_client=_LiveClient()))
        result = await graph.ainvoke(state, config=cfg)

        assert result["response_message"] == CANNED_LLM_REPLY

        await graph.aupdate_state(cfg, {"draft_id": "d-42"})
        stored = (await graph.aget_state(cfg)).values
        assert stored["draft_store"] is None
        assert stored["draft_id"] == "d-42"
        # The allowlist handled it, so the serde never saw the store and the
        # "dropping non-serializable state key" fallback never fired.
        assert "draft_store" not in caplog.text


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
async def test_checkpointer__subgraph_live_objects_do_not_break_a_knowledge_turn(
    no_llm, tmp_path
):
    """Sub-graphs inherit the parent checkpointer, so their state hits the serde too.

    ``knowledge_state_in`` puts the live ``DatabaseClient`` and the decoded
    upload into ``KnowledgeState``. Those channel names never appear in
    ``JarvisState``, so scrubbing the parent's keys alone leaves a
    ``TypeError: Type is not msgpack serializable: FakeDBClient`` that kills
    every KNOWLEDGE_INGESTION / CALENDAR_SYNC / ACTION_ITEM turn.
    """
    import base64

    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        state = _live_state(
            "here are my notes",
            file_base64=base64.b64encode(b"lecture notes").decode(),
            file_media_type="text/plain",
            file_name="notes.txt",
        )
        result = await graph.ainvoke(state, config=cfg)

        # Reaching the module at all proves the scrub is write-path only: intent
        # classification reads file_base64 *after* the first checkpoint write.
        assert "knowledge_module" in result["modules_invoked"]
        assert (await graph.aget_state(cfg)).values["file_base64"] is None


@pytest.mark.asyncio
async def test_checkpointer__uploaded_document_is_never_persisted(no_llm, tmp_path):
    """Documents are re-supplied per turn from the request — keep them out of SQLite.

    Scrubbing only the decoded ``file_bytes`` proves nothing: ``file_base64``
    carries the identical document, and checkpoint rows are append-only, so the
    upload turn's row would hold it forever.
    """
    import base64

    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    secret = b"CONFIDENTIAL salary review"
    encoded = base64.b64encode(secret)
    db_path = tmp_path / "ckpt.sqlite"

    async with open_checkpointer(str(db_path)) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        await graph.ainvoke(
            _live_state(
                "file",
                file_base64=encoded.decode(),
                file_media_type="text/plain",
                file_name="2026-salary-review.pdf",
            ),
            config={"configurable": {"thread_id": "u1:sess-1"}},
        )

    on_disk = db_path.read_bytes()
    assert secret not in on_disk, "decoded upload persisted"
    assert encoded not in on_disk, "base64 upload persisted — b64decode recovers it"
    assert b"2026-salary-review.pdf" not in on_disk, "file name persisted"


@pytest.mark.asyncio
async def test_checkpointer__unknown_live_object_is_dropped_not_fatal(no_llm, tmp_path, caplog):
    """Belt and braces: a key nobody listed must degrade to None, not kill the turn."""
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    class _Unserializable:
        def __init__(self):
            self.lock = asyncio.Lock()

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}

        state = _live_state("hi")
        state["research_results"] = _Unserializable()  # stands in for a future live object
        result = await graph.ainvoke(state, config=cfg)

        assert result["response_message"] == CANNED_LLM_REPLY
        assert (await graph.aget_state(cfg)).values["research_results"] is None
        assert "research_results" in caplog.text


@pytest.mark.asyncio
async def test_checkpoint__history_and_memory_context__not_persisted(no_llm, tmp_path, caplog):
    """Both are read fresh every turn, under the turn lock, so a stored copy buys
    nothing — and checkpoint rows would keep the user's words and memories forever."""
    import logging

    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    caplog.set_level(logging.WARNING, logger="app.orchestrator.checkpoint")
    db_path = tmp_path / "ckpt.sqlite"
    history = [
        {"role": "user", "content": "my bank PIN hint is ZEBRA-HISTORY"},
        {"role": "assistant", "content": "Noted, sir."},
    ]

    async with open_checkpointer(str(db_path)) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}
        await graph.ainvoke(
            _live_state(
                "hi",
                conversation_history=history,
                memory_context="<memory>\n- (you said, today) ZEBRA-MEMORY\n</memory>",
            ),
            config=cfg,
        )
        stored = (await graph.aget_state(cfg)).values

    assert stored["conversation_history"] is None
    assert stored["memory_context"] is None
    on_disk = db_path.read_bytes()
    assert b"ZEBRA-HISTORY" not in on_disk
    assert b"ZEBRA-MEMORY" not in on_disk
    assert "conversation_history" not in caplog.text  # the allowlist, not the fallback


async def _checkpoint_rows(saver) -> dict[tuple[str, str], list[str]]:
    """Checkpoint ids per (thread, namespace), oldest first."""
    rows: dict[tuple[str, str], list[str]] = {}
    async with saver.conn.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints ORDER BY checkpoint_id"
    ) as cursor:
        async for thread_id, ns, checkpoint_id in cursor:
            rows.setdefault((thread_id, ns), []).append(checkpoint_id)
    return rows


async def _write_checkpoint_ids(saver) -> set[tuple[str, str, str]]:
    async with saver.conn.execute(
        "SELECT DISTINCT thread_id, checkpoint_ns, checkpoint_id FROM writes"
    ) as cursor:
        return {tuple(row) async for row in cursor}


async def _three_turns_each(graph, threads: list[str]) -> None:
    for thread_id in threads:
        cfg = {"configurable": {"thread_id": thread_id}}
        await graph.ainvoke(
            _live_state("hi", negotiation_state=NegotiationPhase.ACCEPTED), config=cfg
        )
        for _ in range(2):
            turn = _live_state("hi")
            turn.pop("negotiation_state")
            await graph.ainvoke(turn, config=cfg)


@pytest.mark.asyncio
async def test_prune_checkpoints__keeps_newest_n_per_thread(no_llm, tmp_path):
    from app.orchestrator.checkpoint import open_checkpointer, prune_checkpoints
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        await _three_turns_each(graph, ["u1:sess-1", "u2:sess-1"])
        before = await _checkpoint_rows(saver)
        assert max(len(ids) for ids in before.values()) > 2

        deleted = await prune_checkpoints(saver, keep_per_thread=2)

        after = await _checkpoint_rows(saver)
        assert set(after) == set(before)  # every thread and namespace keeps its newest
        for key, ids in before.items():
            assert after[key] == ids[-2:]
        assert deleted == sum(len(ids) for ids in before.values()) - sum(
            len(ids) for ids in after.values()
        )
        kept = {(t, ns, cid) for (t, ns), ids in after.items() for cid in ids}
        assert await _write_checkpoint_ids(saver) <= kept  # no writes left for a pruned one
        # The thread reads exactly as before: the newest checkpoint is whole.
        for thread_id in ("u1:sess-1", "u2:sess-1"):
            values = (await graph.aget_state({"configurable": {"thread_id": thread_id}})).values
            assert values["negotiation_state"] == NegotiationPhase.ACCEPTED
            assert values["user_id"] == "u1"


@pytest.mark.asyncio
async def test_prune_checkpoints__planning_subgraph_namespaces__pruned_with_their_turn(
    jarvis, llm, clock
):
    """Each module run checkpoints under its own namespace (``planning_module:<task>``),
    with a handful of rows: fewer than the keep, so a per-namespace rule never deletes
    them. Everything a sub-graph wrote before the thread's oldest kept root checkpoint
    goes, and the thread still resumes."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import app.api.v1.endpoints.schedule as schedule_endpoint
    from app.orchestrator.checkpoint import prune_checkpoints

    clock.freeze(datetime(2026, 11, 16, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")), schedule_endpoint)
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA") if "DSA" in req.prompt else "{}",
    )
    llm.script("socratic_chunker", decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision for Friday")
    session = planned.conversation_id
    await jarvis.say("scrap it", session=session)
    await jarvis.say("how was your day?", session=session)
    saver = jarvis.state.jarvis_graph.checkpointer
    before = await _checkpoint_rows(saver)
    (thread,) = {thread_id for thread_id, _ in before}
    subgraphs = [key for key in before if key[1]]
    assert any(key[1].startswith("planning_module") for key in subgraphs)
    assert all(len(before[key]) < 20 for key in subgraphs)

    deleted = await prune_checkpoints(saver, keep_per_thread=2)

    after = await _checkpoint_rows(saver)
    kept_root = before[(thread, "")][-2:]
    assert after[(thread, "")] == kept_root
    expected_subgraph_ids = {
        key: [cid for cid in ids if cid >= kept_root[0]][-2:]
        for key, ids in before.items()
        if key[1]
    }
    assert {k: v for k, v in after.items() if k[1]} == {
        k: v for k, v in expected_subgraph_ids.items() if v
    }
    assert not any(key[1].startswith("planning_module") for key in after)  # turn 1's run
    assert deleted == sum(map(len, before.values())) - sum(map(len, after.values()))
    kept = {(t, ns, cid) for (t, ns), ids in after.items() for cid in ids}
    assert await _write_checkpoint_ids(saver) <= kept
    # The conversation carries on from where it was.
    resumed = await jarvis.say("and how is the week looking?", session=session)
    assert resumed.reply


@pytest.mark.asyncio
async def test_prune_checkpoints__deleted_turns__leave_no_bytes_on_disk(no_llm, tmp_path):
    """secure_delete zeroes what a prune deletes, and the WAL is checkpointed and
    truncated, so a pruned turn's words cannot be read back from the file."""
    from app.orchestrator.checkpoint import open_checkpointer, prune_checkpoints

    db_path = tmp_path / "ckpt.sqlite"
    async with open_checkpointer(str(db_path)) as saver:
        graph = build_jarvis_graph_for(saver)
        cfg = {"configurable": {"thread_id": "u1:sess-1"}}
        for message in ("ZEBRA-FIRST-TURN secret", "second turn", "third turn"):
            await graph.ainvoke(_live_state(message), config=cfg)
        assert b"ZEBRA-FIRST-TURN" in _file_bytes(db_path)

        await prune_checkpoints(saver, keep_per_thread=2)

        assert b"ZEBRA-FIRST-TURN" not in _file_bytes(db_path)
        values = (await graph.aget_state(cfg)).values
        assert values["user_message"] == "third turn"


@pytest.mark.asyncio
async def test_prune_checkpoints__large_backlog__deleted_in_bounded_batches(
    no_llm, tmp_path, monkeypatch
):
    """The saver's lock is released between batches, so other turns' checkpoint
    writes never wait behind one huge delete."""
    import app.orchestrator.checkpoint as checkpoint
    from app.orchestrator.checkpoint import open_checkpointer, prune_checkpoints

    class _CountingLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.acquired = 0

        async def acquire(self) -> bool:
            self.acquired += 1
            return await super().acquire()

    monkeypatch.setattr(checkpoint, "_PRUNE_BATCH", 3)
    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph_for(saver)
        await _three_turns_each(graph, ["u1:sess-1"])
        before = await _checkpoint_rows(saver)
        stale = sum(len(ids) - 1 for ids in before.values())
        assert stale > 6
        saver.lock = _CountingLock()

        deleted = await prune_checkpoints(saver, keep_per_thread=1)

        assert deleted == stale
        assert saver.lock.acquired >= -(-stale // 3)  # at least one hold per batch
        assert {k: len(v) for k, v in (await _checkpoint_rows(saver)).items()} == {
            k: 1 for k in before
        }


def build_jarvis_graph_for(saver):
    from app.orchestrator.graph import build_jarvis_graph

    return build_jarvis_graph(checkpointer=saver)


def _file_bytes(db_path) -> bytes:
    """The database file and its write-ahead log, as they sit on disk."""
    wal = db_path.with_name(db_path.name + "-wal")
    return db_path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


@pytest.mark.asyncio
async def test_checkpoint_retention_sweep__returns_deleted_count(no_llm, tmp_path):
    import inspect
    from datetime import datetime, timedelta, timezone

    from app.orchestrator.checkpoint import CheckpointRetentionSweep, open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    async with open_checkpointer(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        await _three_turns_each(graph, ["u1:sess-1"])
        before = sum(len(ids) for ids in (await _checkpoint_rows(saver)).values())
        sweep = CheckpointRetentionSweep(saver, keep_per_thread=3)

        deleted = await sweep.run(["u1"], datetime(2026, 11, 16, tzinfo=timezone.utc))
        again = await sweep.run(["u1"], datetime(2026, 11, 17, tzinfo=timezone.utc))

        after = sum(len(ids) for ids in (await _checkpoint_rows(saver)).values())
        assert deleted == before - after > 0
        assert again == 0
        assert (sweep.name, sweep.interval) == ("checkpoint_retention", timedelta(hours=24))
        keep = inspect.signature(CheckpointRetentionSweep).parameters["keep_per_thread"]
        assert keep.default == 20  # CHECKPOINT_KEEP_PER_THREAD


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

        # Turn 2 must NOT re-supply the flag: only then does the checkpointed
        # True from turn 1 reach the node, which is the leak being pinned.
        second_turn = _live_state("tell me about the scheduler internals")
        second_turn.pop("trivial_input")
        second = await graph.ainvoke(second_turn, config=cfg)

        assert second["trivial_input"] is False


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
    """Records what chat_stream hands to the orchestrator."""

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
    """Drive chat_stream with every I/O boundary stubbed."""
    from types import SimpleNamespace

    from app.api.v1.endpoints.chat import ChatRequest, chat_stream

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

    app_state = SimpleNamespace(jarvis_graph=graph, db_client=None)
    http_request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    response = await chat_stream(
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
