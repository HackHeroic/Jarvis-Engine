"""The chat stream's frames: one typed catalogue, encoded in one place.

Every SSE frame of ``POST /api/v1/chat/stream`` is a Pydantic model named in
``SSE_EVENTS`` and written by ``SseEncoder``. The catalogue is the wire contract the
frontend's ``consumeSSE`` parses; the golden files (``tests/test_sse_golden.py``) pin
the bytes.
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

import app.services.planning.scheduling as scheduling
import app.domain.clock as utc_clock
from app.domain.entities import TaskEvent, TaskEventType
from app.schemas.chat_stream import (
    SSE_EVENTS,
    PhaseFrame,
    SseEncoder,
    SseEvent,
    StepFrame,
    ToolUseFrame,
)
from app.schemas.errors import ErrorBody, ErrorResponse
from tests.behaviour.conftest import (  # noqa: F401 - jarvis and llm are fixtures
    brain_dump,
    decomposition,
    jarvis,
    llm,
)

IST = ZoneInfo("Asia/Kolkata")

# Every event name jarvis-frontend/lib/api.ts::consumeSSE dispatches on.
FRONTEND_EVENTS = {
    "phase", "step", "thinking", "message", "tool_use", "memory_extracted",
    "pattern_detected", "consent_request", "complete", "error",
}


def _data(frame: str) -> dict:
    return json.loads(frame.split("data: ", 1)[1].strip())


# --- the catalogue and the encoder ---------------------------------------------------------


def test_sse_catalogue__every_event_the_frontend_handles__has_a_model():
    assert {event.value for event in SSE_EVENTS} == {event.value for event in SseEvent}
    assert {event.value for event in SseEvent} == FRONTEND_EVENTS
    assert all(issubclass(model, BaseModel) for model in SSE_EVENTS.values())
    assert SSE_EVENTS[SseEvent.ERROR] is ErrorResponse


def test_encoder__frame__event_line_data_line_blank_line():
    frame = SseEncoder.frame(SseEvent.STEP, StepFrame(intent="CHAT", stage="intent_classified"))

    assert frame == 'event: step\ndata: {"intent": "CHAT", "stage": "intent_classified"}\n\n'


def test_encoder__unset_optional_fields__not_in_the_json():
    """A frame carries exactly the keys its emitter set; an explicit null stays."""
    assert _data(SseEncoder.frame(SseEvent.PHASE, PhaseFrame(phase="planning"))) == {
        "phase": "planning"
    }
    tool = ToolUseFrame(module="planning_module", tool="fuse_tasks", status="started")
    assert "detail" not in _data(SseEncoder.frame(SseEvent.TOOL_USE, tool))
    error = ErrorResponse(
        error=ErrorBody(code="conflict", message="Busy.", details=None, request_id="r1")
    )
    assert _data(SseEncoder.frame(SseEvent.ERROR, error))["error"]["details"] is None


def test_render_progress__off_contract_tool_use__passed_through_raw_and_logged(caplog):
    """Validated against its channel's model; off the contract it is logged (by event type,
    never its content) and sent exactly as it came. A frame is never dropped."""
    raw = json.dumps({
        "_event_type": "tool_use", "module": "planning_module", "tool": "secret-tool",
        "status": "exploded",
    })

    with caplog.at_level(logging.WARNING):
        frame, evt_type, token = SseEncoder.render_progress(raw)

    assert (evt_type, token) == ("tool_use", "")
    assert frame == (
        'event: tool_use\ndata: {"module": "planning_module", "tool": "secret-tool", '
        '"status": "exploded"}\n\n'
    )
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "tool_use" in record.getMessage()
    assert "secret-tool" not in record.getMessage()


# --- progress-queue entry -> SSE frame (moved from test_orchestrator.py) -------------------


def test_render_progress_event__token__lands_on_the_message_channel():
    """Streamed content tokens ride the `message` channel."""
    frame, evt_type, token = SseEncoder.render_progress(
        json.dumps({"_event_type": "token", "token": "Hel"})
    )
    assert evt_type == "token"
    assert token == "Hel"
    assert frame == 'event: message\ndata: {"token": "Hel"}\n\n'


def test_render_progress_event__thinking_token__lands_on_the_thinking_channel():
    frame, evt_type, token = SseEncoder.render_progress(
        json.dumps({"_event_type": "thinking_token", "token": "hmm"})
    )
    assert evt_type == "thinking_token"
    assert token == "hmm"
    assert frame.startswith("event: thinking\n")


def test_render_progress_event__unknown_type__never_leaks_event_type_to_the_ui():
    """The old drain loop popped _event_type off the parsed dict, then yielded
    the untouched raw string — so the internal key rode along into the phase payload."""
    frame, evt_type, _ = SseEncoder.render_progress(
        json.dumps({"_event_type": "something_new", "phase": "planning"})
    )
    assert evt_type == "phase"
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload == {"phase": "planning"}
    assert "_event_type" not in frame


def test_render_progress_event__plain_phase__passes_through():
    raw = json.dumps({"phase": "decomposing", "detail": {"n": 3}})
    frame, evt_type, token = SseEncoder.render_progress(raw)
    assert (evt_type, token) == ("phase", "")
    assert json.loads(frame.split("data: ", 1)[1].strip()) == {
        "phase": "decomposing", "detail": {"n": 3},
    }


def test_render_progress_event__malformed__still_emits_a_phase_frame():
    frame, evt_type, token = SseEncoder.render_progress("not json at all")
    assert (evt_type, token) == ("phase", "")
    assert frame == "event: phase\ndata: not json at all\n\n"


# --- every frame a real turn sends is in the catalogue -------------------------------------


@pytest.mark.asyncio
async def test_chat_turn__every_frame_emitted__parses_into_its_model(
    jarvis, llm, clock, deterministic_solver
):
    """A planning turn (tool steps, a draft, a streamed reply) and a chat turn that learns
    a memory and a pattern, through the real handler and orchestrator."""
    clock.freeze(datetime(2026, 11, 16, 8, 0, tzinfo=IST), scheduling, utc_clock)
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA for Friday's exam")
        if "DSA exam" in req.prompt
        else brain_dump(memories=[{"type": "preference",
                                   "content": "Prefers studying in the mornings",
                                   "confidence": 0.8}]),
    )
    llm.script("socratic_chunker", decomposition(["Graph traversal", "DP on sequences"]))
    await jarvis.state.container.repos.task_events.append_many("alice", [
        TaskEvent(
            user_id="alice", task_id=f"past_{i}", event_type=TaskEventType.SKIPPED,
            occurred_at=datetime(2026, 11, 5 + i, 9, 25, tzinfo=IST),
            scheduled_start=datetime(2026, 11, 5 + i, 9, 0, tzinfo=IST),
            scheduled_end=datetime(2026, 11, 5 + i, 9, 25, tzinfo=IST),
        )
        for i in range(3)
    ])

    planning = await jarvis.say("DSA exam on Friday. Help me revise graphs and DP.")
    chat = await jarvis.say("I prefer studying in the mornings")

    events = planning.events + chat.events
    seen = {event for event, _ in events}
    assert {"phase", "step", "tool_use", "message", "memory_extracted", "pattern_detected",
            "complete"} <= seen
    for event, data in events:
        SSE_EVENTS[SseEvent(event)].model_validate(data)
