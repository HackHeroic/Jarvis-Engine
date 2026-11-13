"""Decorators that add one concern each around any ``LLMProvider``.

The factory composes them per provider, outermost first:
cloud ``Redacting → Retrying → Metered → Timeout → adapter``; local
``Metered → Timeout → adapter``. So redaction happens once before any attempt, each
retry attempt gets its own timeout, every attempt that answers is metered, and the
usage event is published after the timed call returned — a slow listener can delay
the answer but never turn it into a timeout and a second, paid attempt.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, Field

from app.domain.entities import LLMLocation, LLMUsageEvent
from app.domain.errors import LLMTimeoutError
from app.domain.events import EventBus, LLMCallCompleted
from app.domain.ports import LLMProvider, LLMRequest, RetryPolicy
from app.infrastructure.llm.transport import TokenTally, collecting_tokens


class _ProviderDecorator(LLMProvider):
    """Delegates identity and availability to the wrapped provider."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def location(self) -> LLMLocation:
        return self._inner.location

    def is_available(self) -> bool:
        return self._inner.is_available()


class Redactor(Protocol):
    """Removes PII from text bound for a cloud model (the redaction port).

    Returns the text to send and how many spans were redacted. Raises an ``LLMError``
    when the text must not leave the machine at all.
    """

    async def redact(self, text: str) -> tuple[str, int]: ...


_REDACTOR_OVERRIDE: ContextVar[Redactor | None] = ContextVar("llm_redactor_override", default=None)


@contextmanager
def redacting_with(redactor: Redactor) -> Iterator[None]:
    """Make every ``RedactingProvider`` use ``redactor`` for calls made inside the block.

    Lets one call bring its own gate without rebuilding the chain. Redaction runs
    before a provider's first token, so a stream must be started inside the block.
    """
    reset = _REDACTOR_OVERRIDE.set(redactor)
    try:
        yield
    finally:
        _REDACTOR_OVERRIDE.reset(reset)


class RedactedLLMRequest(LLMRequest):
    """A request after the PreCloudLLM gate, carrying how many spans it redacted."""

    pii_redactions: int = Field(default=0, description="PII spans the gate redacted.")


class RedactingProvider(_ProviderDecorator):
    """Runs a ``Redactor`` over everything sent to a cloud model.

    Covers the prompt, the system prompt and every string ``content`` in the
    conversation history, and builds a new request — the caller's is never mutated.
    Only cloud providers are wrapped: a local model must see the user's own words.
    """

    def __init__(self, inner: LLMProvider, redactor: Redactor) -> None:
        if inner.location != LLMLocation.CLOUD:
            raise ValueError(f"RedactingProvider wraps cloud providers only, not {inner.name!r}")
        super().__init__(inner)
        self._redactor = redactor

    async def _redact(self, text: str) -> tuple[str, int]:
        if not text:
            return text, 0
        redactor = _REDACTOR_OVERRIDE.get() or self._redactor
        return await redactor.redact(text)

    async def _redacted(self, req: LLMRequest) -> RedactedLLMRequest:
        prompt, count = await self._redact(req.prompt)
        system_prompt, n = await self._redact(req.system_prompt)
        count += n
        history: list[dict] | None = None
        if req.conversation_history is not None:
            history = []
            for message in req.conversation_history:
                content = message.get("content")
                if isinstance(content, str):
                    content, n = await self._redact(content)
                    count += n
                    message = {**message, "content": content}
                history.append(message)
        fields = {name: getattr(req, name) for name in LLMRequest.model_fields}
        fields.update(
            prompt=prompt,
            system_prompt=system_prompt,
            conversation_history=history,
            pii_redactions=count,
        )
        return RedactedLLMRequest(**fields)

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        return await self._inner.complete(await self._redacted(req))

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        redacted = await self._redacted(req)
        async with aclosing(self._inner.stream(redacted)) as tokens:
            async for item in tokens:
                yield item


class RetryingProvider(_ProviderDecorator):
    """Retries the wrapped provider as ``policy`` allows.

    A stream is retried only before its first token; after that a retry would
    replay text the user has already read. A provider that turned itself
    unavailable (e.g. on a revoked key) is not retried.
    """

    def __init__(
        self,
        inner: LLMProvider,
        policy: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(inner)
        self._policy = policy
        self._sleep = sleep

    def _should_retry(self, error: Exception, attempt: int) -> bool:
        return self._policy.should_retry(error, attempt) and self._inner.is_available()

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        attempt = 1
        while True:
            try:
                return await self._inner.complete(req)
            except Exception as exc:
                if not self._should_retry(exc, attempt):
                    raise
            await self._sleep(self._policy.delay(attempt))
            attempt += 1

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        attempt = 1
        while True:
            started = False
            try:
                async with aclosing(self._inner.stream(req)) as tokens:
                    async for item in tokens:
                        started = True
                        yield item
                return
            except Exception as exc:
                if started or not self._should_retry(exc, attempt):
                    raise
            await self._sleep(self._policy.delay(attempt))
            attempt += 1


class TimeoutProvider(_ProviderDecorator):
    """A hard ceiling of ``req.timeout_s`` around the wrapped provider.

    ``complete`` is bounded as a whole. ``stream`` is bounded per token — a long
    reply that keeps flowing is fine, a stall is not.
    """

    def _timeout(self, req: LLMRequest) -> LLMTimeoutError:
        return LLMTimeoutError(
            "The language model took too long to answer.",
            details={"provider": self.name, "timeout_s": req.timeout_s},
        )

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        try:
            return await asyncio.wait_for(self._inner.complete(req), timeout=req.timeout_s)
        except asyncio.TimeoutError as exc:
            raise self._timeout(req) from exc

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        async with aclosing(self._inner.stream(req)) as tokens:
            while True:
                try:
                    item = await asyncio.wait_for(anext(tokens), timeout=req.timeout_s)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError as exc:
                    raise self._timeout(req) from exc
                yield item


def _billed(tally: TokenTally) -> bool:
    return bool(tally.input_tokens or tally.output_tokens or tally.cache_read_tokens)


class MeteredProvider(_ProviderDecorator):
    """Times each call and publishes ``LLMCallCompleted``.

    A call that answers is always published. A call that failed — raised, or a stream
    that broke or was abandoned — is published only if the transport reported tokens
    for it: those were billed even though no answer came of them. Token counts are
    whatever the transport reports through ``transport.report_token_usage`` during the
    call. ``model_for_role`` names the model the wrapped provider uses for a role.
    """

    def __init__(
        self, inner: LLMProvider, bus: EventBus, model_for_role: Callable[[str], str]
    ) -> None:
        super().__init__(inner)
        self._bus = bus
        self._model_for_role = model_for_role

    async def _publish(self, req: LLMRequest, started: float, tally: TokenTally) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        now = datetime.now(timezone.utc)
        usage = LLMUsageEvent(
            user_id=req.user_id,
            session_id=req.session_id,
            task=req.task,
            role=req.role,
            provider=self.name,
            model=self._model_for_role(req.role),
            location=self.location,
            input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens,
            cache_read_tokens=tally.cache_read_tokens,
            latency_ms=latency_ms,
            pii_redactions=req.pii_redactions if isinstance(req, RedactedLLMRequest) else 0,
            created_at=now,
        )
        await self._bus.publish(LLMCallCompleted(occurred_at=now, user_id=req.user_id, usage=usage))

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        tally = TokenTally()
        started = time.perf_counter()
        try:
            with collecting_tokens(tally):
                result = await self._inner.complete(req)
        except Exception:
            if _billed(tally):
                await self._publish(req, started, tally)
            raise
        await self._publish(req, started, tally)
        return result

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        tally = TokenTally()
        started = time.perf_counter()
        finished = False
        try:
            async with aclosing(self._inner.stream(req)) as tokens:
                while True:
                    try:
                        with collecting_tokens(tally):
                            item = await anext(tokens)
                    except StopAsyncIteration:
                        break
                    yield item
            finished = True
        finally:
            # Also runs when the consumer closes the stream early (GeneratorExit).
            if finished or _billed(tally):
                await self._publish(req, started, tally)
