"""What the provider adapters share: error translation, schema coercion, token reporting.

Error translation decides whether a failure may fall over to the next provider. Only
failures of the model service itself become ``LLMError``s; anything else (a
``TypeError``, a ``KeyError``) is a bug in our code and propagates unchanged, so it
can never quietly ship the prompt to the cloud.
"""

import asyncio
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx
import openai
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.domain.errors import (
    LLMError,
    LLMOutputParseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

_FENCE_RE = re.compile(r"```json|```")


def strip_fences(text: str) -> str:
    """Remove markdown code fences local models wrap around JSON."""
    return _FENCE_RE.sub("", text).strip()


def coerce_to_schema(result: Any, response_schema: type[BaseModel]) -> Any:
    """Normalise whatever the transport returned into a schema instance.

    ``litellm_conf.hybrid_route_query`` returns ``parsed.model_dump()`` — a plain dict —
    for every structured call, while a fake or another transport may hand back the
    model itself or raw JSON text. Callers branch on ``isinstance(result, Schema)``,
    so the adapters owe them one shape (F1: an unvalidated dict emptied a plan).

    Raises ``ValidationError`` / ``JSONDecodeError`` on malformed input.
    """
    if isinstance(result, response_schema):
        return result
    if isinstance(result, str):
        return response_schema.model_validate_json(strip_fences(result))
    if isinstance(result, dict):
        return response_schema.model_validate(result)
    return result


def to_llm_error(
    exc: Exception, parse_errors: tuple[type[Exception], ...] = ()
) -> LLMError | None:
    """Return the ``LLMError`` that ``exc`` means, or None if it is not a model failure.

    Timeouts → ``LLMTimeoutError``; connection failures, 429 and 5xx →
    ``LLMUnavailableError`` (both retryable); any other status the provider reports
    (bad request, unknown model, context too long, auth) → plain ``LLMError``, which
    falls over but is never retried; unparseable structured output →
    ``LLMOutputParseError``.
    """
    details = {"cause": type(exc).__name__}
    if isinstance(exc, LLMError):
        return exc
    # Timeouts first: openai.APITimeoutError is an APIConnectionError and
    # httpx.TimeoutException is a TransportError.
    if isinstance(exc, (asyncio.TimeoutError, openai.APITimeoutError, httpx.TimeoutException)):
        return LLMTimeoutError("The language model took too long to answer.", details=details)
    if isinstance(exc, (openai.APIConnectionError, httpx.TransportError, ConnectionError)):
        return LLMUnavailableError("The language model could not be reached.", details=details)
    if isinstance(exc, openai.APIError):
        status = getattr(exc, "status_code", None)
        details["status"] = status
        if isinstance(status, int) and (status == 429 or status >= 500):
            return LLMUnavailableError("The language model is busy or failing.", details=details)
        return LLMError("The language model rejected the request.", details=details)
    if isinstance(exc, (PydanticValidationError, json.JSONDecodeError, *parse_errors)):
        return LLMOutputParseError(
            "The language model's answer could not be understood.", details=details
        )
    return None


@contextmanager
def translated_errors(*, parse_errors: tuple[type[Exception], ...] = ()) -> Iterator[None]:
    """Re-raise model failures inside the block as ``LLMError``s; let anything else through."""
    try:
        yield
    except Exception as exc:
        llm_error = to_llm_error(exc, parse_errors)
        if llm_error is None or llm_error is exc:
            raise
        raise llm_error from exc


# --- token usage ------------------------------------------------------------------


@dataclass
class TokenTally:
    """Token counts a transport reported during one metered call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


_TOKEN_TALLY: ContextVar[TokenTally | None] = ContextVar("llm_token_tally", default=None)


@contextmanager
def collecting_tokens(tally: TokenTally) -> Iterator[None]:
    """Route ``report_token_usage`` calls made inside the block into ``tally``.

    Set and reset within one step: the block must not span a ``yield`` of an async
    generator, or the variable would leak into (and be reset from) another context.
    """
    reset = _TOKEN_TALLY.set(tally)
    try:
        yield
    finally:
        _TOKEN_TALLY.reset(reset)


def report_token_usage(input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> None:
    """Add a transport's token counts to the metered call in progress; no-op outside one."""
    tally = _TOKEN_TALLY.get()
    if tally is None:
        return
    tally.input_tokens += input_tokens
    tally.output_tokens += output_tokens
    tally.cache_read_tokens += cache_read_tokens
