"""Claude in the cloud, reached through the Anthropic SDK.

Built so the API credits buy answers, not waste:

* the system prompt goes out as one cached block, so a repeated prompt is billed at
  the cache-read rate — provided the caller keeps it byte-stable (volatile context
  belongs in the user message);
* thinking effort follows the role (``CLAUDE_EFFORT_BY_ROLE``);
* a structured request uses the API's structured outputs, so the reply is
  schema-valid by construction — no fence stripping, no repair loop;
* every reply the API bills is metered, including one that is then discarded (cut
  off, refused, or failing its schema);
* the SDK never retries (``max_retries=0``) because ``RetryingProvider`` owns retries;
  a rejected credential switches the provider off for the rest of the process, and a
  refusal is never asked again;
* a policy refusal is first re-run server-side on Anthropic's recommended fallback
  model (``fallbacks: "default"``) within the same call.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType
from typing import Any

import anthropic
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.domain.entities import LLMLocation
from app.domain.errors import (
    LLMError,
    LLMOutputParseError,
    LLMRefusedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.transport import report_token_usage

logger = logging.getLogger(__name__)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Thinking counts toward max_tokens. Output is billed as generated, so these are
# ceilings with room to spare; a reply that reaches one is discarded, never parsed.
_MAX_TOKENS_FAST_STRUCTURED = 2048
_MAX_TOKENS_FAST_TEXT = 4096
_MAX_TOKENS_DEFAULT = 8000

_shared_client: anthropic.AsyncAnthropic | None = None

# The model that answered the call in progress — not CLAUDE_MODEL when the
# server-side fallback served it. Per task, so concurrent requests never mix.
_served_model: ContextVar[str | None] = ContextVar("claude_served_model", default=None)


def build_client(cfg: ModuleType) -> anthropic.AsyncAnthropic:
    """Return a client for the configured credential (API key first) that never retries."""
    if cfg.ANTHROPIC_API_KEY:
        return anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY, max_retries=0)
    return anthropic.AsyncAnthropic(auth_token=cfg.ANTHROPIC_AUTH_TOKEN, max_retries=0)


def _default_client(cfg: ModuleType) -> anthropic.AsyncAnthropic:
    """The process-wide client, built on first use."""
    global _shared_client
    if _shared_client is None:
        _shared_client = build_client(cfg)
    return _shared_client


class ClaudeProvider(LLMProvider):
    """Claude via the Messages API; one model (``CLAUDE_MODEL``) serves every role.

    ``cfg`` is the config module, read at call time. ``client`` stands in for the
    process-wide ``AsyncAnthropic`` (tests pass one wired to a mock transport).
    """

    name = "claude"
    location = LLMLocation.CLOUD

    def __init__(self, cfg: ModuleType, client: Any = None) -> None:
        self._cfg = cfg
        self._injected_client = client
        self._disabled = False

    def model_for_role(self, role: str) -> str:
        """The model to record for the call in this task: the one that answered, if any.

        That is ``CLAUDE_MODEL`` unless the server-side fallback served the reply.
        ``MeteredProvider`` asks once the call has finished.
        """
        return _served_model.get() or self._cfg.CLAUDE_MODEL

    def is_available(self) -> bool:
        """Available while a credential is configured and the API has not rejected it."""
        cfg = self._cfg
        return bool(cfg.ANTHROPIC_API_KEY or cfg.ANTHROPIC_AUTH_TOKEN) and not self._disabled

    def _client(self) -> Any:
        if self._injected_client is not None:
            return self._injected_client
        return _default_client(self._cfg)

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        _served_model.set(None)
        messages_api = self._client().beta.messages
        params = self._params(req)
        if req.response_schema is None:
            with self._translated_errors():
                reply = await messages_api.create(**params)
            body = reply.to_dict()
            _meter(body)
            _check_stop(body)
            return "".join(block.text for block in reply.content if block.type == "text")

        # messages.parse validates the reply inside the SDK call, so a cut-off or
        # malformed reply would raise before its billed usage could be read. The raw
        # response gives the body first: meter it, check why it stopped, then parse.
        with self._translated_errors():
            raw = await messages_api.with_raw_response.parse(
                **params, output_format=req.response_schema
            )
            body = await raw.json()
        _meter(body)
        _check_stop(body)
        with self._translated_errors():
            parsed = (await raw.parse()).parsed_output
        if not isinstance(parsed, req.response_schema):
            raise LLMOutputParseError(
                "The language model's answer could not be understood.",
                details={"cause": "no_parsed_output"},
            )
        return parsed

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        _served_model.set(None)
        sent_text = False
        with self._translated_errors():
            async with self._client().beta.messages.stream(**self._params(req)) as reply_stream:
                async for text in reply_stream.text_stream:
                    if text:
                        sent_text = True
                        yield ("content", text)
                reply = await reply_stream.get_final_message()
        body = reply.to_dict()
        _meter(body)
        if body.get("stop_reason") == "max_tokens" and sent_text:
            # The user has already read the text, and there is nothing to parse.
            logger.warning("Claude reply for %s stopped at max_tokens", req.task)
            return
        _check_stop(body)

    def _params(self, req: LLMRequest) -> dict[str, Any]:
        """Keyword arguments for the SDK call. Never ``thinking``, sampling or ``tool_choice``."""
        params: dict[str, Any] = {
            "model": self._cfg.CLAUDE_MODEL,
            "max_tokens": self._max_tokens(req),
            "messages": _messages(req),
            "output_config": {"effort": self._cfg.CLAUDE_EFFORT_BY_ROLE[req.role]},
            "fallbacks": "default",
            "betas": [_FALLBACK_BETA],
            "timeout": req.timeout_s,
        }
        if req.system_prompt:
            params["system"] = [
                {"type": "text", "text": req.system_prompt, "cache_control": {"type": "ephemeral"}}
            ]
        return params

    def _max_tokens(self, req: LLMRequest) -> int:
        if req.role != "fast":
            return _MAX_TOKENS_DEFAULT
        if req.response_schema is not None:
            return _MAX_TOKENS_FAST_STRUCTURED
        return _MAX_TOKENS_FAST_TEXT

    @contextmanager
    def _translated_errors(self) -> Iterator[None]:
        """Re-raise SDK failures as ``LLMError``s, most specific first.

        A ``BadRequestError`` is a request we built wrong — our bug — so it propagates.
        """
        try:
            yield
        except anthropic.APITimeoutError as exc:  # an APIConnectionError, so it goes first
            raise LLMTimeoutError(
                "The language model took too long to answer.", details=_cause(exc)
            ) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            # An expired or revoked credential fails every call alike: one failure is enough.
            self._disabled = True
            logger.error("Claude rejected its credential (HTTP %s); Claude is off", exc.status_code)
            raise LLMUnavailableError(
                "The language model rejected our credentials.", details=_cause(exc)
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError(
                "The language model is busy or failing.", details=_cause(exc)
            ) from exc
        except anthropic.BadRequestError:
            raise
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailableError(
                    "The language model is busy or failing.", details=_cause(exc)
                ) from exc
            raise LLMError("The language model rejected the request.", details=_cause(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError(
                "The language model could not be reached.", details=_cause(exc)
            ) from exc
        except PydanticValidationError as exc:  # the reply's JSON does not fit the schema
            raise LLMOutputParseError(
                "The language model's answer could not be understood.", details=_cause(exc)
            ) from exc


def _messages(req: LLMRequest) -> list[dict]:
    """The prior turns, then the prompt as the last user turn.

    Only ``role`` and ``content`` are sent. A ``system`` entry in the history goes to
    the front of the user message, not into the cached system block: history notes
    (e.g. how many earlier messages exist) change from turn to turn, and the system
    block must stay byte-stable. The API wants a user turn first, so a window that
    opens on an assistant reply drops that reply.
    """
    notes: list[str] = []
    turns: list[dict] = []
    for message in req.conversation_history or []:
        role, content = message.get("role"), message.get("content")
        if not content:
            continue
        if role == "system" and isinstance(content, str):
            notes.append(content)
        elif role in ("user", "assistant"):
            turns.append({"role": role, "content": content})
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)
    turns.append({"role": "user", "content": "\n\n".join([*notes, req.prompt])})
    return turns


def _meter(body: dict) -> None:
    """Record a billed reply's usage, and the model that wrote it, for ``MeteredProvider``.

    ``input_tokens`` is the whole prompt: uncached, written to cache, and read from
    cache. The tally has no cache-write field, so writes are counted there only;
    cache reads are also reported on their own.
    """
    usage = body.get("usage") or {}
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    report_token_usage(
        input_tokens=(usage.get("input_tokens") or 0) + cache_write + cache_read,
        output_tokens=usage.get("output_tokens") or 0,
        cache_read_tokens=cache_read,
    )
    _served_model.set(body.get("model") or None)


def _check_stop(body: dict) -> None:
    """A refusal (after the server-side fallback) or a cut-off reply is not an answer."""
    stop_reason = body.get("stop_reason")
    if stop_reason == "refusal":
        category = (body.get("stop_details") or {}).get("category")
        raise LLMRefusedError(
            "The language model declined this request.",
            details={"cause": "refusal", "category": category},
        )
    if stop_reason == "max_tokens":
        raise LLMOutputParseError(
            "The language model's answer was cut off.", details={"cause": "max_tokens"}
        )


def _cause(exc: Exception) -> dict:
    details: dict[str, Any] = {"cause": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if status is not None:
        details["status"] = status
    return details
