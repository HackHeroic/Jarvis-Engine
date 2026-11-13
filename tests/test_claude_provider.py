"""ClaudeProvider: the Anthropic SDK behind the ``LLMProvider`` port.

Every test injects a fake client (``client=``) that records the keyword arguments
of each SDK call and hands back a scripted response — nothing reaches Anthropic.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

import app.core.config as _cfg
import app.infrastructure.llm.claude as claude_module
import app.models.brain.litellm_conf as _llm
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
from app.orchestrator.hooks import ActionHooks, register_all_hooks

# --- fakes --------------------------------------------------------------------


def _usage(input_tokens=10, output_tokens=5, cache_read=0, cache_creation=0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


def _message(
    text: str = "ready",
    stop_reason: str = "end_turn",
    parsed: Any = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    """A reply shaped like the SDK's ``ParsedBetaMessage``; an empty thinking block leads."""
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=(
            SimpleNamespace(category="general_harms") if stop_reason == "refusal" else None
        ),
        parsed_output=parsed,
        usage=usage or _usage(),
    )


class _FakeStream:
    def __init__(self, deltas: list[str], final: SimpleNamespace) -> None:
        self._deltas = deltas
        self._final = final

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for delta in self._deltas:
                yield delta

        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self._final


class FakeClient:
    """Stands in for ``AsyncAnthropic``: ``client.beta.messages.{create,parse,stream}``.

    Each call consumes the next outcome — a reply to return or an exception to
    raise. A ``stream`` outcome is ``(deltas, final_message)``.
    """

    def __init__(self, outcomes: list | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._outcomes = list(outcomes or [])
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(create=self._create, parse=self._parse, stream=self._stream)
        )

    def _next(self, default: Any) -> Any:
        outcome = self._outcomes.pop(0) if self._outcomes else default
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(("create", kwargs))
        return self._next(_message())

    async def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(("parse", kwargs))
        return self._next(_message())

    def _stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(("stream", kwargs))
        deltas, final = self._next((["rea", "dy"], _message("ready")))
        return _FakeStream(deltas, final)

    @property
    def kwargs(self) -> dict:
        """Keyword arguments of the only call made."""
        (call,) = self.calls
        return call[1]


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("rejected", response=httpx2.Response(status, request=request), body=None)


def _connection_request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class _Tiny(BaseModel):
    color: str


class _Recorder(EventListener):
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.events.append(event)


def _req(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {"task": "voice_of_jarvis", "role": "fast", "prompt": "hello"}
    fields.update(overrides)
    return LLMRequest(**fields)


async def _collect(tokens: AsyncIterator[tuple[str, str]]) -> list[tuple[str, str]]:
    return [item async for item in tokens]


def _pii_hooks() -> ActionHooks:
    hooks = ActionHooks()
    register_all_hooks(hooks)
    return hooks


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


def _provider(cfg, client: FakeClient) -> ClaudeProvider:
    return ClaudeProvider(cfg, client=client)


# --- request shape ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__fast_role__low_effort_and_small_max_tokens(claude_cfg):
    client = FakeClient([_message(parsed=_Tiny(color="blue")), _message()])
    provider = _provider(claude_cfg, client)

    await provider.complete(_req(response_schema=_Tiny))
    await provider.complete(_req())

    (_, structured), (_, text) = client.calls
    assert structured["output_config"] == {"effort": "low"}
    assert structured["max_tokens"] == 1024
    assert text["output_config"] == {"effort": "low"}
    assert text["max_tokens"] == 4096


@pytest.mark.parametrize("role", ["primary", "cloud"])
@pytest.mark.asyncio
async def test_claude__primary_and_cloud_roles__medium_effort_and_8000_max_tokens(claude_cfg, role):
    client = FakeClient()

    await _provider(claude_cfg, client).complete(_req(role=role, task="socratic_chunker"))

    assert client.kwargs["output_config"] == {"effort": "medium"}
    assert client.kwargs["max_tokens"] == 8000


@pytest.mark.asyncio
async def test_claude__model_and_timeout__from_config_and_request(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "CLAUDE_MODEL", "claude-from-config")
    client = FakeClient()

    await _provider(claude_cfg, client).complete(_req(timeout_s=12.5))

    assert client.kwargs["model"] == "claude-from-config"
    assert client.kwargs["timeout"] == 12.5


@pytest.mark.asyncio
async def test_claude__system_prompt__sent_as_cached_block(claude_cfg):
    client = FakeClient()

    await _provider(claude_cfg, client).complete(_req(system_prompt="You are Jarvis."))

    assert client.kwargs["system"] == [
        {"type": "text", "text": "You are Jarvis.", "cache_control": {"type": "ephemeral"}}
    ]


@pytest.mark.asyncio
async def test_claude__empty_system_prompt__no_system_sent(claude_cfg):
    client = FakeClient()

    await _provider(claude_cfg, client).complete(_req(system_prompt=""))

    assert "system" not in client.kwargs


@pytest.mark.asyncio
async def test_claude__history__mapped_through_and_prompt_is_last_user_turn(claude_cfg):
    client = FakeClient()
    history = [
        {"role": "user", "content": "plan my week", "created_at": "2026-11-14"},
        {"role": "assistant", "content": "Done, sir."},
    ]

    await _provider(claude_cfg, client).complete(
        _req(prompt="and Friday?", conversation_history=history)
    )

    assert client.kwargs["messages"] == [
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "Done, sir."},
        {"role": "user", "content": "and Friday?"},
    ]


@pytest.mark.asyncio
async def test_claude__history_system_entry__prepended_to_user_message_not_system_block(claude_cfg):
    """A history system entry may change per turn; in the system block it would bust the cache."""
    client = FakeClient()
    history = [
        {"role": "system", "content": "[Context summary: 3 earlier messages.]"},
        {"role": "assistant", "content": "Morning, sir."},
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "Done, sir."},
    ]

    await _provider(claude_cfg, client).complete(
        _req(prompt="and Friday?", system_prompt="You are Jarvis.", conversation_history=history)
    )

    assert client.kwargs["system"][0]["text"] == "You are Jarvis."
    # The API needs a user turn first, so the window's leading assistant reply is dropped.
    assert client.kwargs["messages"] == [
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "Done, sir."},
        {"role": "user", "content": "[Context summary: 3 earlier messages.]\n\nand Friday?"},
    ]


@pytest.mark.asyncio
async def test_claude__forbidden_params__never_sent(claude_cfg):
    client = FakeClient([_message(), _message(parsed=_Tiny(color="blue"))])
    provider = _provider(claude_cfg, client)

    await provider.complete(_req(role="primary"))
    await provider.complete(_req(response_schema=_Tiny))
    await _collect(provider.stream(_req()))

    forbidden = {"thinking", "temperature", "top_p", "top_k", "tool_choice", "budget_tokens"}
    for method, kwargs in client.calls:
        assert forbidden.isdisjoint(kwargs), method
        assert forbidden.isdisjoint(kwargs["output_config"]), method


@pytest.mark.asyncio
async def test_claude__every_call__opts_into_default_refusal_fallback(claude_cfg):
    client = FakeClient([_message(), _message(parsed=_Tiny(color="blue"))])
    provider = _provider(claude_cfg, client)

    await provider.complete(_req())
    await provider.complete(_req(response_schema=_Tiny))
    await _collect(provider.stream(_req()))

    for method, kwargs in client.calls:
        assert kwargs["fallbacks"] == "default", method
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"], method


@pytest.mark.asyncio
async def test_claude__auth_token_credential__adds_oauth_beta(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(_cfg, "ANTHROPIC_AUTH_TOKEN", "test-token")
    client = FakeClient()

    await _provider(claude_cfg, client).complete(_req())

    assert client.kwargs["betas"] == ["server-side-fallback-2026-07-01", "oauth-2025-04-20"]


# --- answers ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__text_request__returns_text_blocks_only(claude_cfg):
    client = FakeClient([_message("Good evening, sir.")])

    result = await _provider(claude_cfg, client).complete(_req())

    assert result == "Good evening, sir."
    assert client.calls[0][0] == "create"


@pytest.mark.asyncio
async def test_claude__schema_request__returns_schema_instance(claude_cfg):
    client = FakeClient([_message('{"color": "blue"}', parsed=_Tiny(color="blue"))])

    result = await _provider(claude_cfg, client).complete(_req(response_schema=_Tiny))

    assert result == _Tiny(color="blue")
    method, kwargs = client.calls[0]
    assert method == "parse"
    assert kwargs["output_format"] is _Tiny


@pytest.mark.asyncio
async def test_claude__schema_reply_fails_validation__raises_output_parse_error(claude_cfg):
    from pydantic import ValidationError

    try:
        _Tiny.model_validate_json('{"color": ')
    except ValidationError as exc:
        invalid = exc
    client = FakeClient([invalid])

    with pytest.raises(LLMOutputParseError):
        await _provider(claude_cfg, client).complete(_req(response_schema=_Tiny))


@pytest.mark.parametrize("schema", [_Tiny, None])
@pytest.mark.asyncio
async def test_claude__max_tokens_stop__raises_output_parse_error(claude_cfg, schema):
    client = FakeClient([_message('{"color": "bl', stop_reason="max_tokens", parsed=None)])

    with pytest.raises(LLMOutputParseError):
        await _provider(claude_cfg, client).complete(_req(response_schema=schema))


@pytest.mark.asyncio
async def test_claude__refusal__raises_refused_and_is_not_retried(claude_cfg):
    """After the server-side fallback a refusal is final: Gemini answers, Claude isn't re-asked."""
    client = FakeClient([_message("", stop_reason="refusal")])
    provider = _provider(claude_cfg, client)
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.raises(LLMRefusedError) as caught:
        await RetryingProvider(provider, ExponentialBackoff(), sleep=no_sleep).complete(_req())

    assert isinstance(caught.value, LLMError)
    assert len(client.calls) == 1
    assert slept == []
    assert provider.is_available()


# --- streaming ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__stream__yields_content_events_for_text_deltas(claude_cfg):
    client = FakeClient([(["Good ", "evening."], _message("Good evening."))])

    items = await _collect(_provider(claude_cfg, client).stream(_req(system_prompt="sys")))

    assert items == [("content", "Good "), ("content", "evening.")]
    method, kwargs = client.calls[0]
    assert method == "stream"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude__stream_refused_before_any_text__raises_refused(claude_cfg):
    client = FakeClient([([], _message("", stop_reason="refusal"))])

    with pytest.raises(LLMRefusedError):
        await _collect(_provider(claude_cfg, client).stream(_req()))


@pytest.mark.asyncio
async def test_claude__stream_cut_off_before_any_text__raises_output_parse_error(claude_cfg):
    client = FakeClient([([], _message("", stop_reason="max_tokens"))])

    with pytest.raises(LLMOutputParseError):
        await _collect(_provider(claude_cfg, client).stream(_req()))


@pytest.mark.asyncio
async def test_claude__stream_cut_off_after_text__ends_with_what_was_sent(claude_cfg):
    client = FakeClient([(["Good "], _message("Good ", stop_reason="max_tokens"))])

    items = await _collect(_provider(claude_cfg, client).stream(_req()))

    assert items == [("content", "Good ")]


# --- errors -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude__auth_error__disables_provider_and_raises_unavailable(claude_cfg):
    """Review Focus 4: an expired key costs one failed call, not one per turn."""
    client = FakeClient([_status_error(anthropic.AuthenticationError, 401)])
    provider = _provider(claude_cfg, client)
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.raises(LLMUnavailableError):
        await RetryingProvider(provider, ExponentialBackoff(), sleep=no_sleep).complete(_req())

    assert provider.is_available() is False
    assert len(client.calls) == 1
    assert slept == []


@pytest.mark.asyncio
async def test_claude__permission_denied__disables_provider(claude_cfg):
    client = FakeClient([_status_error(anthropic.PermissionDeniedError, 403)])
    provider = _provider(claude_cfg, client)

    with pytest.raises(LLMUnavailableError):
        await provider.complete(_req())

    assert provider.is_available() is False


@pytest.mark.asyncio
async def test_claude__rate_limit__raises_unavailable_and_stays_enabled(claude_cfg):
    client = FakeClient([_status_error(anthropic.RateLimitError, 429)])
    provider = _provider(claude_cfg, client)

    with pytest.raises(LLMUnavailableError):
        await provider.complete(_req())

    assert provider.is_available() is True


@pytest.mark.parametrize("status", [500, 529])
@pytest.mark.asyncio
async def test_claude__server_error__raises_unavailable(claude_cfg, status):
    client = FakeClient([_status_error(anthropic.InternalServerError, status)])

    with pytest.raises(LLMUnavailableError):
        await _provider(claude_cfg, client).complete(_req())


@pytest.mark.asyncio
async def test_claude__connection_error__raises_unavailable(claude_cfg):
    client = FakeClient([anthropic.APIConnectionError(request=_connection_request())])

    with pytest.raises(LLMUnavailableError):
        await _provider(claude_cfg, client).complete(_req())


@pytest.mark.asyncio
async def test_claude__sdk_timeout__raises_llm_timeout(claude_cfg):
    client = FakeClient([anthropic.APITimeoutError(request=_connection_request())])

    with pytest.raises(LLMTimeoutError):
        await _provider(claude_cfg, client).complete(_req())


@pytest.mark.asyncio
async def test_claude__bad_request__propagates_as_our_bug(claude_cfg):
    client = FakeClient([_status_error(anthropic.BadRequestError, 400)])

    with pytest.raises(anthropic.BadRequestError):
        await _provider(claude_cfg, client).complete(_req())


@pytest.mark.asyncio
async def test_claude__other_client_error__falls_over_without_retry(claude_cfg):
    client = FakeClient([_status_error(anthropic.NotFoundError, 404)])
    provider = _provider(claude_cfg, client)

    with pytest.raises(LLMError) as caught:
        await provider.complete(_req())

    assert not isinstance(caught.value, (LLMUnavailableError, LLMTimeoutError))
    assert provider.is_available() is True


@pytest.mark.asyncio
async def test_claude__stream_auth_error__disables_provider(claude_cfg):
    client = FakeClient([_status_error(anthropic.AuthenticationError, 401)])
    provider = _provider(claude_cfg, client)

    with pytest.raises(LLMUnavailableError):
        await _collect(provider.stream(_req()))

    assert provider.is_available() is False


# --- availability, usage, client ----------------------------------------------------


def test_claude__availability__needs_a_credential(claude_cfg, monkeypatch):
    provider = ClaudeProvider(claude_cfg, client=FakeClient())
    assert provider.is_available()

    monkeypatch.setattr(_cfg, "ANTHROPIC_API_KEY", "")
    assert not provider.is_available()

    monkeypatch.setattr(_cfg, "ANTHROPIC_AUTH_TOKEN", "test-token")
    assert provider.is_available()


def test_claude__identity__cloud_provider_named_claude(claude_cfg):
    provider = ClaudeProvider(claude_cfg, client=FakeClient())

    assert (provider.name, provider.location) == ("claude", LLMLocation.CLOUD)
    assert provider.model_for_role("fast") == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_claude__usage__reports_cache_read_tokens(claude_cfg):
    usage = _usage(input_tokens=40, output_tokens=7, cache_read=900, cache_creation=60)
    client = FakeClient([_message(usage=usage)])
    provider = _provider(claude_cfg, client)
    bus = EventBus()
    recorder = _Recorder()
    bus.subscribe(LLMCallCompleted, recorder)

    await MeteredProvider(provider, bus, provider.model_for_role).complete(_req())

    (event,) = recorder.events
    assert event.usage.cache_read_tokens == 900
    # input_tokens is the whole prompt: uncached + written to cache + read from cache.
    assert event.usage.input_tokens == 1000
    assert event.usage.output_tokens == 7
    assert (event.usage.provider, event.usage.model) == ("claude", "claude-sonnet-5-5")


@pytest.mark.asyncio
async def test_claude__stream_usage__reported_once_the_stream_ends(claude_cfg):
    final = _message("ready", usage=_usage(input_tokens=3, output_tokens=2, cache_read=600))
    client = FakeClient([(["ready"], final)])
    provider = _provider(claude_cfg, client)
    bus = EventBus()
    recorder = _Recorder()
    bus.subscribe(LLMCallCompleted, recorder)

    await _collect(MeteredProvider(provider, bus, provider.model_for_role).stream(_req()))

    (event,) = recorder.events
    assert (event.usage.input_tokens, event.usage.output_tokens) == (603, 2)
    assert event.usage.cache_read_tokens == 600


def test_build_client__sdk_never_retries(claude_cfg):
    client = build_client(claude_cfg)

    assert isinstance(client, anthropic.AsyncAnthropic)
    assert client.max_retries == 0


def test_build_client__auth_token_when_no_api_key(claude_cfg, monkeypatch):
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
    chain = LLMProviderFactory.build(claude_cfg, _pii_hooks(), NullEventBus())

    assert [p.name for p in chain.providers] == ["local", "claude", "gemini"]
    assert [p.location for p in chain.providers] == [
        LLMLocation.LOCAL,
        LLMLocation.CLOUD,
        LLMLocation.CLOUD,
    ]


def test_factory__without_anthropic_credential__claude_left_out(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "ANTHROPIC_API_KEY", "")

    chain = LLMProviderFactory.build(claude_cfg, _pii_hooks(), NullEventBus())

    assert [p.name for p in chain.providers] == ["local", "gemini"]


@pytest.mark.asyncio
async def test_factory__claude_primary__local_skipped(claude_cfg, monkeypatch):
    monkeypatch.setattr(_cfg, "CLAUDE_PRIMARY", True)
    client = FakeClient([_message("from claude")])
    monkeypatch.setattr(claude_module, "_shared_client", client)

    async def local_transport(**kwargs):
        raise AssertionError("local must not be tried while CLAUDE_PRIMARY is on")

    monkeypatch.setattr(_llm, "hybrid_route_query", local_transport)
    bus = EventBus()
    recorder = _Recorder()
    bus.subscribe(LLMCallCompleted, recorder)
    chain = LLMProviderFactory.build(claude_cfg, _pii_hooks(), bus)

    result = await chain.complete(_req(prompt="madhav@example.com"))

    assert result == "from claude"
    assert client.kwargs["messages"] == [{"role": "user", "content": "[EMAIL]"}]
    (event,) = recorder.events
    assert (event.usage.provider, event.usage.pii_redactions) == ("claude", 1)
