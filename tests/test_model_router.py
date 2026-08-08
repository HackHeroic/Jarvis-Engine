import pytest
from app.core.model_router import ModelRole, MODEL_ROUTING


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
    """Streaming lost route_llm_call's retry when it called litellm directly."""
    import app.core.config as _cfg
    import app.models.brain.litellm_conf as _llm
    from app.core.model_router import route_llm_call

    attempts: list[bool] = []

    async def flaky_hybrid(**kwargs):
        attempts.append(bool(kwargs.get("force_cloud")))
        if not kwargs.get("force_cloud"):
            raise RuntimeError("LM Studio: no models loaded")

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
