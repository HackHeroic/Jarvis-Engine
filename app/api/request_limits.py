"""The request body cap: a body larger than any valid request is refused before it is read.

The field limits (``ChatStreamRequest``, ``app/schemas/upload.py``: an 8,000-character
message, a file of at most 10 MB) are checked by Pydantic, after FastAPI has read and
parsed the whole body. ``BodyLimitMiddleware`` stops a far larger body first: on its
declared ``Content-Length`` without reading it, or, sent chunked with no length, as soon
as the bytes received pass the cap. Either way the answer is the one envelope, 413
``payload_too_large``, and no route runs.
"""

from collections.abc import Mapping
from uuid import uuid4

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.error_handlers import jarvis_error_response
from app.core.config import MAX_CHAT_PROMPT_CHARS, MAX_CHAT_UPLOAD_BYTES
from app.domain.errors import PayloadTooLargeError

# The largest file a request may carry, base64-encoded as it arrives (4 bytes per 3).
_LARGEST_FILE_FIELD = 4 * -(-MAX_CHAT_UPLOAD_BYTES // 3)
# Room for everything else: the message with every character JSON-escaped (an emoji
# is 12 bytes, "\ud83d\ude00", plus its quotes), the ids, the field names.
_HEADROOM = MAX_CHAT_PROMPT_CHARS * 14 + 64 * 1024
# The biggest body the field limits allow, and then some: about 14 MB.
MAX_REQUEST_BODY_BYTES = _LARGEST_FILE_FIELD + _HEADROOM

# The routes that take an upload, each capped at the largest valid body.
CAPPED_ROUTES: dict[str, int] = {
    "/api/v1/chat/stream": MAX_REQUEST_BODY_BYTES,
    "/api/v1/ingestion/process": MAX_REQUEST_BODY_BYTES,
}

_TOO_LARGE = "The request is too large."


class BodyLimitMiddleware:
    """Caps the body of the requests to ``limits``' paths (path -> most bytes).

    Pure ASGI, so the chat stream's response passes through untouched. A body within the
    cap reaches the route exactly as sent; a chunked one is read here first (the route
    would read all of it anyway) and handed on whole.
    """

    def __init__(self, app: ASGIApp, limits: Mapping[str, int]) -> None:
        self.app = app
        self.limits = dict(limits)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self.limits.get(scope.get("path", "")) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            # The server holds the body to its declared length, so a length within the
            # cap is a body within it.
            if declared.isdigit() and int(declared) > limit:
                await self._refuse(scope, receive, send, limit)
                return
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                return
            body += message.get("body", b"")
            if len(body) > limit:
                await self._refuse(scope, receive, send, limit)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()  # afterwards: the disconnect a stream listens for

        await self.app(scope, replay, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        # The id RequestIdMiddleware (outside this one) gave the request.
        request_id = scope.get("state", {}).get("request_id") or uuid4().hex
        response = jarvis_error_response(
            request_id, PayloadTooLargeError(_TOO_LARGE, details={"max_bytes": limit})
        )
        await response(scope, receive, send)
