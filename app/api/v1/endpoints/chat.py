"""The chat endpoint: ``POST /api/v1/chat/stream``, the one way to talk to Jarvis.

Routing and parsing only. The turn itself (the LangGraph orchestrator run, its lock, its
frames) is ``ChatTurnService`` (``app/services/chat_turn/``); every frame is a model in
``app/schemas/chat_stream.py``.
"""

from fastapi import APIRouter, Request
from starlette.background import BackgroundTask

from app.api.error_handlers import json_error_responses, request_id_of
from app.api.sse import EventStreamResponse
from app.core.config import MAX_CHAT_PROMPT_CHARS, MAX_CHAT_UPLOAD_BYTES
from app.schemas.chat_stream import ChatCompletePayload, ChatStreamRequest
from app.services.chat_turn import ChatTurnService

router = APIRouter()

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


@router.post(
    "/stream",
    summary="Talk to Jarvis (server-sent events)",
    description=(
        "THE entry point. Streams phase / step / thinking / message / tool_use / "
        "memory_extracted / pattern_detected / consent_request frames (the frame "
        "catalogue, SSE_EVENTS in app/schemas/chat_stream.py), then one `complete` frame "
        "whose data is a `ChatCompletePayload` (the 200 schema below), or one `error` "
        "frame in the standard envelope (`ErrorResponse`). A message over "
        f"{MAX_CHAT_PROMPT_CHARS:,} characters or a file over "
        f"{MAX_CHAT_UPLOAD_BYTES // (1024 * 1024)} MB is a 422."
    ),
    response_class=EventStreamResponse,
    responses={
        200: {
            "model": ChatCompletePayload,
            "description": "The event stream; its last frame is `complete`, whose data is this.",
        },
        **json_error_responses(),  # a refusal before the stream starts is the JSON envelope
    },
)
async def chat_stream(
    request: ChatStreamRequest, http_request: Request
) -> EventStreamResponse:
    """One turn, streamed. Opened here, in the handler's context, so the stream inherits
    the turn's cloud flag and usage attribution; the background task ends them once the
    last frame is sent or the client has gone."""
    turn = await ChatTurnService.for_app(http_request.app.state).open_turn(
        request, request_id=request_id_of(http_request)
    )
    return EventStreamResponse(
        turn.frames(),
        headers=_SSE_HEADERS,
        background=BackgroundTask(turn.finish),
    )
