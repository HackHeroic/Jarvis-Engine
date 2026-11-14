"""The error envelope, ``{"error": {"code", "message", "details", "request_id"}}``.

One model for every error a client can receive: the HTTP handlers in
``app/api/error_handlers.py`` answer with it, and the chat stream sends it as its
``error`` frame (``SSE_EVENTS[SseEvent.ERROR]``), so the two cannot drift apart.
"""

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable error code, e.g. 'not_found'.")
    message: str = Field(description="Human-readable explanation, safe to show the user.")
    details: dict | None = Field(
        default=None, description="Structured context, e.g. field errors; null when none."
    )
    request_id: str = Field(description="This request's id, also sent as the X-Request-ID header.")


class ErrorResponse(BaseModel):
    error: ErrorBody = Field(description="The error that ended the request.")
