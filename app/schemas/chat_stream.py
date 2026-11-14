"""The frames of ``POST /api/v1/chat/stream``: one typed catalogue, encoded in one place.

Every server-sent event of a turn is a model in ``SSE_EVENTS``, keyed by its event name
(``SseEvent``), and ``SseEncoder`` writes every one of them as ``event: <name>``, a
``data:`` line of JSON and a blank line. The catalogue is the contract the frontend's
``consumeSSE`` (``jarvis-frontend/lib/api.ts``) parses: a frame carries exactly the keys
its emitter set, so the bytes on the wire are what they were before the models existed
(pinned by ``tests/test_sse_golden.py``).

- The handler's own frames (phase, step, thinking, message, consent_request, complete,
  error) are built from the models.
- The progress queue's entries (tool steps, learning moments, streamed tokens, phases a
  module reports) are validated against their channel's model by
  ``SseEncoder.render_progress``. One that does not fit is logged by its event type and
  sent as it came: a frame is never dropped.

The ``complete`` frame is ``ChatCompletePayload``, validated before it is sent, so the
contract the frontend is built against is explicit and cannot drift: the fields are
exactly the ones it reads today, none renamed. The ``error`` frame is the HTTP error
envelope itself (``app/schemas/errors.py::ErrorResponse``).

**The vocabulary.** A record's id is named ``<kind>_id`` wherever it is referred to
(``draft_id``, ``memory_id``, ``message_id``; ``conversation_id`` is the chat session's
id, the one the frontend sends back); it is a bare ``id`` only on a list item that is
the record itself (a rule or a suggestion on the You page). Where a memory came from is
``provenance``: ``said`` (the user's own words) or ``inferred`` (Jarvis noticed it from
what happened). Work that did not happen is "set aside", never anything that blames.
"""

import json
import logging
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import MAX_CHAT_PROMPT_CHARS, MAX_CHAT_UPLOAD_BYTES
from app.domain.entities import MemorySource, MemoryType
from app.schemas.errors import ErrorResponse

logger = logging.getLogger(__name__)


class ChatStreamRequest(BaseModel):
    """Request body for ``POST /api/v1/chat/stream``: one message, and optionally a file.

    A message longer than ``MAX_CHAT_PROMPT_CHARS`` or a file that decodes to more than
    ``MAX_CHAT_UPLOAD_BYTES`` is refused with a 422 before the turn starts.
    """

    user_prompt: str = Field(
        ..., max_length=MAX_CHAT_PROMPT_CHARS, description="The user's message or goal"
    )
    user_id: str = Field(..., description="User identifier for habits and storage")
    conversation_id: str | None = Field(
        default=None,
        description="Session ID for conversation continuity. If None, auto-creates a new session.",
    )
    file_base64: str | None = Field(
        default=None,
        description="Base64-encoded PDF or image. Combined with user_prompt for brain dump.",
    )
    media_type: str | None = Field(
        default=None,
        description="One of 'pdf', 'image', 'png', 'jpeg'. Required when file_base64 is set.",
    )
    file_name: str | None = Field(
        default=None,
        description="Original filename of the uploaded file (for document management).",
    )
    model_mode: str | None = Field(
        default="auto",
        description=(
            "Which models answer this turn. 'auto' (the default): the local model first, "
            "the cloud providers when it cannot answer (cloud only while no local model is "
            "loaded). 'cloud' (also 'gemini', and the older '4b'): the cloud providers only, "
            "for this turn. Any other value reads as 'auto'."
        ),
    )

    @field_validator("file_base64")
    @classmethod
    def _file_within_the_limit(cls, value: str | None) -> str | None:
        if value is not None and _decoded_size(value) > MAX_CHAT_UPLOAD_BYTES:
            limit_mb = MAX_CHAT_UPLOAD_BYTES // (1024 * 1024)
            raise ValueError(f"The file can be at most {limit_mb} MB.")
        return value


def _decoded_size(encoded: str) -> int:
    """The bytes a base64 string decodes to (exact for well-formed base64)."""
    padding = len(encoded) - len(encoded.rstrip("="))
    return len(encoded) * 3 // 4 - padding


class MemoryShown(BaseModel):
    """A memory the model was given about the user this turn."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(description="The memory's id (as in /memories).")
    memory_type: MemoryType = Field(description="What kind of thing is remembered.")
    content: str = Field(description="The memory in words.")
    confidence: float = Field(description="How sure Jarvis is that it holds, 0 to 1.")
    provenance: MemorySource = Field(
        description="'said': the user said it. 'inferred': Jarvis noticed it."
    )


class PearlInsight(BaseModel):
    """Something PEARL noticed in what happened to the user's tasks."""

    model_config = ConfigDict(extra="forbid")

    insight: str = Field(description="What Jarvis tells the user, in words without blame.")
    confidence: float = Field(description="How consistent the behaviour is, 0 to 1.")
    occurrences: int = Field(description="How many outcomes it rests on.")
    memory_id: str | None = Field(
        description="The memory that holds the pattern, to confirm or dismiss."
    )


class GenerationMetrics(BaseModel):
    """How the reply was generated."""

    model_config = ConfigDict(extra="forbid")

    total_tokens: int = Field(description="Tokens (or frames) the reply streamed.")
    total_time_s: float = Field(description="The generation window, in seconds.")
    tok_per_sec: int | float = Field(description="total_tokens over total_time_s; 0 when no time.")
    ttft_ms: float | None = Field(description="Milliseconds to the first token; null if none.")
    model: str = Field(description="The model that wrote the reply, or how it was made.")


class ChatCompletePayload(BaseModel):
    """The turn's last frame (``event: complete``): the reply and everything around it."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="The turn's classified intent (e.g. PLAN_DAY, CHAT).")
    message: str = Field(description="Jarvis's reply, as saved to the conversation.")
    schedule: dict | None = Field(
        description="The proposed schedule (horizon_start + task_id -> minutes), when the "
        "turn planned; null otherwise."
    )
    execution_graph: dict | None = Field(
        description="The decomposition behind the schedule, when the turn planned."
    )
    ingestion_result: dict | None = Field(description="What a document upload produced.")
    action_proposals: list[dict] | None = Field(
        description="Proposed action items; null on this route."
    )
    search_result: dict | None = Field(description="Web search results; null on this route.")
    suggested_action: Literal["replan"] | None = Field(
        description="'replan' when a rule was saved or work was set aside: offer a new plan."
    )
    thinking_process: str | None = Field(
        description="The model's own reasoning, when it gave any; never made up."
    )
    schedule_status: Literal["draft", "infeasible"] | None = Field(
        description="'draft': a plan awaits the user's accept (draft_id); 'infeasible': it "
        "did not fit (conflict_summary says why); null when the turn did not plan."
    )
    conflict_summary: str | None = Field(
        description="Why the plan did not fit, without blame; only when infeasible."
    )
    draft_id: str | None = Field(
        description="The draft this turn proposed (accept it at /drafts/{draft_id}/accept); "
        "null for a turn that proposed none."
    )
    saved_constraints: list[str] | None = Field(
        description="The rules this turn saved, in the user's words."
    )
    conversation_id: str | None = Field(
        description="The chat session's id: the frontend sends it back on the next turn."
    )
    message_id: str | None = Field(description="The saved reply's id.")
    clarification_options: list[str] | None = Field(
        description="Quick replies; null on this route."
    )
    memories: list[MemoryShown] | None = Field(
        description="What the model was given about the user this turn; null for none."
    )
    since_last_spoke: str | None = Field(
        description="What the model was told about the last conversation, or null."
    )
    pearl_insights: list[PearlInsight] | None = Field(
        description="What PEARL noticed and has confirmed enough to say; null for none."
    )
    generation_metrics: GenerationMetrics = Field(description="How the reply was generated.")


# --- the frames before ``complete`` ---------------------------------------------------------


class SseEvent(str, Enum):
    """The event name of every frame the chat stream sends."""

    PHASE = "phase"
    STEP = "step"
    THINKING = "thinking"
    MESSAGE = "message"
    TOOL_USE = "tool_use"
    MEMORY_EXTRACTED = "memory_extracted"
    PATTERN_DETECTED = "pattern_detected"
    CONSENT_REQUEST = "consent_request"
    COMPLETE = "complete"
    ERROR = "error"


class PhaseFrame(BaseModel):
    """A stage of the turn began or ended (``event: phase``): an orchestrator node, or a
    phase a module reports. Its emitter may add keys of its own (``skipped``, ``status``)."""

    model_config = ConfigDict(extra="allow")

    phase: str = Field(description="The stage, e.g. 'planning' or 'learning'.")
    verb: str | None = Field(default=None, description="The spinner verb shown while it runs.")
    detail: dict | None = Field(
        default=None, description="What the stage found (an intent, counts, a module)."
    )


class StepFrame(BaseModel):
    """The turn's intent was classified, or the graph run is over (``event: step``)."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="The turn's classified intent (e.g. PLAN_DAY, CHAT).")
    stage: Literal["intent_classified", "pipeline_done"] = Field(
        description="'intent_classified' once the intent is known; 'pipeline_done' after the run."
    )
    model_mode: str | None = Field(
        default=None, description="The model picker's mode for this turn (pipeline_done only)."
    )
    synthesis_model: str | None = Field(
        default=None, description="The model that wrote the reply (pipeline_done only)."
    )
    force_cloud: bool | None = Field(
        default=None, description="Whether the turn ran on the cloud providers only."
    )


class TokenFrame(BaseModel):
    """A piece of the reply (``event: message``) or of the model's reasoning (``thinking``)."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(description="The text, appended to what came before.")


class ToolUseFrame(BaseModel):
    """A module step started, finished, failed or did not run (``event: tool_use``)."""

    model_config = ConfigDict(extra="forbid")

    module: str = Field(description="The module the step belongs to.")
    tool: str = Field(description="The step's name.")
    status: Literal["started", "done", "error", "skipped"] = Field(
        description="'skipped': the step did not run; detail.reason says why."
    )
    detail: dict | None = Field(
        default=None, description="What the step reports: counts, a model, a reason."
    )


class MemoryExtractedFrame(BaseModel):
    """A memory the turn created, reinforced or superseded (``event: memory_extracted``)."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(description="The memory's id (as in /memories).")
    type: MemoryType = Field(description="What kind of thing is remembered.")
    content: str = Field(description="The memory in words.")
    confidence: float = Field(description="How sure Jarvis is that it holds, 0 to 1.")
    provenance: MemorySource = Field(description="'said': the user said it.")
    action: Literal["created", "reinforced", "superseded"] = Field(
        description="What the turn did to the memory."
    )


class PatternDetectedFrame(BaseModel):
    """A learning moment: a pattern PEARL created or strengthened (``event: pattern_detected``)."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="The detector that found it.")
    content: str = Field(description="The pattern in words, without blame.")
    confidence: float = Field(description="How consistent the behaviour is, 0 to 1.")
    occurrence_count: int = Field(description="How many outcomes it rests on.")
    action: Literal["created", "strengthened"] = Field(
        description="'created': newly remembered; 'strengthened': seen again."
    )
    pattern_key: str | None = Field(description="The pattern's stable key, when it has one.")
    memory_id: str = Field(description="The memory that holds the pattern.")


class ConsentRequestFrame(BaseModel):
    """The turn paused before a step that needs the user's say-so (``event: consent_request``)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(description="Why consent is needed, in words for the user.")
    module: str = Field(description="The orchestrator node that asked.")


# The catalogue: every frame the stream sends, by event name. docs/capstone 07 cites it.
SSE_EVENTS: dict[SseEvent, type[BaseModel]] = {
    SseEvent.PHASE: PhaseFrame,
    SseEvent.STEP: StepFrame,
    SseEvent.THINKING: TokenFrame,
    SseEvent.MESSAGE: TokenFrame,
    SseEvent.TOOL_USE: ToolUseFrame,
    SseEvent.MEMORY_EXTRACTED: MemoryExtractedFrame,
    SseEvent.PATTERN_DETECTED: PatternDetectedFrame,
    SseEvent.CONSENT_REQUEST: ConsentRequestFrame,
    SseEvent.COMPLETE: ChatCompletePayload,
    SseEvent.ERROR: ErrorResponse,
}

# Progress-queue ``_event_type`` -> the frame's event. Token events land on the ``message``
# and ``thinking`` channels, so a streamed reply is many ``event: message`` frames. Any
# other type (or none) is a phase.
_PROGRESS_CHANNELS: dict[str, SseEvent] = {
    "tool_use": SseEvent.TOOL_USE,
    "memory_extracted": SseEvent.MEMORY_EXTRACTED,
    "pattern_detected": SseEvent.PATTERN_DETECTED,
    "token": SseEvent.MESSAGE,
    "thinking_token": SseEvent.THINKING,
}
_TOKEN_TYPES = ("token", "thinking_token")


class SseEncoder:
    """Writes every frame of the chat stream."""

    @staticmethod
    def frame(event: SseEvent, payload: BaseModel) -> str:
        """``event: <name>``, ``data: <json>``, a blank line. The JSON carries exactly the
        fields the payload was given (``exclude_unset``), as the emitter's dict did."""
        data = json.dumps(payload.model_dump(mode="json", exclude_unset=True))
        return f"event: {event.value}\ndata: {data}\n\n"

    @staticmethod
    def render_progress(raw: Any) -> tuple[str, str, str]:
        """One progress-queue entry as (sse_frame, event_type, token).

        ``token`` is "" for anything that is not a token event. An unknown or absent
        ``_event_type`` is a phase frame, re-dumped from the parsed dict: the raw string
        would leak the internal ``_event_type`` key into the phase payload. An entry
        that is not JSON is sent as a phase frame as it came; one off its channel's
        model is logged by its event type and sent as it came.
        """
        try:
            parsed = json.loads(raw)
            evt_type = parsed.pop("_event_type", "phase")
        except (json.JSONDecodeError, TypeError):
            return f"event: phase\ndata: {raw}\n\n", "phase", ""

        channel = _PROGRESS_CHANNELS.get(evt_type)
        if channel is None:
            channel, evt_type = SseEvent.PHASE, "phase"
        is_token = evt_type in _TOKEN_TYPES
        body = {"token": parsed.get("token", "")} if is_token else parsed
        token = body["token"] if is_token else ""
        try:
            payload = SSE_EVENTS[channel].model_validate(body)
        except ValidationError:
            logger.warning(
                "Progress entry %r is off the %s frame's contract; sent as it came",
                evt_type, channel.value,
            )
            return f"event: {channel.value}\ndata: {json.dumps(body)}\n\n", evt_type, token
        return SseEncoder.frame(channel, payload), evt_type, token
