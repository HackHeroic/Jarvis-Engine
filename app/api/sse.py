"""A streamed response that always closes its stream, at once, however the response ends.

When a client leaves (or stops reading) while a frame waits to be sent, Starlette
cancels the ``send``, not the body's async generator: the generator stays parked at
its ``yield`` and its ``finally`` (for a chat turn: stop the graph run, release the
conversation's lock) waits for the garbage collector. ``ClosingStreamingResponse``
closes the generator itself, inside a shielded scope, so that cleanup runs before the
response returns.
"""

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Send


class ClosingStreamingResponse(StreamingResponse):
    """``StreamingResponse`` whose body iterator is closed after streaming or when cancelled."""

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # Shielded: a disconnect cancels this scope, and anyio would cancel the
            # close itself at its first await.
            with anyio.CancelScope(shield=True):
                aclose = getattr(self.body_iterator, "aclose", None)
                if aclose is not None:
                    await aclose()


class EventStreamResponse(ClosingStreamingResponse):
    """A server-sent event stream (``text/event-stream``) that always closes its stream.

    Its media type is the class's own, so the OpenAPI schema documents a route that
    returns it as an event stream.
    """

    media_type = "text/event-stream"
