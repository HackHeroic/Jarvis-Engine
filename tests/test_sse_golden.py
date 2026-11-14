"""The chat stream's bytes, frozen: every frame type, exactly as the browser receives it.

The frontend parses the stream line by line (``jarvis-frontend/lib/api.ts::consumeSSE``):
an ``event:`` line, a ``data:`` line of JSON, a blank line. These tests send scripted
turns through the real ``POST /api/v1/chat/stream`` route and compare the raw body with
a golden file in ``tests/golden/sse/``, so a refactor of how frames are built cannot
change a key, its order, a value's JSON spelling or the framing.

Only what differs from run to run is masked before the comparison: ids minted per run,
the request id, timings and the spinner verb (picked at random). Everything else is
compared byte for byte.

To re-record after a deliberate wire change: ``JARVIS_WRITE_SSE_GOLDEN=1 pytest
tests/test_sse_golden.py``, then review the diff of ``tests/golden/sse/``.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.domain.clock as utc_clock
from app.domain.entities import TaskEvent, TaskEventType
from app.domain.errors import ConflictError
from tests.behaviour.conftest import (  # noqa: F401 - jarvis and llm are fixtures
    brain_dump,
    decomposition,
    jarvis,
    llm,
    settle_background_tasks,
)

GOLDEN = Path(__file__).parent / "golden" / "sse"
IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_REQUEST_ID = re.compile(r'"request_id": "[0-9a-f]{32}"')
_TIMING = re.compile(r'"(total_time_s|tok_per_sec|ttft_ms|duration_ms)": [0-9.e-]+')
# A planning run's token (six hex characters), minted per decomposition: {goal_id}_{run}_{task_id}.
_RUN_TOKEN = re.compile(r"_[0-9a-f]{6}_(?=task_)")


def _masked(raw: str) -> str:
    raw = _UUID.sub("<uuid>", raw)
    raw = _REQUEST_ID.sub('"request_id": "<request_id>"', raw)
    raw = _TIMING.sub(lambda m: f'"{m.group(1)}": "<n>"', raw)
    return _RUN_TOKEN.sub("_<run>_", raw)


def _assert_golden(name: str, raw: str) -> None:
    path = GOLDEN / f"{name}.sse"
    body = _masked(raw)
    if os.environ.get("JARVIS_WRITE_SSE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    assert body == path.read_text(), f"the {name} stream's bytes changed"


@pytest.fixture(autouse=True)
def _fixed_verbs(monkeypatch):
    """The spinner verb is random (80% per phase, 20% wildcard): one per phase here."""
    monkeypatch.setattr("app.utils.spinner_verbs.get_spinner_verb", lambda phase: f"<{phase}>")


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    clock.freeze(MONDAY_8AM, schedule_endpoint, utc_clock)


async def _post(monkeypatch, state: dict, **body) -> str:
    """One turn through the real route; the raw response body."""
    from app.main import app

    for name, value in state.items():
        monkeypatch.setattr(app.state, name, value, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/chat/stream", json={"user_id": "u1", **body})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    await settle_background_tasks()
    return response.text


# --- scripted orchestrator runs: every frame the handler builds, every reply branch --------


class _ScriptedGraph:
    """Plays a turn as the orchestrator would: progress entries onto the turn's queue and
    node events out of ``astream``, in script order, on a brand-new thread."""

    def __init__(self, script: list[tuple]) -> None:
        self.script = script

    async def aget_state(self, config):
        return None

    async def astream(self, state, config=None, **kwargs):
        queue = state["progress_queue"]
        for kind, *args in self.script:
            if kind == "progress":
                (entry,) = args
                queue.put_nowait(entry if isinstance(entry, str) else json.dumps(entry))
            elif kind == "callback":
                phase, detail = args
                state["progress_callback"](phase, **detail)
            elif kind == "raise":
                raise args[0]
            else:
                name, update = args
                yield {name: update}


def _stub_chat_io(monkeypatch) -> None:
    async def _session(*_a, **_k):
        return "sess-1"

    async def _user_message(*_a, **_k):
        return "msg-user"

    async def _assistant_message(*_a, **_k):
        return "msg-reply"

    async def _history(*_a, **_k):
        return []

    monkeypatch.setattr("app.services.chat_history.get_or_create_session", _session)
    monkeypatch.setattr("app.services.chat_history.save_user_message", _user_message)
    monkeypatch.setattr("app.services.chat_history.save_assistant_message", _assistant_message)
    monkeypatch.setattr("app.services.chat_history.build_context_messages", _history)


async def _scripted_turn(monkeypatch, script: list[tuple], **body) -> str:
    _stub_chat_io(monkeypatch)
    state = {"jarvis_graph": _ScriptedGraph(script), "db_client": None, "container": None}
    return await _post(monkeypatch, state, user_prompt=body.pop("user_prompt", "hello there"),
                       **body)


def _tool(tool: str, status: str, detail: dict | None = None) -> tuple:
    entry = {"_event_type": "tool_use", "module": "planning_module", "tool": tool,
             "status": status}
    if detail is not None:
        entry["detail"] = detail
    return ("progress", entry)


_LEARNING = [
    ("progress", {"_event_type": "phase_start", "phase": "learning"}),
    ("progress", {
        "_event_type": "memory_extracted", "memory_id": "mem-1", "type": "preference",
        "content": "Prefers studying in the mornings", "confidence": 0.8,
        "provenance": "said", "action": "created",
    }),
    ("progress", {
        "_event_type": "pattern_detected", "type": "skip_time_window",
        "content": "Tends to set aside 9 AM slots", "confidence": 0.75,
        "occurrence_count": 3, "action": "created", "pattern_key": "skip_time_window:9",
        "memory_id": "mem-2",
    }),
    ("progress", {"_event_type": "phase_end", "phase": "learning", "status": "done"}),
    ("node", "observation_loop", {"memories_extracted_count": 1, "patterns_detected_count": 1}),
]


@pytest.mark.asyncio
async def test_sse_golden__scripted_planning_turn__bytes_unchanged(monkeypatch, llm):
    """Phase, step, tool_use (on and off contract), raw passthroughs, the schedule reply
    branch, learning frames and the complete frame of a plan."""
    raw = await _scripted_turn(monkeypatch, [
        ("node", "load_context", {}),
        ("node", "extract_brain_dump", {"brain_dump": None}),
        ("node", "classify_intent", {"intent": "PLAN_DAY"}),
        _tool("validate_goal", "started"),
        _tool("validate_goal", "done", {"task_count": 3, "duration_ms": 12}),
        _tool("translate_habits", "skipped", {"reason": "feature 'habits' disabled"}),
        _tool("fuse_tasks", "exploded"),  # off the contract: passed through as it came
        ("callback", "decomposing", {"task_count": 3}),
        ("progress", {"_event_type": "something_new", "phase": "planning"}),
        ("progress", "not json at all"),
        ("node", "planning_module", {
            "schedule": {"horizon_start": "2026-11-16T08:00:00+05:30",
                         "schedule": {"t1": {"start_min": 0, "end_min": 25}}},
            "execution_graph": {"decomposition": [{"task_id": "t1", "title": "Graphs"}]},
            "draft_id": "draft-1",
            "response_message": "Here's the plan, sir.",
            "thinking_process": "Three tasks fit before noon.",
            "saved_constraints": ["never before 11 AM"],
        }),
        ("node", "synthesize_response", {}),
        *_LEARNING,
    ], user_prompt="Plan my DSA revision")

    _assert_golden("scripted_planning_turn", raw)


@pytest.mark.asyncio
async def test_sse_golden__streamed_chat_turn_on_the_cloud__bytes_unchanged(monkeypatch, llm):
    """Thinking and message tokens through the bridge (no re-emit), a skipped learning
    step, and the step frame of a turn the model picker sent to the cloud."""
    raw = await _scripted_turn(monkeypatch, [
        ("node", "load_context", {}),
        ("node", "classify_intent", {"intent": "CHAT"}),
        ("progress", {"_event_type": "thinking_token", "token": "Hmm, "}),
        ("progress", {"_event_type": "thinking_token", "token": "a greeting."}),
        ("progress", {"_event_type": "token", "token": "Good "}),
        ("progress", {"_event_type": "token", "token": "evening, \"sir\" — café."}),
        ("node", "conversation_module", {"response_message": "Good evening.",
                                         "thinking_process": "Hmm, a greeting."}),
        ("progress", {"_event_type": "phase_end", "phase": "learning", "skipped": True}),
        ("node", "observation_loop", {"memories_extracted_count": 0,
                                      "patterns_detected_count": 0}),
    ], user_prompt="good evening", model_mode="cloud")

    _assert_golden("streamed_chat_turn_cloud", raw)


@pytest.mark.asyncio
async def test_sse_golden__consent_requested__bytes_unchanged(monkeypatch, llm):
    """A module held for consent, and the pipeline branch emitting the graph's reply."""
    raw = await _scripted_turn(monkeypatch, [
        ("node", "classify_intent", {"intent": "KNOWLEDGE_INGESTION"}),
        ("node", "knowledge_module", {"needs_consent": True,
                                      "response_message": "May I read that file, sir?"}),
    ])

    _assert_golden("consent_requested", raw)


@pytest.mark.asyncio
async def test_sse_golden__empty_chat_reply__fallback_bytes_unchanged(monkeypatch, llm):
    """The graph wrote no chat reply: the conversation module answers from the handler."""
    llm.script("voice_of_jarvis", "<think>They want a reply.</think>Good evening, sir.")
    raw = await _scripted_turn(monkeypatch, [
        ("node", "classify_intent", {"intent": "CHAT"}),
        ("node", "conversation_module", {"response_message": ""}),
    ])

    _assert_golden("empty_chat_reply_fallback", raw)


@pytest.mark.asyncio
async def test_sse_golden__pipeline_without_a_reply__synthesis_stream_bytes_unchanged(
    monkeypatch, llm
):
    """A pipeline intent with no reply: Voice of Jarvis streams one from the handler,
    its reasoning on the thinking channel."""
    llm.script(
        "voice_of_jarvis_synthesis",
        "<think>Lead with the framing.</think>Two tasks moved to tomorrow, sir.",
    )
    raw = await _scripted_turn(monkeypatch, [
        ("node", "classify_intent", {"intent": "ADD_CONSTRAINT"}),
        ("node", "planning_module", {"anti_guilt_message": "Two tasks were set aside."}),
    ])

    _assert_golden("pipeline_synthesis_fallback", raw)


@pytest.mark.asyncio
async def test_sse_golden__pipeline_with_nothing_to_say__standing_by_bytes_unchanged(
    monkeypatch, llm
):
    raw = await _scripted_turn(monkeypatch, [
        ("node", "classify_intent", {"intent": "ADD_CONSTRAINT"}),
        ("node", "planning_module", {}),
    ])

    _assert_golden("pipeline_nothing_to_say", raw)


@pytest.mark.asyncio
async def test_sse_golden__plan_that_did_not_fit__bytes_unchanged(monkeypatch, llm):
    raw = await _scripted_turn(monkeypatch, [
        ("node", "classify_intent", {"intent": "PLAN_DAY"}),
        ("node", "planning_module", {
            "error": "INFEASIBLE_EXHAUSTED",
            "clarification_request": "Thirty days is not enough room for all of it.",
            "response_message": "That's more than the month holds, sir.",
        }),
    ])

    _assert_golden("plan_did_not_fit", raw)


@pytest.mark.asyncio
async def test_sse_golden__graph_crashes__error_frame_bytes_unchanged(monkeypatch, llm):
    raw = await _scripted_turn(monkeypatch, [
        ("node", "load_context", {}),
        ("raise", RuntimeError("db password is hunter2")),
    ])

    assert "hunter2" not in raw
    _assert_golden("graph_crashed", raw)


@pytest.mark.asyncio
async def test_sse_golden__jarvis_error__envelope_frame_bytes_unchanged(monkeypatch, llm):
    raw = await _scripted_turn(monkeypatch, [
        ("raise", ConflictError("Your previous message is still being answered.")),
    ])

    _assert_golden("jarvis_error", raw)


# --- the real orchestrator: what its emitters actually put on the wire ---------------------


def _harness_state(jarvis) -> dict:
    return {name: getattr(jarvis.state, name)
            for name in ("jarvis_graph", "db_client", "container")}


@pytest.mark.asyncio
async def test_sse_golden__real_planning_turn__bytes_unchanged(
    monkeypatch, jarvis, llm, deterministic_solver
):
    llm.script("brain_dump_extraction", brain_dump(planning_goal="Revise DSA for Friday's exam"))
    llm.script("socratic_chunker", decomposition(["Graph traversal", "DP on sequences"]))

    raw = await _post(monkeypatch, _harness_state(jarvis), user_id="alice",
                      user_prompt="DSA exam on Friday. Help me revise graphs and DP.")

    _assert_golden("real_planning_turn", raw)


@pytest.mark.asyncio
async def test_sse_golden__real_learning_turn__bytes_unchanged(monkeypatch, jarvis, llm):
    """A stated preference (memory_extracted) and a pattern from three set-aside 9 AM
    slots (pattern_detected), between the learning step's start and end."""
    events = [
        TaskEvent(
            user_id="alice", task_id=f"past_{i}", event_type=TaskEventType.SKIPPED,
            occurred_at=datetime(2026, 11, 5 + i, 9, 25, tzinfo=IST),
            scheduled_start=datetime(2026, 11, 5 + i, 9, 0, tzinfo=IST),
            scheduled_end=datetime(2026, 11, 5 + i, 9, 25, tzinfo=IST),
            metadata={"title": f"Morning task {i}", "goal_id": "past", "deadline_hint": None,
                      "reason": None},
        )
        for i in range(3)
    ]
    await jarvis.state.container.repos.task_events.append_many("alice", events)
    llm.script("brain_dump_extraction", brain_dump(memories=[
        {"type": "preference", "content": "Prefers studying in the mornings", "confidence": 0.8},
    ]))

    raw = await _post(monkeypatch, _harness_state(jarvis), user_id="alice",
                      user_prompt="I prefer studying in the mornings")

    _assert_golden("real_learning_turn", raw)
