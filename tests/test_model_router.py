import pytest
from pydantic import BaseModel

from app.core.model_router import ModelRole, MODEL_ROUTING
from app.domain.entities import LLMLocation
from app.domain.ports import LLMProvider


def test_routing_table_completeness():
    expected_tasks = [
        "socratic_chunker", "habit_translation", "document_understanding",
        "research_summarization", "intent_classification", "brain_dump_extraction",
        "memory_extraction", "voice_of_jarvis", "calendar_parsing",
        "goal_validation", "web_search", "real_time_research",
    ]
    for task in expected_tasks:
        assert task in MODEL_ROUTING, f"Missing routing for {task}"


def test_primary_tasks_use_26b():
    assert MODEL_ROUTING["socratic_chunker"] == ModelRole.PRIMARY
    assert MODEL_ROUTING["habit_translation"] == ModelRole.PRIMARY


def test_fast_tasks_use_e4b():
    assert MODEL_ROUTING["intent_classification"] == ModelRole.FAST
    assert MODEL_ROUTING["voice_of_jarvis"] == ModelRole.FAST


def test_cloud_tasks_use_gemini():
    assert MODEL_ROUTING["web_search"] == ModelRole.CLOUD
    assert MODEL_ROUTING["real_time_research"] == ModelRole.CLOUD


# --- streaming keeps the fallback chain and the PII gate -------------------


@pytest.mark.asyncio
async def test_route_llm_call__stream__still_runs_the_pii_gate_before_cloud(monkeypatch):
    """A streamed CHAT reply must not be the one path that skips L8.

    The streaming synthesis added in Task 11 bypassed route_llm_call at first,
    which quietly took the PreCloudLLM hook out of the CHAT path.
    """
    import app.core.config as _cfg
    import app.models.brain.litellm_conf as _llm
    from app.core.model_router import route_llm_call
    from app.orchestrator.hooks import ActionHooks, register_all_hooks

    seen: dict = {}

    async def fake_hybrid(**kwargs):
        seen.update(kwargs)

        async def _gen():
            yield ("content", "ok")
        return _gen()

    monkeypatch.setattr(_llm, "hybrid_route_query", fake_hybrid)
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", True)
    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")

    hooks = ActionHooks()
    register_all_hooks(hooks)

    gen = await route_llm_call(
        task="voice_of_jarvis",
        prompt="mail me at madhav@example.com",
        system_prompt="sys",
        stream=True,
        hooks=hooks,
    )
    chunks = [tok async for _evt, tok in gen]

    assert chunks == ["ok"]
    assert "madhav@example.com" not in seen["user_prompt"]
    assert "[EMAIL]" in seen["user_prompt"]


@pytest.mark.asyncio
async def test_route_llm_call__stream__local_failure_falls_back_to_cloud(monkeypatch):
    """Streaming lost route_llm_call's retry when it called litellm directly.

    The local failure is the error LiteLLM raises when LM Studio rejects the call.
    A bare RuntimeError would now propagate: only model failures fall over to the
    cloud, a code bug does not (tests/test_llm_providers.py).
    """
    import litellm

    import app.core.config as _cfg
    import app.models.brain.litellm_conf as _llm
    from app.core.model_router import route_llm_call

    attempts: list[bool] = []

    async def flaky_hybrid(**kwargs):
        attempts.append(bool(kwargs.get("force_cloud")))
        if not kwargs.get("force_cloud"):
            raise litellm.exceptions.BadRequestError(
                message="LM Studio: no models loaded", model="gemma", llm_provider="openai"
            )

        async def _gen():
            yield ("content", "cloud reply")
        return _gen()

    monkeypatch.setattr(_llm, "hybrid_route_query", flaky_hybrid)
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.core.model_router.force_cloud_var", __import__("contextvars").ContextVar("fc", default=False))

    gen = await route_llm_call(
        task="voice_of_jarvis", prompt="hi", system_prompt="sys", stream=True,
    )
    assert [tok async for _evt, tok in gen] == ["cloud reply"]
    assert attempts == [False, True]  # local first, then cloud


# --- structured output is normalised to the schema, whatever the transport ---
# `litellm_conf.hybrid_route_query` returns `parsed.model_dump()` — a plain
# dict — for EVERY structured call, local and cloud alike. The router used to
# normalise only `str`, so the dict fell through unvalidated and every caller
# that branched on `isinstance(result, Schema)` silently took its failure path.
# That is F1: planning turns produced a schedule containing none of the user's
# goal while reporting success.


class _Tiny(BaseModel):
    color: str


def _offline_router(monkeypatch, transport, *, gemini_primary: bool):
    """Point route_llm_call at an in-memory transport, no network, no .env."""
    import contextvars

    import app.core.config as _cfg
    import app.models.brain.litellm_conf as _llm

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", gemini_primary)
    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.core.model_router.force_cloud_var",
        contextvars.ContextVar("fc", default=False),
    )


@pytest.mark.asyncio
async def test_route_llm_call__local_transport_returns_dict__validated_into_schema(monkeypatch):
    """The local path must hand callers a model, not the transport's raw dict."""
    from app.core.model_router import route_llm_call

    async def dict_transport(**kwargs):
        return {"color": "blue"}

    _offline_router(monkeypatch, dict_transport, gemini_primary=False)

    result = await route_llm_call(
        task="socratic_chunker", prompt="p", system_prompt="s", response_schema=_Tiny,
    )

    assert isinstance(result, _Tiny), f"got {type(result).__name__}: {result!r}"
    assert result.color == "blue"


@pytest.mark.asyncio
async def test_route_llm_call__cloud_transport_returns_dict__validated_into_schema(monkeypatch):
    """Same contract on the Gemini path — F1 was reproduced live against cloud."""
    from app.core.model_router import route_llm_call

    async def dict_transport(**kwargs):
        return {"color": "blue"}

    _offline_router(monkeypatch, dict_transport, gemini_primary=True)

    result = await route_llm_call(
        task="socratic_chunker", prompt="p", system_prompt="s", response_schema=_Tiny,
    )

    assert isinstance(result, _Tiny), f"got {type(result).__name__}: {result!r}"
    assert result.color == "blue"


@pytest.mark.asyncio
async def test_route_llm_call__transport_returns_schema_instance__passes_through(monkeypatch):
    """A transport that already validated must not be re-parsed or dropped."""
    from app.core.model_router import route_llm_call

    instance = _Tiny(color="green")

    async def model_transport(**kwargs):
        return instance

    _offline_router(monkeypatch, model_transport, gemini_primary=False)

    result = await route_llm_call(
        task="socratic_chunker", prompt="p", system_prompt="s", response_schema=_Tiny,
    )

    assert result is instance


@pytest.mark.asyncio
async def test_route_llm_call__transport_returns_fenced_json__still_validated(monkeypatch):
    """The pre-existing str path keeps working — fences and all."""
    from app.core.model_router import route_llm_call

    async def str_transport(**kwargs):
        return '```json\n{"color": "red"}\n```'

    _offline_router(monkeypatch, str_transport, gemini_primary=False)

    result = await route_llm_call(
        task="socratic_chunker", prompt="p", system_prompt="s", response_schema=_Tiny,
    )

    assert isinstance(result, _Tiny)
    assert result.color == "red"


@pytest.mark.asyncio
async def test_route_llm_call__local_dict_fails_schema__falls_back_to_cloud(monkeypatch):
    """A malformed local dict is a local failure, not a value to return.

    Before the fix an unvalidated dict short-circuited the cloud retry: the
    caller got garbage instead of a second, better attempt.
    """
    from app.core.model_router import route_llm_call

    attempts: list[bool] = []

    async def flaky_transport(**kwargs):
        cloud = bool(kwargs.get("force_cloud"))
        attempts.append(cloud)
        return {"color": "blue"} if cloud else {"colour": "en-GB"}

    _offline_router(monkeypatch, flaky_transport, gemini_primary=False)

    result = await route_llm_call(
        task="socratic_chunker", prompt="p", system_prompt="s", response_schema=_Tiny,
    )

    assert isinstance(result, _Tiny)
    assert result.color == "blue"
    assert attempts == [False, True]  # local first, then cloud


# --- one PRIMARY-role call at a time ------------------------------------------
# The heavy local model fits in memory once: a background brief's habit translation
# and a user's planning turn must never run two PRIMARY calls at once.


class _HeldModel(LLMProvider):
    """A cloud model that counts the calls in flight, and holds each until released."""

    name = "held"
    location = LLMLocation.CLOUD

    def __init__(self) -> None:
        self.in_flight: list[str] = []
        self.overlap = 0
        self.started: list[str] = []
        self.release = None  # an asyncio.Event; set by the test before calls begin

    def is_available(self) -> bool:
        return True

    async def complete(self, req):
        self.started.append(req.task)
        self.in_flight.append(req.role)
        self.overlap = max(self.overlap, self.in_flight.count("primary"))
        try:
            await self.release.wait()
        finally:
            self.in_flight.remove(req.role)
        return "ok"

    async def stream(self, req):
        yield ("content", "ok")


def _install(model) -> None:
    from app.infrastructure.llm.chain import FallbackChainProvider
    from app.infrastructure.llm.factory import set_chain

    set_chain(FallbackChainProvider([model]))


async def _until(predicate, timeout: float = 2.0) -> None:
    import asyncio

    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
async def test_route_llm_call__two_primary_calls__never_overlap():
    import asyncio

    from app.core.model_router import route_llm_call

    model = _HeldModel()
    model.release = asyncio.Event()
    _install(model)

    first = asyncio.create_task(route_llm_call(task="socratic_chunker", prompt="plan"))
    second = asyncio.create_task(route_llm_call(task="habit_translation", prompt="habits"))
    await _until(lambda: model.started)
    for _ in range(20):  # the second call has every chance to start, and must not
        await asyncio.sleep(0)
    assert model.started == ["socratic_chunker"]

    model.release.set()
    assert await asyncio.gather(first, second) == ["ok", "ok"]
    assert model.started == ["socratic_chunker", "habit_translation"]
    assert model.overlap == 1


@pytest.mark.asyncio
async def test_route_llm_call__fast_call__not_blocked_by_primary():
    import asyncio

    from app.core.model_router import route_llm_call

    model = _HeldModel()
    model.release = asyncio.Event()
    _install(model)

    heavy = asyncio.create_task(route_llm_call(task="socratic_chunker", prompt="plan"))
    await _until(lambda: model.started == ["socratic_chunker"])
    quick = asyncio.create_task(route_llm_call(task="voice_of_jarvis", prompt="hello"))

    await _until(lambda: model.started == ["socratic_chunker", "voice_of_jarvis"])
    assert not heavy.done()  # the FAST call started while the PRIMARY one held the lock
    model.release.set()
    assert await asyncio.gather(heavy, quick) == ["ok", "ok"]


@pytest.mark.asyncio
async def test_route_llm_call__primary_call_cancelled__lock_released_for_the_next():
    """A PRIMARY call cancelled mid-flight (a step timeout, a stopped turn) frees the lock."""
    import asyncio

    from app.core.model_router import route_llm_call

    model = _HeldModel()
    model.release = asyncio.Event()
    _install(model)

    stuck = asyncio.create_task(route_llm_call(task="socratic_chunker", prompt="plan"))
    await _until(lambda: model.started == ["socratic_chunker"])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(route_llm_call(task="habit_translation", prompt="h"), 0.05)
    stuck.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stuck

    model.release.set()
    assert await asyncio.wait_for(route_llm_call(task="habit_translation", prompt="h"), 2) == "ok"


@pytest.mark.asyncio
async def test_route_llm_call__nested_primary_call__runs_under_the_outer_hold():
    """A PRIMARY call made from inside one (same task, or a task it spawned) does not
    wait for the lock its own caller holds: no deadlock."""
    import asyncio

    from app.core.model_router import route_llm_call

    class _NestingModel(_HeldModel):
        async def complete(self, req):
            self.started.append(req.task)
            if req.task == "socratic_chunker":
                inner = await route_llm_call(task="habit_translation", prompt="inner")
                spawned = await asyncio.create_task(
                    route_llm_call(task="calendar_parsing", prompt="spawned")
                )
                return f"outer({inner},{spawned})"
            return "ok"

    model = _NestingModel()
    _install(model)

    result = await asyncio.wait_for(route_llm_call(task="socratic_chunker", prompt="p"), 2)

    assert result == "outer(ok,ok)"
    assert model.started == ["socratic_chunker", "habit_translation", "calendar_parsing"]
