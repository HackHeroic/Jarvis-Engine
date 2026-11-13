"""The single HTTP error envelope.

Every error response has one shape — ``{"error": {"code", "message", "details",
"request_id"}}`` — whether it came from a ``JarvisError``, request validation, an
``HTTPException`` or an exception nobody caught. The last kind is logged with its
request id and answered with a fixed message: exception text never goes on the wire.
"""

import logging
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.domain import errors

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_INTERNAL_ERROR_MESSAGE = "Something went wrong on our side."
_VALIDATION_MESSAGE = "The request is invalid."


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable error code, e.g. 'not_found'.")
    message: str = Field(description="Human-readable explanation, safe to show the user.")
    details: dict | None = Field(
        default=None, description="Structured context, e.g. field errors; null when none."
    )
    request_id: str = Field(description="This request's id, also sent as the X-Request-ID header.")


class ErrorResponse(BaseModel):
    error: ErrorBody = Field(description="The error that ended the request.")


class RequestIdMiddleware:
    """Gives every HTTP request an id: ``request.state.request_id`` and ``X-Request-ID``.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so SSE streams pass through
    untouched. The header is only added when absent: error responses carry it
    already, because a 500 is sent by Starlette's outermost middleware and never
    passes back through this one.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault(REQUEST_ID_HEADER, request_id)
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid4().hex


def _envelope(
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=details, request_id=request_id)
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


async def _handle_jarvis_error(request: Request, exc: errors.JarvisError) -> JSONResponse:
    request_id = _request_id(request)
    if exc.http_status >= 500:
        logger.error(
            "%s [request_id=%s] %s %s",
            exc.code, request_id, request.method, request.url.path,
            exc_info=exc,
        )
    return _envelope(request_id, exc.http_status, exc.code, exc.message, exc.details)


async def _handle_request_validation(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # loc/msg/type only: `input` would echo the caller's payload back, and `ctx`
    # can hold exception objects that are not JSON-serializable.
    fields = [
        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    return _envelope(
        _request_id(request),
        errors.ValidationError.http_status,
        errors.ValidationError.code,
        _VALIDATION_MESSAGE,
        {"errors": fields},
    )


async def _handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    message = (
        exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    )
    return _envelope(
        _request_id(request), exc.status_code, "http_error", message, headers=exc.headers
    )


async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id(request)
    logger.error(
        "Unhandled error [request_id=%s] %s %s",
        request_id, request.method, request.url.path,
        exc_info=exc,
    )
    return _envelope(
        request_id, errors.JarvisError.http_status, errors.JarvisError.code,
        _INTERNAL_ERROR_MESSAGE,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Route every error a request can end in through the one envelope."""
    app.add_exception_handler(errors.JarvisError, _handle_jarvis_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unhandled)
