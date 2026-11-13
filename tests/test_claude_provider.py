"""ClaudeProvider: the Anthropic SDK behind the ``LLMProvider`` port.

Every test runs the real ``AsyncAnthropic`` against an ``httpx2.MockTransport``
(``FakeAPI``): the SDK builds, sends, parses, validates and raises exactly as it does
in production, but each request is answered here and nothing leaves the process.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

import app.core.config as _cfg
import app.infrastructure.llm.claude as claude_module
import app.models.brain.litellm_conf as _llm
from app.core.model_router import PreCloudLLMRedactor
from app.domain.entities import LLMLocation
from app.domain.errors import (
    LLMError,
    LLMOutputParseError,
    LLMRefusedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.domain.events import DomainEvent, EventBus, EventListener, LLMCallCompleted, NullEventBus
from app.domain.ports import LLMRequest
from app.infrastructure.llm.claude import ClaudeProvider, build_client
from app.infrastructure.llm.decorators import MeteredProvider, RetryingProvider
from app.infrastructure.llm.factory import LLMProviderFactory
from app.infrastructure.llm.retry import ExponentialBackoff
from app.infrastructure.llm.transport import TokenTally, collecting_tokens
from app.orchestrator.hooks import ActionHooks, register_all_hooks

# --- the Messages API, faked at the HTTP layer ------------------------------------


def _usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


def _reply(
    text: str = "ready",
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    usage: dict | None = None,
) -> dict:
    """A Messages API response body; like Sonnet 5.5's, it opens with an empty thinking block."""
    content: list[dict] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text:
        content.append({"type": "text", "text": text})
    refused = stop_reason == "refusal"
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": (
            {"type": "refusal", "category": "general_harms", "explanation": None} if refused else None
        ),
        "usage": usage or _usage(),
    }


@dataclass
class _Stream:
    """A streamed reply: these text deltas, then the final message ``final``."""

    deltas: list[str]
    final: dict


def _sse(stream: _Stream) -> bytes:
    final = stream.final
    start = {**final, "content": [], "stop_reason": None, "usage": {**final["usage"], "output_tokens": 1}}
    events: list[dict] = [{"type": "message_start", "message": start}]
    if stream.deltas:
        events.append(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        )
        for delta in stream.deltas:
            events.append(
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}}
            )
        events.append({"type": "content_block_stop", "index": 0})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": final["stop_reason"], "stop_sequence": None},
            "usage": final["usage"],
        }
    )
    events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _api_error(status: int, error_type: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": "rejected"}}
    )


class FakeAPI:
    """The Messages API behind an ``httpx2.MockTransport``, with a real SDK client on top.

    Each request consumes the next outcome: a response body (``dict``), a ``_Stream``,
    an ``httpx2.Response`` (e.g. an error status), or an ``httpx2`` transport error
    class to raise. With no outcomes left it answers ``_reply()``.
    """

    def __init__(self, outcomes: list | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._outcomes = list(outcomes or [])
        transport = httpx2.MockTransport(self._handle)
        self.client = anthropic.AsyncAnthropic(
            api_key="test-anthropic-key",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=transport),
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self._outcomes.pop(0) if self._outcomes else _reply()
        if isinstance(outcome, type) and issubclass(outcome, httpx2.TransportError):
            raise outcome("simulated", request=request)
        if isinstance(outcome, httpx2.Response):
            return outcome
        if isinstance(outcome, _Stream):
            return httpx2.Response(
                200, content=_sse(outcome), headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(200, json=outcome)

    @property
    def bodies(self) -> list[dict]:
        """The JSON body of every request sent."""
        return [json.loads(request.content) for request in self.requests]

    @property
    def sent(self) -> dict:
        """The JSON body of the only request sent."""
        (body,) = self.bodies
        return body


# --- helpers ---------------------------------------------------------------------


class _Tiny(BaseModel):
    color: str


class _Recorder(EventListener):
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.events.append(event)


def _recording_bus() -> tuple[EventBus, _Recorder]:
    bus = EventBus()
    recorder = _Recorder()
    bus.subscribe(LLMCallCompleted, recorder)
    return bus, recorder


def _req(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {"task": "voice_of_jarvis", "role": "fast", "prompt": "hello"}
    fields.update(overrides)
    return LLMRequest(**fields)


async def _collect(tokens: AsyncIterator[tuple[str, str]]) -> list[tuple[str, str]]:
    return [item async for item in tokens]


def _counts(tally: TokenTally) -> tuple[int, int, int]:
    return (tally.input_tokens, tally.output_tokens, tally.cache_read_tokens)


def _pii_redactor() -> PreCloudLLMRedactor:
    hooks = ActionHooks()
    register_all_hooks(hooks)
    return PreCloudLLMRedactor(hooks)


async def _no_sleep(seconds: float) -> None:
    raise AssertionError(f"retried after {seconds}s; this failure must not be retried")


@pytest.fixture
def claude_cfg(monkeypatch):
    """Config for a machine with an Anthropic API key, a local model and a Gemini key."""
    monkeypatch.setattr(_cfg, "ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(_cfg, "ANTHROPIC_AUTH_TOKEN", "")
    monkeypatch.setattr(_cfg, "CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(_cfg, "CLAUDE_PRIMARY", False)
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(_cfg, "GEMMA_PRIMARY_MODEL", "openai/google/gemma-4-26b-a4b")
    monkeypatch.setattr(_cfg, "GEMMA_FAST_MODEL", "openai/google/gemma-4-e4b")
    return _cfg


def _provider(cfg, api: FakeAPI) -> ClaudeProvider:
    return ClaudeProvider(cfg, client=api.client)


# --- request shape ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__fast_role__low_effort_and_small_max_tokens(claude_cfg):
    api = FakeAPI([_reply('{"color": "blue"}'), _reply()])
    provider = _provider(claude_cfg, api)

    await provider.complete(_req(response_schema=_Tiny))
    await provider.complete(_req())

    structured, text = api.bodies
    assert structured["output_config"]["effort"] == "low"
    assert structured["max_tokens"] == 2048
    assert text["output_config"] == {"effort": "low"}
    assert text["max_tokens"] == 4096


@pytest.mark.parametrize("role", ["primary", "cloud"])
@pytest.mark.asyncio
async def test_claude__primary_and_cloud_roles__medium_effort_and_8000_max_tokens(claude_cfg, role):
    api = FakeAPI()

    await _provider(claude_cfg, api).complete(_req(role=role, task="socratic_chunker"))

    assert api.sent["output_config"] == {"effort": "medium"}
    assert api.sent["max_tokens"] == 8000


@pytest.mark.asyncio
async def test_claude__model_and_timeout__from_config_and_request(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "CLAUDE_MODEL", "claude-from-config")
    api = FakeAPI()

    await _provider(claude_cfg, api).complete(_req(timeout_s=12.5))

    assert api.sent["model"] == "claude-from-config"
    assert api.requests[0].extensions["timeout"]["read"] == 12.5


@pytest.mark.asyncio
async def test_claude__system_prompt__sent_as_cached_block(claude_cfg):
    api = FakeAPI()

    await _provider(claude_cfg, api).complete(_req(system_prompt="You are Jarvis."))

    assert api.sent["system"] == [
        {"type": "text", "text": "You are Jarvis.", "cache_control": {"type": "ephemeral"}}
    ]


@pytest.mark.asyncio
async def test_claude__empty_system_prompt__no_system_sent(claude_cfg):
    api = FakeAPI()

    await _provider(claude_cfg, api).complete(_req(system_prompt=""))

    assert "system" not in api.sent


@pytest.mark.asyncio
async def test_claude__history__mapped_through_and_prompt_is_last_user_turn(claude_cfg):
    api = FakeAPI()
    history = [
        {"role": "user", "content": "plan my week", "created_at": "2026-11-14"},
        {"role": "assistant", "content": "Done, sir."},
    ]

    await _provider(claude_cfg, api).complete(
        _req(prompt="and Friday?", conversation_history=history)
    )

    assert api.sent["messages"] == [
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "Done, sir."},
        {"role": "user", "content": "and Friday?"},
    ]


@pytest.mark.asyncio
async def test_claude__history_system_entry__prepended_to_user_message_not_system_block(claude_cfg):
    """A history system entry may change per turn; in the system block it would bust the cache."""
    api = FakeAPI()
    history = [
        {"role": "system", "content": "[Context summary: 3 earlier messages.]"},
        {"role": "assistant", "content": "Morning, sir."},
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "Done, sir."},
    ]

    await _provider(claude_cfg, api).complete(
        _req(prompt="and Friday?", system_prompt="You are Jarvis.", conversation_history=history)
    )

    assert api.sent["system"][0]["text"] == "You are Jarvis."
    # The API needs a user turn first, so the window's leading assistant reply is dropped.
    assert api.sent["messages"] == [
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "Done, sir."},
        {"role": "user", "content": "[Context summary: 3 earlier messages.]\n\nand Friday?"},
    ]


@pytest.mark.asyncio
async def test_claude__forbidden_params__never_sent(claude_cfg):
    api = FakeAPI([_reply(), _reply('{"color": "blue"}'), _Stream(["ready"], _reply())])
    provider = _provider(claude_cfg, api)

    await provider.complete(_req(role="primary"))
    await provider.complete(_req(response_schema=_Tiny))
    await _collect(provider.stream(_req()))

    forbidden = {"thinking", "temperature", "top_p", "top_k", "tool_choice", "budget_tokens"}
    assert len(api.bodies) == 3
    for body in api.bodies:
        assert forbidden.isdisjoint(body)
        assert forbidden.isdisjoint(body["output_config"])


@pytest.mark.asyncio
async def test_claude__every_call__opts_into_default_refusal_fallback(claude_cfg):
    api = FakeAPI([_reply(), _reply('{"color": "blue"}'), _Stream(["ready"], _reply())])
    provider = _provider(claude_cfg, api)

    await provider.complete(_req())
    await provider.complete(_req(response_schema=_Tiny))
    await _collect(provider.stream(_req()))

    assert len(api.requests) == 3
    for request, body in zip(api.requests, api.bodies):
        assert body["fallbacks"] == "default"
        assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"


# --- answers ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__text_request__returns_text_blocks_only(claude_cfg):
    api = FakeAPI([_reply("Good evening, sir.")])

    result = await _provider(claude_cfg, api).complete(_req())

    assert result == "Good evening, sir."


@pytest.mark.asyncio
async def test_claude__schema_request__returns_schema_instance(claude_cfg):
    api = FakeAPI([_reply('{"color": "blue"}')])

    result = await _provider(claude_cfg, api).complete(_req(response_schema=_Tiny))

    assert result == _Tiny(color="blue")
    output_format = api.sent["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["properties"]["color"]["type"] == "string"


@pytest.mark.parametrize("schema", [_Tiny, None])
@pytest.mark.asyncio
async def test_claude__max_tokens_stop__raises_output_parse_error(claude_cfg, schema):
    """The cut-off reply is billed: its usage is recorded, and its JSON is never parsed."""
    truncated = _reply(
        '{"color": "bl', stop_reason="max_tokens", usage=_usage(40, 2048, cache_read=300)
    )
    api = FakeAPI([truncated])
    tally = TokenTally()

    with collecting_tokens(tally):
        with pytest.raises(LLMOutputParseError) as caught:
            await _provider(claude_cfg, api).complete(_req(response_schema=schema))

    assert _counts(tally) == (340, 2048, 300)
    assert caught.value.details == {"cause": "max_tokens"}
    assert caught.value.__cause__ is None  # stopped on stop_reason, not on a failed parse


@pytest.mark.asyncio
async def test_claude__schema_reply_fails_validation__usage_recorded_and_parse_error(claude_cfg):
    api = FakeAPI([_reply('{"colour": "blue"}', usage=_usage(30, 8))])
    tally = TokenTally()

    with collecting_tokens(tally):
        with pytest.raises(LLMOutputParseError) as caught:
            await _provider(claude_cfg, api).complete(_req(response_schema=_Tiny))

    assert _counts(tally) == (30, 8, 0)
    assert isinstance(caught.value.__cause__, PydanticValidationError)


@pytest.mark.parametrize("schema", [_Tiny, None])
@pytest.mark.asyncio
async def test_claude__refusal__usage_recorded_and_refused_without_retry(claude_cfg, schema):
    """After the server-side fallback a refusal is final: Gemini answers, Claude isn't re-asked."""
    api = FakeAPI([_reply("", stop_reason="refusal", usage=_usage(25, 0))])
    provider = _provider(claude_cfg, api)
    tally = TokenTally()

    with collecting_tokens(tally):
        with pytest.raises(LLMRefusedError) as caught:
            await RetryingProvider(provider, ExponentialBackoff(), sleep=_no_sleep).complete(
                _req(response_schema=schema)
            )

    assert _counts(tally) == (25, 0, 0)
    assert caught.value.details == {"cause": "refusal", "category": "general_harms"}
    assert len(api.requests) == 1
    assert provider.is_available()


# --- usage and the model that answered --------------------------------------------


@pytest.mark.asyncio
async def test_claude__usage__reports_cache_read_tokens(claude_cfg):
    api = FakeAPI([_reply(usage=_usage(40, 7, cache_read=900, cache_write=60))])
    provider = _provider(claude_cfg, api)
    bus, recorder = _recording_bus()

    await MeteredProvider(provider, bus, provider.model_for_role).complete(_req())

    (event,) = recorder.events
    assert event.usage.cache_read_tokens == 900
    # input_tokens is the whole prompt: uncached + written to cache + read from cache.
    assert event.usage.input_tokens == 1000
    assert event.usage.output_tokens == 7
    assert (event.usage.provider, event.usage.model) == ("claude", "claude-sonnet-5-5")


@pytest.mark.asyncio
async def test_claude__fallback_model_answers__usage_records_that_model(claude_cfg):
    api = FakeAPI([_reply(model="claude-sonnet-5"), _reply(model="claude-sonnet-5-5")])
    provider = _provider(claude_cfg, api)
    bus, recorder = _recording_bus()
    metered = MeteredProvider(provider, bus, provider.model_for_role)

    await metered.complete(_req())
    await metered.complete(_req())

    assert [e.usage.model for e in recorder.events] == ["claude-sonnet-5", "claude-sonnet-5-5"]


@pytest.mark.asyncio
async def test_claude__call_fails_before_a_reply__model_is_the_configured_one(claude_cfg):
    api = FakeAPI([_reply(model="claude-sonnet-5"), httpx2.ConnectError])
    provider = _provider(claude_cfg, api)

    await provider.complete(_req())
    assert provider.model_for_role("fast") == "claude-sonnet-5"
    with pytest.raises(LLMUnavailableError):
        await provider.complete(_req())

    assert provider.model_for_role("fast") == "claude-sonnet-5-5"


# --- streaming ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__stream__yields_content_events_for_text_deltas(claude_cfg):
    api = FakeAPI([_Stream(["Good ", "evening."], _reply("Good evening."))])

    items = await _collect(_provider(claude_cfg, api).stream(_req(system_prompt="sys")))

    assert items == [("content", "Good "), ("content", "evening.")]
    assert api.sent["stream"] is True
    assert api.sent["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude__stream_refused_before_any_text__raises_refused(claude_cfg):
    api = FakeAPI([_Stream([], _reply("", stop_reason="refusal"))])

    with pytest.raises(LLMRefusedError):
        await _collect(_provider(claude_cfg, api).stream(_req()))


@pytest.mark.asyncio
async def test_claude__stream_cut_off_before_any_text__raises_output_parse_error(claude_cfg):
    api = FakeAPI([_Stream([], _reply("", stop_reason="max_tokens"))])

    with pytest.raises(LLMOutputParseError):
        await _collect(_provider(claude_cfg, api).stream(_req()))


@pytest.mark.asyncio
async def test_claude__stream_cut_off_after_text__ends_with_what_was_sent(claude_cfg):
    api = FakeAPI([_Stream(["Good "], _reply("Good ", stop_reason="max_tokens"))])

    items = await _collect(_provider(claude_cfg, api).stream(_req()))

    assert items == [("content", "Good ")]


@pytest.mark.asyncio
async def test_claude__stream_usage__reported_once_the_stream_ends(claude_cfg):
    final = _reply("ready", model="claude-sonnet-5", usage=_usage(3, 2, cache_read=600))
    api = FakeAPI([_Stream(["ready"], final)])
    provider = _provider(claude_cfg, api)
    bus, recorder = _recording_bus()

    await _collect(MeteredProvider(provider, bus, provider.model_for_role).stream(_req()))

    (event,) = recorder.events
    assert (event.usage.input_tokens, event.usage.output_tokens) == (603, 2)
    assert event.usage.cache_read_tokens == 600
    assert event.usage.model == "claude-sonnet-5"


# --- errors -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__auth_error__disables_provider_and_raises_unavailable(claude_cfg):
    """Review Focus 4: an expired key costs one failed call, not one per turn."""
    api = FakeAPI([_api_error(401, "authentication_error")])
    provider = _provider(claude_cfg, api)

    with pytest.raises(LLMUnavailableError):
        await RetryingProvider(provider, ExponentialBackoff(), sleep=_no_sleep).complete(_req())

    assert provider.is_available() is False
    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_claude__permission_denied__disables_provider(claude_cfg):
    api = FakeAPI([_api_error(403, "permission_error")])
    provider = _provider(claude_cfg, api)

    with pytest.raises(LLMUnavailableError):
        await provider.complete(_req(response_schema=_Tiny))

    assert provider.is_available() is False


@pytest.mark.asyncio
async def test_claude__rate_limit__raises_unavailable_and_stays_enabled(claude_cfg):
    api = FakeAPI([_api_error(429, "rate_limit_error")])
    provider = _provider(claude_cfg, api)

    with pytest.raises(LLMUnavailableError):
        await provider.complete(_req())

    assert provider.is_available() is True


@pytest.mark.parametrize(("status", "error_type"), [(500, "api_error"), (529, "overloaded_error")])
@pytest.mark.asyncio
async def test_claude__server_error__raises_unavailable(claude_cfg, status, error_type):
    api = FakeAPI([_api_error(status, error_type)])

    with pytest.raises(LLMUnavailableError):
        await _provider(claude_cfg, api).complete(_req())


@pytest.mark.asyncio
async def test_claude__connection_error__raises_unavailable(claude_cfg):
    api = FakeAPI([httpx2.ConnectError])

    with pytest.raises(LLMUnavailableError):
        await _provider(claude_cfg, api).complete(_req())


@pytest.mark.asyncio
async def test_claude__sdk_timeout__raises_llm_timeout(claude_cfg):
    api = FakeAPI([httpx2.ReadTimeout])

    with pytest.raises(LLMTimeoutError):
        await _provider(claude_cfg, api).complete(_req())


@pytest.mark.asyncio
async def test_claude__bad_request__propagates_as_our_bug(claude_cfg):
    api = FakeAPI([_api_error(400, "invalid_request_error")])

    with pytest.raises(anthropic.BadRequestError):
        await _provider(claude_cfg, api).complete(_req())


@pytest.mark.asyncio
async def test_claude__other_client_error__falls_over_without_retry(claude_cfg):
    api = FakeAPI([_api_error(404, "not_found_error")])
    provider = _provider(claude_cfg, api)

    with pytest.raises(LLMError) as caught:
        await provider.complete(_req())

    assert not isinstance(caught.value, (LLMUnavailableError, LLMTimeoutError))
    assert provider.is_available() is True


@pytest.mark.asyncio
async def test_claude__stream_auth_error__disables_provider(claude_cfg):
    api = FakeAPI([_api_error(401, "authentication_error")])
    provider = _provider(claude_cfg, api)

    with pytest.raises(LLMUnavailableError):
        await _collect(provider.stream(_req()))

    assert provider.is_available() is False


# --- availability and the client ----------------------------------------------------


def test_claude__availability__needs_a_credential(claude_cfg, monkeypatch):
    provider = ClaudeProvider(claude_cfg, client=FakeAPI().client)
    assert provider.is_available()

    monkeypatch.setattr(_cfg, "ANTHROPIC_API_KEY", "")
    assert not provider.is_available()

    monkeypatch.setattr(_cfg, "ANTHROPIC_AUTH_TOKEN", "test-token")
    assert provider.is_available()


def test_claude__identity__cloud_provider_named_claude(claude_cfg):
    provider = ClaudeProvider(claude_cfg, client=FakeAPI().client)

    assert (provider.name, provider.location) == ("claude", LLMLocation.CLOUD)
    assert provider.model_for_role("fast") == "claude-sonnet-5-5"


def test_build_client__any_credential__sdk_retries_disabled(claude_cfg):
    client = build_client(claude_cfg)

    assert isinstance(client, anthropic.AsyncAnthropic)
    assert client.max_retries == 0


def test_build_client__no_api_key__uses_auth_token(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(_cfg, "ANTHROPIC_AUTH_TOKEN", "test-token")

    client = build_client(claude_cfg)

    assert (client.api_key, client.auth_token) == (None, "test-token")


def test_claude__default_client__one_shared_instance(claude_cfg, monkeypatch):
    monkeypatch.setattr(claude_module, "_shared_client", None)

    first = ClaudeProvider(claude_cfg)._client()
    second = ClaudeProvider(claude_cfg)._client()

    assert first is second


# --- factory ------------------------------------------------------------------------


def test_factory__with_anthropic_key__order_is_local_claude_gemini(claude_cfg):
    chain = LLMProviderFactory.build(claude_cfg, _pii_redactor(), NullEventBus())

    assert [p.name for p in chain.providers] == ["local", "claude", "gemini"]
    assert [p.location for p in chain.providers] == [
        LLMLocation.LOCAL,
        LLMLocation.CLOUD,
        LLMLocation.CLOUD,
    ]


def test_factory__without_anthropic_credential__claude_left_out(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "ANTHROPIC_API_KEY", "")

    chain = LLMProviderFactory.build(claude_cfg, _pii_redactor(), NullEventBus())

    assert [p.name for p in chain.providers] == ["local", "gemini"]


@pytest.mark.asyncio
async def test_factory__claude_primary__local_skipped(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "CLAUDE_PRIMARY", True)
    api = FakeAPI([_reply("from claude")])
    monkeypatch.setattr(claude_module, "_shared_client", api.client)

    async def local_transport(**kwargs):
        raise AssertionError("local must not be tried while CLAUDE_PRIMARY is on")

    monkeypatch.setattr(_llm, "hybrid_route_query", local_transport)
    bus, recorder = _recording_bus()
    chain = LLMProviderFactory.build(claude_cfg, _pii_redactor(), bus)

    result = await chain.complete(_req(prompt="madhav@example.com"))

    assert result == "from claude"
    assert api.sent["messages"] == [{"role": "user", "content": "[EMAIL]"}]
    (event,) = recorder.events
    assert (event.usage.provider, event.usage.pii_redactions) == ("claude", 1)


@pytest.mark.asyncio
async def test_factory__claude_reply_cut_off__billed_usage_and_answering_model_published(
    claude_cfg, monkeypatch
):
    """Through the real wrapping (Timeout inside Metered): the discarded reply is still billed."""
    truncated = _reply(
        '{"color": "bl', stop_reason="max_tokens", model="claude-sonnet-5", usage=_usage(50, 2048)
    )
    api = FakeAPI([truncated])
    monkeypatch.setattr(claude_module, "_shared_client", api.client)
    bus, recorder = _recording_bus()
    chain = LLMProviderFactory.build(claude_cfg, _pii_redactor(), bus)

    async def gemini_transport(**kwargs):
        assert kwargs["force_cloud"] is True
        return {"color": "green"}

    monkeypatch.setattr(_llm, "hybrid_route_query", gemini_transport)

    result = await chain.complete(_req(response_schema=_Tiny), skip_local=True)

    assert result == _Tiny(color="green")
    assert len(api.requests) == 1  # a cut-off reply is not retried
    claude_event, gemini_event = recorder.events
    assert claude_event.usage.provider == "claude"
    assert claude_event.usage.model == "claude-sonnet-5"
    assert (claude_event.usage.input_tokens, claude_event.usage.output_tokens) == (50, 2048)
    assert gemini_event.usage.provider == "gemini"
