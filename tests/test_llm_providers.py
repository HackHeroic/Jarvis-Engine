"""LLM provider strategy: adapters, decorators, the fallback chain and route_llm_call.

Every provider here is a ``FakeProvider`` or an adapter over a faked transport —
nothing reaches LiteLLM, LM Studio or a cloud API.
"""

import asyncio
import contextvars
import inspect
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any, Optional

import pytest
from pydantic import BaseModel

import app.core.config as _cfg
import app.models.brain.litellm_conf as _llm
from app.domain.entities import LLMLocation
from app.core.model_router import PreCloudLLMRedactor, get_chain
from app.domain.errors import (
    LLMError,
    LLMOutputParseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.domain.events import DomainEvent, EventBus, EventListener, LLMCallCompleted, NullEventBus
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.decorators import (
    MeteredProvider,
    RedactingProvider,
    RetryingProvider,
    ServedModels,
    TimeoutProvider,
    recording_served_models,
)
from app.infrastructure.llm.factory import LLMProviderFactory, reset_chain, set_chain
from app.infrastructure.llm.gemini import GeminiProvider
from app.infrastructure.llm.local import LocalLMStudioProvider
from app.infrastructure.llm.retry import ExponentialBackoff, NoRetry
from app.infrastructure.llm.transport import report_token_usage
from app.orchestrator.hooks import ActionHooks, HookDecision, HookResult, register_all_hooks

# --- fakes --------------------------------------------------------------------


class FakeProvider(LLMProvider):
    """A scripted provider: each call consumes the next outcome.

    ``complete`` outcomes are a value to return or an exception to raise.
    ``stream`` outcomes are a list of items, each a ``(event, token)`` pair to
    yield or an exception to raise at that point.
    """

    def __init__(
        self,
        name: str = "fake",
        location: LLMLocation = LLMLocation.CLOUD,
        outcomes: list | None = None,
        *,
        available: bool = True,
        delay_s: float = 0.0,
        tokens: tuple[int, int, int] | None = None,
    ) -> None:
        self.name = name
        self.location = location
        self.available = available
        self.calls: list[LLMRequest] = []
        self._outcomes = list(outcomes or [])
        self._delay_s = delay_s
        self._tokens = tokens

    def is_available(self) -> bool:
        return self.available

    def _next(self, default: Any) -> Any:
        return self._outcomes.pop(0) if self._outcomes else default

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        self.calls.append(req)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._tokens:
            report_token_usage(*self._tokens)
        outcome = self._next(f"{self.name}-answer")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        self.calls.append(req)
        if self._tokens:
            report_token_usage(*self._tokens)
        for item in self._next([("content", f"{self.name}-token")]):
            if isinstance(item, BaseException):
                raise item
            yield item


class HangingProvider(FakeProvider):
    """A provider that never answers (Review Focus 3)."""

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        self.calls.append(req)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        self.calls.append(req)
        await asyncio.Event().wait()
        yield ("content", "unreachable")


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


def _pii_hooks() -> ActionHooks:
    hooks = ActionHooks()
    register_all_hooks(hooks)
    return hooks


def _pii_redactor() -> PreCloudLLMRedactor:
    return PreCloudLLMRedactor(_pii_hooks())


class _Tiny(BaseModel):
    color: str


@pytest.fixture(autouse=True)
def _fresh_chain():
    """No test leaks its chain into the process-wide accessor."""
    reset_chain()
    yield
    reset_chain()


@pytest.fixture
def offline_cfg(monkeypatch):
    """Config for a machine with a local model and a Gemini key, no switches on."""
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(_cfg, "CLAUDE_PRIMARY", False)
    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(_cfg, "GEMMA_PRIMARY_MODEL", "openai/google/gemma-4-26b-a4b")
    monkeypatch.setattr(_cfg, "GEMMA_FAST_MODEL", "openai/google/gemma-4-e4b")
    monkeypatch.setattr(
        "app.core.model_router.force_cloud_var",
        contextvars.ContextVar("fc_test", default=False),
    )
    return _cfg


# --- fallback chain -----------------------------------------------------------


@pytest.mark.asyncio
async def test_chain__first_available_answers__second_not_called():
    first = FakeProvider("local", LLMLocation.LOCAL, ["from local"])
    second = FakeProvider("gemini", LLMLocation.CLOUD, ["from cloud"])

    result = await FallbackChainProvider([first, second]).complete(_req())

    assert result == "from local"
    assert second.calls == []


@pytest.mark.asyncio
async def test_chain__unavailable_provider__skipped_without_a_call():
    first = FakeProvider("local", LLMLocation.LOCAL, available=False)
    second = FakeProvider("gemini", LLMLocation.CLOUD, ["from cloud"])

    result = await FallbackChainProvider([first, second]).complete(_req())

    assert result == "from cloud"
    assert first.calls == []


@pytest.mark.asyncio
async def test_chain__timeout_on_first__second_answers():
    """Review Focus 3: a provider that never answers costs one timeout, not the turn."""
    hung = HangingProvider("local", LLMLocation.LOCAL)
    second = FakeProvider("gemini", LLMLocation.CLOUD, ["from cloud"])
    chain = FallbackChainProvider([TimeoutProvider(hung), second])

    started = time.perf_counter()
    result = await chain.complete(_req(timeout_s=0.05))

    assert result == "from cloud"
    assert len(hung.calls) == 1
    assert time.perf_counter() - started < 1.0


@pytest.mark.asyncio
async def test_chain__parse_error_on_first__falls_over():
    first = FakeProvider("local", LLMLocation.LOCAL, [LLMOutputParseError("bad json")])
    second = FakeProvider("gemini", LLMLocation.CLOUD, ["from cloud"])

    result = await FallbackChainProvider([first, second]).complete(_req())

    assert result == "from cloud"


@pytest.mark.asyncio
async def test_chain__programming_error__propagates_and_second_not_called():
    """A local code bug must not quietly ship the prompt to the cloud."""
    first = FakeProvider("local", LLMLocation.LOCAL, [TypeError("bug in our code")])
    second = FakeProvider("gemini", LLMLocation.CLOUD, ["from cloud"])

    with pytest.raises(TypeError, match="bug in our code"):
        await FallbackChainProvider([first, second]).complete(_req())

    assert second.calls == []


@pytest.mark.asyncio
async def test_chain__all_fail__raises_llm_unavailable():
    first = FakeProvider("local", LLMLocation.LOCAL, [LLMTimeoutError("slow")])
    second = FakeProvider("gemini", LLMLocation.CLOUD, [LLMUnavailableError("down")])

    with pytest.raises(LLMUnavailableError) as info:
        await FallbackChainProvider([first, second]).complete(_req())

    assert info.value.message == "No language model is reachable right now."
    assert len(first.calls) == len(second.calls) == 1


@pytest.mark.asyncio
async def test_chain__skip_local__local_not_called():
    local = FakeProvider("local", LLMLocation.LOCAL, ["from local"])
    cloud = FakeProvider("gemini", LLMLocation.CLOUD, ["from cloud"])

    result = await FallbackChainProvider([local, cloud]).complete(_req(), skip_local=True)

    assert result == "from cloud"
    assert local.calls == []


@pytest.mark.asyncio
async def test_chain__stream_fails_before_first_token__falls_over():
    local = FakeProvider("local", LLMLocation.LOCAL, [[LLMUnavailableError("no model")]])
    cloud = FakeProvider("gemini", LLMLocation.CLOUD, [[("content", "cloud "), ("content", "reply")]])

    tokens = await _collect(FallbackChainProvider([local, cloud]).stream(_req()))

    assert tokens == [("content", "cloud "), ("content", "reply")]


@pytest.mark.asyncio
async def test_chain__stream_fails_midway__raises():
    """Once a token is on the wire, a second provider would restart the reply."""
    local = FakeProvider(
        "local", LLMLocation.LOCAL, [[("content", "half a "), LLMUnavailableError("dropped")]]
    )
    cloud = FakeProvider("gemini", LLMLocation.CLOUD)
    received: list[tuple[str, str]] = []

    with pytest.raises(LLMUnavailableError, match="dropped"):
        async for item in FallbackChainProvider([local, cloud]).stream(_req()):
            received.append(item)

    assert received == [("content", "half a ")]
    assert cloud.calls == []


@pytest.mark.asyncio
async def test_chain__stream_skip_local__local_not_called():
    local = FakeProvider("local", LLMLocation.LOCAL)
    cloud = FakeProvider("gemini", LLMLocation.CLOUD, [[("content", "cloud")]])

    tokens = await _collect(
        FallbackChainProvider([local, cloud]).stream(_req(), skip_local=True)
    )

    assert tokens == [("content", "cloud")]
    assert local.calls == []


# --- retry ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrying__transient_then_success__retries_with_backoff():
    inner = FakeProvider(
        outcomes=[LLMUnavailableError("503"), LLMTimeoutError("slow"), "finally"]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    policy = ExponentialBackoff(base=0.5, cap=8.0, jitter=0.25, max_attempts=3)
    result = await RetryingProvider(inner, policy, sleep=fake_sleep).complete(_req())

    assert result == "finally"
    assert len(inner.calls) == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0] > 0
    assert all(s <= 8.0 * (1 + 0.25) for s in sleeps)


def test_exponential_backoff__late_attempt__delay_capped():
    policy = ExponentialBackoff(base=0.5, cap=2.0, jitter=0.25, max_attempts=10)

    assert all(policy.delay(n) <= 2.0 * 1.25 for n in range(1, 10))


@pytest.mark.asyncio
async def test_retrying__parse_error__not_retried():
    inner = FakeProvider(outcomes=[LLMOutputParseError("bad json"), "never"])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(LLMOutputParseError):
        await RetryingProvider(inner, ExponentialBackoff(), sleep=fake_sleep).complete(_req())

    assert len(inner.calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_retrying__keeps_failing__gives_up_after_max_attempts():
    inner = FakeProvider(outcomes=[LLMUnavailableError(f"503 #{i}") for i in range(5)])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(LLMUnavailableError, match="503 #2"):
        await RetryingProvider(
            inner, ExponentialBackoff(max_attempts=3), sleep=fake_sleep
        ).complete(_req())

    assert len(inner.calls) == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_retrying__provider_turns_unavailable__not_retried():
    """A provider that switched itself off (e.g. a revoked key) costs one call."""
    inner = FakeProvider(outcomes=[LLMUnavailableError("401"), "never"])
    original_complete = inner.complete

    async def failing_then_disabled(req: LLMRequest) -> str | BaseModel:
        try:
            return await original_complete(req)
        finally:
            inner.available = False

    inner.complete = failing_then_disabled  # type: ignore[method-assign]

    async def no_sleep(seconds: float) -> None:
        return None

    with pytest.raises(LLMUnavailableError):
        await RetryingProvider(inner, ExponentialBackoff(), sleep=no_sleep).complete(_req())

    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_retrying__no_retry_policy__single_attempt():
    inner = FakeProvider(outcomes=[LLMUnavailableError("503"), "never"])

    with pytest.raises(LLMUnavailableError):
        await RetryingProvider(inner, NoRetry()).complete(_req())

    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_retrying__stream_fails_before_first_token__retried():
    inner = FakeProvider(outcomes=[[LLMUnavailableError("503")], [("content", "ok")]])

    async def no_sleep(seconds: float) -> None:
        return None

    tokens = await _collect(
        RetryingProvider(inner, ExponentialBackoff(), sleep=no_sleep).stream(_req())
    )

    assert tokens == [("content", "ok")]
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_retrying__stream_fails_midway__not_retried():
    inner = FakeProvider(
        outcomes=[[("content", "half"), LLMUnavailableError("dropped")], [("content", "x")]]
    )

    async def no_sleep(seconds: float) -> None:
        return None

    with pytest.raises(LLMUnavailableError, match="dropped"):
        await _collect(
            RetryingProvider(inner, ExponentialBackoff(), sleep=no_sleep).stream(_req())
        )

    assert len(inner.calls) == 1


# --- timeout --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout__hung_complete__raises_llm_timeout():
    with pytest.raises(LLMTimeoutError):
        await TimeoutProvider(HangingProvider()).complete(_req(timeout_s=0.05))


@pytest.mark.asyncio
async def test_timeout__hung_stream__raises_llm_timeout():
    with pytest.raises(LLMTimeoutError):
        await _collect(TimeoutProvider(HangingProvider()).stream(_req(timeout_s=0.05)))


# --- redaction ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redacting__email_in_history_and_system_prompt__redacted():
    """The gate used to cover the prompt alone; history and system went out raw."""
    inner = FakeProvider(location=LLMLocation.CLOUD)
    history = [
        {"role": "user", "content": "my email is madhav@example.com"},
        {"role": "assistant", "content": "Noted, sir."},
    ]
    req = _req(
        prompt="call me on 555-123-4567",
        system_prompt="The user's address is madhav@example.com",
        conversation_history=history,
    )

    await RedactingProvider(inner, _pii_redactor()).complete(req)

    sent = inner.calls[0]
    wire = sent.prompt + sent.system_prompt + "".join(m["content"] for m in sent.conversation_history)
    assert "madhav@example.com" not in wire
    assert "555-123-4567" not in wire
    assert "[EMAIL]" in sent.system_prompt
    assert "[EMAIL]" in sent.conversation_history[0]["content"]
    assert "[PHONE]" in sent.prompt
    assert sent.conversation_history[1] == {"role": "assistant", "content": "Noted, sir."}
    # The caller's request and history are untouched.
    assert req.prompt == "call me on 555-123-4567"
    assert req.system_prompt == "The user's address is madhav@example.com"
    assert history[0]["content"] == "my email is madhav@example.com"


@pytest.mark.asyncio
async def test_redacting__pii_in_the_request__counts_redactions():
    inner = FakeProvider(location=LLMLocation.CLOUD)
    bus, recorder = _recording_bus()
    provider = RedactingProvider(
        MeteredProvider(inner, bus, lambda role: "gemini/gemini-2.5-flash"), _pii_redactor()
    )

    await provider.complete(
        _req(
            prompt="a@example.com and b@example.com",
            system_prompt="phone 555-123-4567",
            conversation_history=[{"role": "user", "content": "c@example.com"}],
        )
    )

    (event,) = recorder.events
    assert event.usage.pii_redactions == 4


@pytest.mark.asyncio
async def test_redacting__stream__redacted_before_the_send():
    inner = FakeProvider(location=LLMLocation.CLOUD, outcomes=[[("content", "ok")]])

    tokens = await _collect(
        RedactingProvider(inner, _pii_redactor()).stream(_req(prompt="madhav@example.com"))
    )

    assert tokens == [("content", "ok")]
    assert inner.calls[0].prompt == "[EMAIL]"


def test_redacting__local_provider__refused():
    with pytest.raises(ValueError):
        RedactingProvider(FakeProvider(location=LLMLocation.LOCAL), _pii_redactor())


# --- metering -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metered__call_answers__publishes_llm_call_completed_with_location_and_latency():
    inner = FakeProvider("local", LLMLocation.LOCAL, ["answer"], delay_s=0.02)
    bus, recorder = _recording_bus()
    provider = MeteredProvider(inner, bus, lambda role: f"model-for-{role}")

    result = await provider.complete(
        _req(task="socratic_chunker", role="primary", user_id="u1", session_id="s1")
    )

    assert result == "answer"
    (event,) = recorder.events
    assert isinstance(event, LLMCallCompleted)
    usage = event.usage
    assert usage.location == LLMLocation.LOCAL
    assert usage.provider == "local"
    assert usage.model == "model-for-primary"
    assert (usage.task, usage.role) == ("socratic_chunker", "primary")
    assert (usage.user_id, usage.session_id) == ("u1", "s1")
    assert event.user_id == "u1"
    assert 20 <= usage.latency_ms < 1000
    assert (usage.input_tokens, usage.output_tokens) == (0, 0)
    assert usage.pii_redactions == 0


@pytest.mark.asyncio
async def test_metered__transport_reports_tokens__event_carries_them():
    inner = FakeProvider(tokens=(120, 30, 100))
    bus, recorder = _recording_bus()

    await MeteredProvider(inner, bus, lambda role: "m").complete(_req())

    usage = recorder.events[0].usage
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (120, 30, 100)


@pytest.mark.asyncio
async def test_metered__stream__publishes_once_after_the_last_token():
    inner = FakeProvider(
        "gemini", LLMLocation.CLOUD, [[("reasoning", "hm"), ("content", "hi")]]
    )
    bus, recorder = _recording_bus()

    tokens = await _collect(MeteredProvider(inner, bus, lambda role: "m").stream(_req()))

    assert tokens == [("reasoning", "hm"), ("content", "hi")]
    (event,) = recorder.events
    assert event.usage.location == LLMLocation.CLOUD


@pytest.mark.asyncio
async def test_metered__failed_call__publishes_nothing():
    inner = FakeProvider(outcomes=[LLMUnavailableError("down")])
    bus, recorder = _recording_bus()

    with pytest.raises(LLMUnavailableError):
        await MeteredProvider(inner, bus, lambda role: "m").complete(_req())

    assert recorder.events == []


def test_report_token_usage__outside_a_metered_call__is_a_no_op():
    report_token_usage(10, 20)  # must not raise


# --- adapters over the LiteLLM transport --------------------------------------


@pytest.mark.asyncio
async def test_local__structured_request__passes_role_model_and_coerces_dict_into_schema(monkeypatch, offline_cfg):
    seen: dict = {}

    async def transport(**kwargs):
        seen.update(kwargs)
        return {"color": "blue"}

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    result = await LocalLMStudioProvider(offline_cfg).complete(
        _req(role="primary", response_schema=_Tiny, system_prompt="sys")
    )

    assert result == _Tiny(color="blue")
    assert seen["model_override"] == "openai/google/gemma-4-26b-a4b"
    assert seen["system_prompt"] == "sys"


@pytest.mark.asyncio
async def test_local__gemini_or_claude_primary__unavailable(monkeypatch, offline_cfg):
    local = LocalLMStudioProvider(offline_cfg)
    assert local.is_available()

    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", True)
    assert not local.is_available()

    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(_cfg, "CLAUDE_PRIMARY", True)
    assert not local.is_available()


@pytest.mark.asyncio
async def test_local__transport_timeout__raises_llm_timeout(monkeypatch, offline_cfg):
    import litellm

    async def transport(**kwargs):
        raise litellm.exceptions.Timeout(message="read timeout", model="m", llm_provider="openai")

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    with pytest.raises(LLMTimeoutError):
        await LocalLMStudioProvider(offline_cfg).complete(_req())


@pytest.mark.asyncio
async def test_local__connection_refused__raises_llm_unavailable(monkeypatch, offline_cfg):
    import litellm

    async def transport(**kwargs):
        raise litellm.exceptions.APIConnectionError(
            message="connection refused", llm_provider="openai", model="m"
        )

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    with pytest.raises(LLMUnavailableError):
        await LocalLMStudioProvider(offline_cfg).complete(_req())


@pytest.mark.asyncio
async def test_local__rate_limited__raises_llm_unavailable(monkeypatch, offline_cfg):
    import litellm

    async def transport(**kwargs):
        raise litellm.exceptions.RateLimitError(message="429", llm_provider="openai", model="m")

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    with pytest.raises(LLMUnavailableError):
        await LocalLMStudioProvider(offline_cfg).complete(_req())


@pytest.mark.asyncio
async def test_local__provider_rejects_request__llm_error_that_is_not_retried(
    monkeypatch, offline_cfg
):
    """A 4xx (e.g. LM Studio with no model loaded, or context too long) falls over
    to the next provider but is never retried against the same one."""
    import litellm

    async def transport(**kwargs):
        raise litellm.exceptions.BadRequestError(
            message="No models loaded", model="m", llm_provider="openai"
        )

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    with pytest.raises(LLMError) as info:
        await LocalLMStudioProvider(offline_cfg).complete(_req())

    assert not isinstance(info.value, (LLMTimeoutError, LLMUnavailableError))
    assert not ExponentialBackoff().should_retry(info.value, 1)


@pytest.mark.asyncio
async def test_local__schema_mismatch__raises_output_parse_error(monkeypatch, offline_cfg):
    async def transport(**kwargs):
        return {"colour": "en-GB"}

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    with pytest.raises(LLMOutputParseError):
        await LocalLMStudioProvider(offline_cfg).complete(_req(response_schema=_Tiny))


@pytest.mark.asyncio
async def test_local__empty_structured_reply__raises_output_parse_error(monkeypatch, offline_cfg):
    """An empty local reply for a schema used to fall back to the cloud; it still must."""

    class _Msg:
        content = ""

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    async def fake_acompletion(**kwargs):
        return _Resp()

    monkeypatch.setattr(_llm.litellm, "acompletion", fake_acompletion)

    with pytest.raises(LLMOutputParseError):
        await LocalLMStudioProvider(offline_cfg).complete(_req(response_schema=_Tiny))


@pytest.mark.asyncio
async def test_local__programming_error__propagates_unchanged(monkeypatch, offline_cfg):
    async def transport(**kwargs):
        raise KeyError("choices")

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    with pytest.raises(KeyError):
        await LocalLMStudioProvider(offline_cfg).complete(_req())


@pytest.mark.asyncio
async def test_local__stream__yields_transport_pairs_unchanged(monkeypatch, offline_cfg):
    seen: dict = {}

    async def transport(**kwargs):
        seen.update(kwargs)

        async def _gen():
            yield ("reasoning", "thinking")
            yield ("content", "answer")

        return _gen()

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    tokens = await _collect(LocalLMStudioProvider(offline_cfg).stream(_req()))

    assert tokens == [("reasoning", "thinking"), ("content", "answer")]
    assert seen["stream"] is True
    assert seen["model_override"] == "openai/google/gemma-4-e4b"


@pytest.mark.asyncio
async def test_gemini__no_api_key__unavailable(monkeypatch, offline_cfg):
    assert GeminiProvider(offline_cfg).is_available()

    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", None)

    assert not GeminiProvider(offline_cfg).is_available()


@pytest.mark.asyncio
async def test_gemini__complete__forces_cloud_on_the_transport(monkeypatch, offline_cfg):
    seen: dict = {}

    async def transport(**kwargs):
        seen.update(kwargs)
        return '```json\n{"color": "red"}\n```'

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)

    result = await GeminiProvider(offline_cfg).complete(
        _req(prompt="p", response_schema=_Tiny, conversation_history=[{"role": "user", "content": "x"}])
    )

    assert result == _Tiny(color="red")
    assert seen["force_cloud"] is True
    assert seen.get("model_override") is None
    assert seen["user_prompt"] == "p"
    assert seen["conversation_history"] == [{"role": "user", "content": "x"}]


# --- the context note, folded the same way for every provider ---------------------

_NOTE = "<memory>\nAbout you:\n- (you said, today) Prefers mornings\n</memory>"
_HISTORY_WITH_NOTE = [
    {"role": "assistant", "content": "Morning, sir."},  # a window opening on a reply
    {"role": "user", "content": "plan my week", "created_at": "2026-11-14"},
    {"role": "assistant", "content": "Done, sir."},
    {"role": "user", "content": ""},  # nothing to send
    {"role": "system", "content": _NOTE},
]


def test_fold_system_notes__notes_prefixed_to_prompt_and_removed_from_turns():
    from app.infrastructure.llm.messages import fold_system_notes

    history = [
        {"role": "system", "content": "[Context summary: 3 earlier messages.]"},
        *_HISTORY_WITH_NOTE,
    ]

    turns, prompt = fold_system_notes(history, "and Friday?")

    assert turns == [
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "Done, sir."},
    ]
    assert prompt == f"[Context summary: 3 earlier messages.]\n\n{_NOTE}\n\nand Friday?"
    assert fold_system_notes(None, "hi") == ([], "hi")
    assert history[0]["role"] == "system"  # the caller's list is left as it was


@pytest.mark.asyncio
async def test_local_provider__system_note__folded_before_transport(monkeypatch, offline_cfg):
    """Gemma's chat template rejects a system turn mid-conversation."""
    seen: list[dict] = []

    async def transport(**kwargs):
        seen.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield ("content", "answer")
            return _gen()
        return "answer"

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    local = LocalLMStudioProvider(offline_cfg)
    req = _req(prompt="and Friday?", system_prompt="sys", conversation_history=_HISTORY_WITH_NOTE)

    await local.complete(req)
    await _collect(local.stream(req))

    for sent in seen:
        assert sent["system_prompt"] == "sys"
        assert sent["user_prompt"] == f"{_NOTE}\n\nand Friday?"
        assert sent["conversation_history"] == [
            {"role": "user", "content": "plan my week"},
            {"role": "assistant", "content": "Done, sir."},
        ]


@pytest.mark.asyncio
async def test_gemini_provider__system_note__folded_before_transport(monkeypatch, offline_cfg):
    seen: list[dict] = []

    async def transport(**kwargs):
        seen.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield ("content", "answer")
            return _gen()
        return "answer"

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    gemini = GeminiProvider(offline_cfg)
    req = _req(prompt="and Friday?", conversation_history=_HISTORY_WITH_NOTE)

    await gemini.complete(req)
    await _collect(gemini.stream(req))

    for sent in seen:
        assert sent["user_prompt"] == f"{_NOTE}\n\nand Friday?"
        assert all(m["role"] != "system" for m in sent["conversation_history"])


# --- transport timeouts and usage ---------------------------------------------


def _fake_completion(content: str, usage: Any = None):
    class _Msg:
        pass

    msg = _Msg()
    msg.content = content

    class _Choice:
        message = msg

    class _Resp:
        choices = [_Choice()]

    resp = _Resp()
    if usage is not None:
        resp.usage = usage
    return resp


@pytest.mark.asyncio
async def test_hybrid_route__every_completion__carries_a_timeout(monkeypatch, offline_cfg):
    captured: list[dict] = []

    async def fake_acompletion(**kwargs):
        captured.append(kwargs)
        return _fake_completion("ok")

    monkeypatch.setattr(_llm.litellm, "acompletion", fake_acompletion)

    await _llm.hybrid_route_query(user_prompt="hi", system_prompt="s", model_override="m")
    await _llm.gemini_web_search("query")

    assert [c["timeout"] for c in captured] == [_cfg.LLM_TIMEOUT_SECONDS] * 2


@pytest.mark.asyncio
async def test_hybrid_route__stream__carries_the_stream_timeout(monkeypatch, offline_cfg):
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        async def _chunks():
            return
            yield

        return _chunks()

    monkeypatch.setattr(_llm.litellm, "acompletion", fake_acompletion)

    await _llm.hybrid_route_query(
        user_prompt="hi", system_prompt="s", model_override="m", stream=True
    )

    assert captured["timeout"] == _cfg.LLM_STREAM_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_hybrid_route__usage_on_response__reaches_the_meter(monkeypatch, offline_cfg):
    class _Usage:
        prompt_tokens = 42
        completion_tokens = 7

    async def fake_acompletion(**kwargs):
        return _fake_completion("ok", usage=_Usage())

    monkeypatch.setattr(_llm.litellm, "acompletion", fake_acompletion)
    bus, recorder = _recording_bus()
    local = LocalLMStudioProvider(offline_cfg)

    await MeteredProvider(local, bus, local.model_for_role).complete(_req())

    usage = recorder.events[0].usage
    assert (usage.input_tokens, usage.output_tokens) == (42, 7)
    assert usage.model == "openai/google/gemma-4-e4b"


# --- factory ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory__extra_cloud_provider__order_is_local_extra_cloud_gemini(offline_cfg):
    extra = FakeProvider("claude", LLMLocation.CLOUD)

    chain = LLMProviderFactory.build(offline_cfg, _pii_redactor(), NullEventBus(), extra_cloud=[extra])

    assert [p.name for p in chain.providers] == ["local", "claude", "gemini"]
    assert [p.location for p in chain.providers] == [
        LLMLocation.LOCAL,
        LLMLocation.CLOUD,
        LLMLocation.CLOUD,
    ]


@pytest.mark.asyncio
async def test_factory__cloud_provider__redacted_and_metered(offline_cfg):
    extra = FakeProvider("claude", LLMLocation.CLOUD, ["from claude"])
    bus, recorder = _recording_bus()
    chain = LLMProviderFactory.build(offline_cfg, _pii_redactor(), bus, extra_cloud=[extra])

    result = await chain.complete(_req(prompt="madhav@example.com"), skip_local=True)

    assert result == "from claude"
    assert extra.calls[0].prompt == "[EMAIL]"
    (event,) = recorder.events
    assert (event.usage.provider, event.usage.location) == ("claude", LLMLocation.CLOUD)
    assert event.usage.pii_redactions == 1


@pytest.mark.asyncio
async def test_factory__local_provider__not_redacted(monkeypatch, offline_cfg):
    seen: dict = {}

    async def transport(**kwargs):
        seen.update(kwargs)
        return "local answer"

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    chain = LLMProviderFactory.build(offline_cfg, _pii_redactor(), NullEventBus())

    assert await chain.complete(_req(prompt="madhav@example.com")) == "local answer"
    assert seen["user_prompt"] == "madhav@example.com"


def test_get_chain__set_and_reset__override_then_rebuild():
    custom = FallbackChainProvider([FakeProvider()])

    set_chain(custom)
    assert get_chain() is custom

    reset_chain()
    rebuilt = get_chain()
    assert rebuilt is not custom
    assert [p.name for p in rebuilt.providers] == ["local", "gemini"]


# --- route_llm_call delegates to the chain ------------------------------------


def test_route_llm_call__public_signature__unchanged():
    from app.core.model_router import route_llm_call
    from app.orchestrator.hooks import ActionHooks as _Hooks

    sig = inspect.signature(route_llm_call)
    empty = inspect.Parameter.empty

    assert inspect.iscoroutinefunction(route_llm_call)
    assert [
        (p.name, p.kind, p.default, p.annotation) for p in sig.parameters.values()
    ] == [
        ("task", inspect.Parameter.POSITIONAL_OR_KEYWORD, empty, str),
        ("prompt", inspect.Parameter.POSITIONAL_OR_KEYWORD, empty, str),
        ("system_prompt", inspect.Parameter.POSITIONAL_OR_KEYWORD, "", str),
        ("response_schema", inspect.Parameter.POSITIONAL_OR_KEYWORD, None, Optional[type[BaseModel]]),
        ("hooks", inspect.Parameter.POSITIONAL_OR_KEYWORD, None, Optional[_Hooks]),
        ("conversation_history", inspect.Parameter.POSITIONAL_OR_KEYWORD, None, Optional[list[dict]]),
        ("force_cloud", inspect.Parameter.POSITIONAL_OR_KEYWORD, None, Optional[bool]),
        ("stream", inspect.Parameter.POSITIONAL_OR_KEYWORD, False, bool),
    ]
    assert sig.return_annotation == (str | BaseModel | Any)


@pytest.mark.asyncio
async def test_route_llm_call__any_call__delegates_to_the_chain(offline_cfg):
    from app.core.model_router import route_llm_call

    local = FakeProvider("local", LLMLocation.LOCAL, [_Tiny(color="blue")])
    set_chain(FallbackChainProvider([local]))
    history = [{"role": "user", "content": "earlier"}]

    result = await route_llm_call(
        task="socratic_chunker",
        prompt="p",
        system_prompt="s",
        response_schema=_Tiny,
        conversation_history=history,
    )

    assert result == _Tiny(color="blue")
    (req,) = local.calls
    assert (req.task, req.role, req.prompt, req.system_prompt) == ("socratic_chunker", "primary", "p", "s")
    assert req.response_schema is _Tiny
    assert req.conversation_history == history
    assert req.timeout_s == _cfg.LLM_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "switch",
    ["force_cloud_kwarg", "force_cloud_var", "GEMINI_PRIMARY", "CLAUDE_PRIMARY", "cloud_role"],
)
@pytest.mark.asyncio
async def test_route_llm_call__cloud_preference__skips_local(monkeypatch, offline_cfg, switch):
    from app.core import model_router
    from app.core.model_router import route_llm_call

    local = FakeProvider("local", LLMLocation.LOCAL)
    cloud = FakeProvider("gemini", LLMLocation.CLOUD, ["from cloud"])
    set_chain(FallbackChainProvider([local, cloud]))
    kwargs: dict = {"task": "voice_of_jarvis", "prompt": "p"}
    if switch == "force_cloud_kwarg":
        kwargs["force_cloud"] = True
    elif switch == "force_cloud_var":
        model_router.force_cloud_var.set(True)
    elif switch == "cloud_role":
        kwargs["task"] = "web_search"
    else:
        monkeypatch.setattr(_cfg, switch, True)

    assert await route_llm_call(**kwargs) == "from cloud"
    assert local.calls == []


@pytest.mark.asyncio
async def test_route_llm_call__no_cloud_available_after_local_fails__runtime_error(offline_cfg):
    from app.core.model_router import route_llm_call

    local = FakeProvider("local", LLMLocation.LOCAL, [LLMUnavailableError("down")])
    cloud = FakeProvider("gemini", LLMLocation.CLOUD, available=False)
    set_chain(FallbackChainProvider([local, cloud]))

    with pytest.raises(RuntimeError, match="Local LLM failed for socratic_chunker and no GEMINI_API_KEY set"):
        await route_llm_call(task="socratic_chunker", prompt="p")


@pytest.mark.asyncio
async def test_route_llm_call__stream__same_pairs_as_the_transport(offline_cfg):
    from app.core.model_router import route_llm_call

    pairs = [("reasoning", "hm"), ("content", "Good "), ("content", "evening.")]
    local = FakeProvider("local", LLMLocation.LOCAL, [pairs])
    set_chain(FallbackChainProvider([local]))

    tokens = await route_llm_call(task="voice_of_jarvis", prompt="hi", stream=True)

    assert await _collect(tokens) == pairs
    assert local.calls[0].timeout_s == _cfg.LLM_STREAM_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_route_llm_call__stream_cannot_start__raises_at_the_await(offline_cfg):
    """Callers rely on a stream that never starts raising before the first token."""
    from app.core.model_router import route_llm_call

    local = FakeProvider("local", LLMLocation.LOCAL, [[LLMUnavailableError("down")]])
    set_chain(FallbackChainProvider([local]))

    with pytest.raises(RuntimeError, match="no GEMINI_API_KEY set"):
        await route_llm_call(task="voice_of_jarvis", prompt="hi", stream=True)


@pytest.mark.asyncio
async def test_route_llm_call__stream_with_schema__value_error(offline_cfg):
    from app.core.model_router import route_llm_call

    with pytest.raises(ValueError):
        await route_llm_call(task="voice_of_jarvis", prompt="hi", stream=True, response_schema=_Tiny)


@pytest.mark.asyncio
async def test_route_llm_call__attributed_context__request_carries_user_and_session(offline_cfg):
    from app.core.model_router import attribute_llm_calls, route_llm_call

    local = FakeProvider("local", LLMLocation.LOCAL, ["a", "b"])
    set_chain(FallbackChainProvider([local]))

    await route_llm_call(task="voice_of_jarvis", prompt="unattributed")
    end_attribution = attribute_llm_calls("u1", "sess-1")
    try:
        await route_llm_call(task="voice_of_jarvis", prompt="attributed")
    finally:
        end_attribution()

    assert [(r.user_id, r.session_id) for r in local.calls] == [(None, None), ("u1", "sess-1")]


def test_attribute_llm_calls__undo__restores_what_was_there():
    from app.core.model_router import attribute_llm_calls, current_session_var, current_user_var

    end_outer = attribute_llm_calls("u1", "sess-1")
    end_inner = attribute_llm_calls("u2", "sess-2")
    assert (current_user_var.get(), current_session_var.get()) == ("u2", "sess-2")

    end_inner()
    assert (current_user_var.get(), current_session_var.get()) == ("u1", "sess-1")
    end_outer()
    assert (current_user_var.get(), current_session_var.get()) == (None, None)


# --- fix round 1: Gemini owns no fallback of its own --------------------------


def _split_transport(monkeypatch, cloud_outcome: Any) -> dict[str, int]:
    """Patch the transport: cloud calls (``force_cloud=True``) get ``cloud_outcome``,
    local calls answer. Returns live call counts per side."""
    calls = {"local": 0, "cloud": 0}

    async def transport(**kwargs):
        if kwargs.get("force_cloud"):
            calls["cloud"] += 1
            if isinstance(cloud_outcome, BaseException):
                raise cloud_outcome
            return cloud_outcome
        calls["local"] += 1
        return "local answer"

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    return calls


@pytest.mark.asyncio
async def test_gemini__rate_limited__retried_with_backoff_then_answers(monkeypatch, offline_cfg):
    import litellm

    calls: list[dict] = []

    async def transport(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise litellm.exceptions.RateLimitError(message="429", llm_provider="gemini", model="m")
        return "from gemini"

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = await RetryingProvider(
        GeminiProvider(offline_cfg), ExponentialBackoff(), sleep=fake_sleep
    ).complete(_req())

    assert result == "from gemini"
    assert [c["force_cloud"] for c in calls] == [True, True]
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 0.5 * 1.25


@pytest.mark.asyncio
async def test_gemini__failure__one_transport_call_and_no_local_call(monkeypatch, offline_cfg):
    """gemini_primary_route retried a failed cloud call on the local model inside the adapter."""
    import litellm

    calls = _split_transport(
        monkeypatch,
        litellm.exceptions.ServiceUnavailableError(message="503", llm_provider="gemini", model="m"),
    )

    with pytest.raises(LLMUnavailableError):
        await GeminiProvider(offline_cfg).complete(_req())

    assert calls == {"local": 0, "cloud": 1}


@pytest.mark.asyncio
async def test_gemini__programming_error__propagates_and_local_not_called(monkeypatch, offline_cfg):
    calls = _split_transport(monkeypatch, TypeError("bug in our code"))
    chain = LLMProviderFactory.build(offline_cfg, _pii_redactor(), NullEventBus())

    with pytest.raises(TypeError, match="bug in our code"):
        await chain.complete(_req(), skip_local=True)

    assert calls == {"local": 0, "cloud": 1}


@pytest.mark.asyncio
async def test_chain__skip_local_and_gemini_fails__local_never_dialled(monkeypatch, offline_cfg):
    import litellm

    calls = _split_transport(
        monkeypatch,
        litellm.exceptions.BadRequestError(message="bad", model="m", llm_provider="gemini"),
    )
    chain = LLMProviderFactory.build(offline_cfg, _pii_redactor(), NullEventBus())

    with pytest.raises(LLMUnavailableError):
        await chain.complete(_req(), skip_local=True)

    assert calls == {"local": 0, "cloud": 1}


# --- fix round 1: the PII gate fails closed -----------------------------------


@pytest.mark.parametrize("decision", [HookDecision.DENY, HookDecision.ASK])
@pytest.mark.asyncio
async def test_redactor__hook_denies_or_asks__fails_closed_and_nothing_is_sent(decision):
    hooks = ActionHooks()

    async def gate(**context):
        return HookResult(decision=decision, reason="not for the cloud")

    hooks.register("PreCloudLLM", gate)
    inner = FakeProvider(location=LLMLocation.CLOUD)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = RedactingProvider(
        RetryingProvider(inner, ExponentialBackoff(), sleep=fake_sleep), PreCloudLLMRedactor(hooks)
    )

    with pytest.raises(LLMUnavailableError) as info:
        await provider.complete(_req())
    assert info.value.message == "This request cannot be sent to a cloud model."

    with pytest.raises(LLMUnavailableError):
        await _collect(provider.stream(_req()))

    assert inner.calls == []
    assert sleeps == []


@pytest.mark.asyncio
async def test_route_llm_call__hooks_deny__nothing_reaches_the_cloud(monkeypatch, offline_cfg):
    from app.core.model_router import route_llm_call

    calls = _split_transport(monkeypatch, "from gemini")
    hooks = ActionHooks()

    async def deny(**context):
        return HookResult(decision=HookDecision.DENY)

    hooks.register("PreCloudLLM", deny)

    with pytest.raises(LLMUnavailableError):
        await route_llm_call(task="voice_of_jarvis", prompt="p", hooks=hooks, force_cloud=True)

    assert calls == {"local": 0, "cloud": 0}


# --- fix round 1: metering stays out of the timed region ----------------------


class _SlowListener(EventListener):
    async def handle(self, event: DomainEvent) -> None:
        await asyncio.sleep(0.1)  # twice the calls' timeout_s


@pytest.mark.asyncio
async def test_metered__slow_listener__does_not_turn_an_answer_into_a_timeout(
    monkeypatch, offline_cfg
):
    """A slow ledger write after an answer must not cost a timeout, a retry and a second paid call."""

    async def transport(**kwargs):
        if kwargs.get("force_cloud"):
            raise AssertionError("Gemini must not be reached")
        return "local answer"

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    bus = EventBus()
    bus.subscribe(LLMCallCompleted, _SlowListener())
    extra = FakeProvider("claude", LLMLocation.CLOUD, ["answer", "a second paid call"])
    chain = LLMProviderFactory.build(offline_cfg, _pii_redactor(), bus, extra_cloud=[extra])

    assert await chain.complete(_req(timeout_s=0.05), skip_local=True) == "answer"
    assert len(extra.calls) == 1
    assert await chain.complete(_req(timeout_s=0.05)) == "local answer"


# --- fix round 1: a fall-over says why ----------------------------------------


@pytest.mark.asyncio
async def test_chain__fall_over__logs_code_details_and_truncated_cause(caplog):
    error = LLMUnavailableError("down", details={"status": 503})
    error.__cause__ = RuntimeError("upstream said: " + "x" * 300 + "TAIL")
    first = FakeProvider("local", LLMLocation.LOCAL, [error])
    second = FakeProvider("gemini", LLMLocation.CLOUD, ["ok"])

    with caplog.at_level(logging.WARNING, logger="app.infrastructure.llm.chain"):
        await FallbackChainProvider([first, second]).complete(_req(prompt="secret prompt text"))

    (record,) = [r for r in caplog.records if r.name == "app.infrastructure.llm.chain"]
    text = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "llm_unavailable" in text
    assert "'status': 503" in text
    assert "RuntimeError: upstream said: xxx" in text
    assert "TAIL" not in text
    assert "secret prompt text" not in text


# --- fix round 1: route_llm_call(hooks=...) gates that call ------------------


@pytest.mark.asyncio
async def test_route_llm_call__explicit_hooks__gate_this_call_only(offline_cfg):
    from app.core.model_router import route_llm_call

    hooks = ActionHooks()

    async def custom(prompt: str, **context):
        return HookResult(
            decision=HookDecision.MODIFY, modified_input={"prompt": "custom-redacted"}, redactions=1
        )

    hooks.register("PreCloudLLM", custom)
    cloud = FakeProvider("gemini", LLMLocation.CLOUD, ["a", [("content", "b")], "c"])
    set_chain(FallbackChainProvider([RedactingProvider(cloud, _pii_redactor())]))

    await route_llm_call(task="voice_of_jarvis", prompt="madhav@example.com", hooks=hooks)
    tokens = await route_llm_call(
        task="voice_of_jarvis", prompt="madhav@example.com", hooks=hooks, stream=True
    )
    await _collect(tokens)
    await route_llm_call(task="voice_of_jarvis", prompt="madhav@example.com")

    assert [c.prompt for c in cloud.calls] == ["custom-redacted", "custom-redacted", "[EMAIL]"]


# --- fix round 1: more coverage -----------------------------------------------


@pytest.mark.asyncio
async def test_chain__stream_programming_error__propagates_and_cloud_stream_not_opened():
    local = FakeProvider("local", LLMLocation.LOCAL, [[TypeError("bug in our code")]])
    cloud = FakeProvider("gemini", LLMLocation.CLOUD, [[("content", "cloud")]])

    with pytest.raises(TypeError, match="bug in our code"):
        await _collect(FallbackChainProvider([local, cloud]).stream(_req()))

    assert cloud.calls == []


@pytest.mark.asyncio
async def test_route_llm_call__local_fails__cloud_receives_redacted_prompt_system_and_history(
    monkeypatch, offline_cfg
):
    """Through the real factory, adapters and PreCloudLLM hook — only the transport is faked."""
    import litellm

    from app.core.model_router import route_llm_call

    sent: list[dict] = []

    async def transport(**kwargs):
        sent.append(kwargs)
        if not kwargs.get("force_cloud"):
            raise litellm.exceptions.APIConnectionError(
                message="connection refused", llm_provider="openai", model="m"
            )
        return "from gemini"

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    history = [
        {"role": "user", "content": "reach me at madhav@example.com"},
        {"role": "assistant", "content": "Noted, sir."},
    ]

    result = await route_llm_call(
        task="voice_of_jarvis",
        prompt="call 555-123-4567",
        system_prompt="The user's email is madhav@example.com",
        conversation_history=history,
    )

    assert result == "from gemini"
    local, cloud = sent
    assert local["user_prompt"] == "call 555-123-4567"  # local sees the user's own words
    assert cloud["force_cloud"] is True
    assert cloud["user_prompt"] == "call [PHONE]"
    assert cloud["system_prompt"] == "The user's email is [EMAIL]"
    assert cloud["conversation_history"] == [
        {"role": "user", "content": "reach me at [EMAIL]"},
        {"role": "assistant", "content": "Noted, sir."},
    ]
    assert history[0]["content"] == "reach me at madhav@example.com"


@pytest.mark.asyncio
async def test_chain__fall_over_on_schema_error__log_omits_the_model_reply(caplog):
    """A Pydantic error's text quotes the reply, which can echo the user's own data."""
    from pydantic import ValidationError as PydanticValidationError

    try:
        _Tiny.model_validate({"colour": "madhav@example.com"})
    except PydanticValidationError as cause:
        error = LLMOutputParseError("bad json")
        error.__cause__ = cause
    first = FakeProvider("local", LLMLocation.LOCAL, [error])
    second = FakeProvider("gemini", LLMLocation.CLOUD, ["ok"])

    with caplog.at_level(logging.WARNING, logger="app.infrastructure.llm.chain"):
        await FallbackChainProvider([first, second]).complete(_req())

    text = caplog.records[-1].getMessage()
    assert "ValidationError: color: missing" in text
    assert "madhav@example.com" not in text


# --- fix round 1: billed-but-failed calls reach the ledger --------------------


@pytest.mark.asyncio
async def test_metered__billed_call_then_error__published_and_error_raised():
    """A reply cut off at max_tokens is paid for even though it is not an answer."""
    inner = FakeProvider(
        "claude", LLMLocation.CLOUD, [LLMOutputParseError("cut off")], tokens=(500, 8000, 120)
    )
    bus, recorder = _recording_bus()

    with pytest.raises(LLMOutputParseError):
        await MeteredProvider(inner, bus, lambda role: "m").complete(_req())

    (event,) = recorder.events
    usage = event.usage
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (500, 8000, 120)
    assert usage.provider == "claude"


@pytest.mark.asyncio
async def test_metered__stream_abandoned_after_two_tokens__billed_usage_published():
    inner = FakeProvider(
        "claude",
        LLMLocation.CLOUD,
        [[("content", "a"), ("content", "b"), ("content", "c")]],
        tokens=(40, 3, 0),
    )
    bus, recorder = _recording_bus()
    received: list[tuple[str, str]] = []

    async with aclosing(MeteredProvider(inner, bus, lambda role: "m").stream(_req())) as tokens:
        async for item in tokens:
            received.append(item)
            if len(received) == 2:
                break

    assert received == [("content", "a"), ("content", "b")]
    (event,) = recorder.events
    assert (event.usage.input_tokens, event.usage.output_tokens) == (40, 3)


@pytest.mark.asyncio
async def test_metered__stream_fails_midway_after_billing__published_and_error_raised():
    inner = FakeProvider(
        "claude",
        LLMLocation.CLOUD,
        [[("content", "half"), LLMUnavailableError("dropped")]],
        tokens=(10, 2, 0),
    )
    bus, recorder = _recording_bus()

    with pytest.raises(LLMUnavailableError, match="dropped"):
        await _collect(MeteredProvider(inner, bus, lambda role: "m").stream(_req()))

    (event,) = recorder.events
    assert (event.usage.input_tokens, event.usage.output_tokens) == (10, 2)


@pytest.mark.asyncio
async def test_metered__stream_fails_without_tokens__publishes_nothing():
    inner = FakeProvider(outcomes=[[("content", "half"), LLMUnavailableError("dropped")]])
    bus, recorder = _recording_bus()

    with pytest.raises(LLMUnavailableError):
        await _collect(MeteredProvider(inner, bus, lambda role: "m").stream(_req()))

    assert recorder.events == []


# --- one policy for cancellation: a billed call that is cancelled is still metered ---


class _BilledThenHangs(FakeProvider):
    """Reports tokens (the request was billed), then never finishes."""

    def __init__(self) -> None:
        super().__init__("claude", LLMLocation.CLOUD, tokens=(70, 5, 0))
        self.billed = asyncio.Event()

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        report_token_usage(*self._tokens)
        self.billed.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        report_token_usage(*self._tokens)
        yield ("content", "first")
        self.billed.set()
        await asyncio.Event().wait()
        yield ("content", "unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["complete", "stream"])
async def test_metered__billed_call_cancelled__usage_published_and_cancellation_kept(path):
    inner = _BilledThenHangs()
    bus, recorder = _recording_bus()
    provider = MeteredProvider(inner, bus, lambda role: "m")
    call = provider.complete(_req()) if path == "complete" else _collect(provider.stream(_req()))

    task = asyncio.create_task(call)
    await asyncio.wait_for(inner.billed.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    (event,) = recorder.events
    assert (event.usage.input_tokens, event.usage.output_tokens) == (70, 5)


@pytest.mark.asyncio
async def test_metered__unbilled_call_cancelled__publishes_nothing():
    inner = HangingProvider()
    bus, recorder = _recording_bus()
    task = asyncio.create_task(MeteredProvider(inner, bus, lambda role: "m").complete(_req()))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.events == []


# --- which model answered (the chat reply credits it) ---------------------------


@pytest.mark.asyncio
async def test_metered__served_models_recorded__answers_only_visible_from_child_tasks():
    """Billed failures are not answers; calls made in tasks started from the context land
    in the same record (the chat graph runs in its own task)."""
    claude = MeteredProvider(
        FakeProvider("claude", LLMLocation.CLOUD, ["hi"]), EventBus(), lambda role: "claude-sonnet-5-5"
    )
    billed_failure = MeteredProvider(
        FakeProvider("gemini", LLMLocation.CLOUD, [LLMOutputParseError("cut off")], tokens=(1, 2, 0)),
        EventBus(),
        lambda role: "gemini-2.5-flash",
    )
    served = ServedModels()
    undo = recording_served_models(served)
    try:
        await asyncio.create_task(claude.complete(_req(task="voice_of_jarvis")))
        with pytest.raises(LLMOutputParseError):
            await billed_failure.complete(_req(task="voice_of_jarvis"))
    finally:
        undo()

    assert served.answers == [("voice_of_jarvis", "claude", "claude-sonnet-5-5")]
    assert served.last_model_for(("voice_of_jarvis",)) == "claude-sonnet-5-5"
    assert served.last_model_for(("memory_extraction",)) is None
