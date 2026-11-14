"""The chat endpoint: ``POST /api/v1/chat/stream``, the one way to talk to Jarvis.

Routing and parsing only. The turn itself (the LangGraph orchestrator run, its lock, its
frames) is ``ChatTurnService`` (``app/services/chat_turn/``); every frame is a model in
``app/schemas/chat_stream.py``. ``/api/v1/chat/v2/stream`` is the same handler under its
old path, hidden from the schema and deprecated, kept so a frontend and a backend
restarted in either order keep talking.
"""

from fastapi import APIRouter, Request
from starlette.background import BackgroundTask

from app.api.error_handlers import request_id_of
from app.api.sse import ClosingStreamingResponse
from app.core.config import MAX_CHAT_PROMPT_CHARS, MAX_CHAT_UPLOAD_BYTES
from app.schemas.chat_stream import ChatStreamRequest
from app.services.chat_turn import ChatTurnService

router = APIRouter()

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


@router.post(
    "/stream",
    summary="Talk to Jarvis (server-sent events)",
    description=(
        "THE entry point. Streams phase / step / thinking / message / tool_use / "
        "memory_extracted / pattern_detected / consent_request frames (SSE_EVENTS in "
        "app/schemas/chat_stream.py), then one `complete` frame whose data is a "
        "`ChatCompletePayload`, or one `error` frame in the standard envelope. A message "
        f"over {MAX_CHAT_PROMPT_CHARS:,} characters or a file over "
        f"{MAX_CHAT_UPLOAD_BYTES // (1024 * 1024)} MB is a 422."
    ),
)
# The old path of this same handler, hidden and deprecated: a frontend and a backend
# restarted in either order keep talking. It goes once the new path has passed a live run.
@router.post("/v2/stream", include_in_schema=False, deprecated=True)
async def chat_stream(
    request: ChatStreamRequest, http_request: Request
) -> ClosingStreamingResponse:
    """One turn, streamed. Opened here, in the handler's context, so the stream inherits
    the turn's cloud flag and usage attribution; the background task ends them once the
    last frame is sent or the client has gone."""
    turn = await ChatTurnService.for_app(http_request.app.state).open_turn(
        request, request_id=request_id_of(http_request)
    )
    return ClosingStreamingResponse(
        turn.frames(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        background=BackgroundTask(turn.finish),
    )
