"""Tests for gemini_primary_route and local_primary_route routing helpers."""

import pytest
from unittest.mock import AsyncMock, patch

MODULE = "app.models.brain.litellm_conf"
# GEMINI_API_KEY lives in app.core.config; litellm_conf reads it at call time
# as `_cfg.GEMINI_API_KEY`, so the config module is the only valid patch target.
CONFIG = "app.core.config"


@pytest.fixture
def mock_hybrid():
    """Patch hybrid_route_query and return the mock."""
    with patch(f"{MODULE}.hybrid_route_query", new_callable=AsyncMock) as m:
        m.return_value = "ok"
        yield m


# ── gemini_primary_route ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_primary__cloud_success(mock_hybrid):
    """When Gemini key is set and cloud succeeds, return cloud result."""
    from app.models.brain.litellm_conf import gemini_primary_route

    mock_hybrid.return_value = "cloud-result"

    with patch(f"{CONFIG}.GEMINI_API_KEY", "fake-key"):
        result = await gemini_primary_route(
            user_prompt="plan my day",
            system_prompt="You are Jarvis.",
        )

    assert result == "cloud-result"
    # Called once with force_cloud=True
    assert mock_hybrid.call_count == 1
    _, kwargs = mock_hybrid.call_args
    assert kwargs["force_cloud"] is True


@pytest.mark.asyncio
async def test_gemini_primary__cloud_fails__falls_back_to_local(mock_hybrid):
    """When Gemini call raises, fall back to local SLM."""
    from app.models.brain.litellm_conf import gemini_primary_route

    mock_hybrid.side_effect = [RuntimeError("cloud down"), "local-result"]

    with patch(f"{CONFIG}.GEMINI_API_KEY", "fake-key"):
        result = await gemini_primary_route(
            user_prompt="plan my day",
            system_prompt="You are Jarvis.",
        )

    assert result == "local-result"
    assert mock_hybrid.call_count == 2
    # Second call should use model_override (local fallback)
    _, kwargs = mock_hybrid.call_args
    assert kwargs.get("model_override") is not None
    assert kwargs.get("force_cloud") is None or kwargs.get("force_cloud") is False


@pytest.mark.asyncio
async def test_gemini_primary__no_api_key__goes_straight_to_local(mock_hybrid):
    """When GEMINI_API_KEY is empty, skip cloud entirely."""
    from app.models.brain.litellm_conf import gemini_primary_route

    mock_hybrid.return_value = "local-result"

    with patch(f"{CONFIG}.GEMINI_API_KEY", ""):
        result = await gemini_primary_route(
            user_prompt="plan my day",
            system_prompt="You are Jarvis.",
        )

    assert result == "local-result"
    assert mock_hybrid.call_count == 1
    _, kwargs = mock_hybrid.call_args
    assert kwargs.get("model_override") is not None


# ── local_primary_route ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_primary__local_success(mock_hybrid):
    """When local SLM succeeds, return its result directly."""
    from app.models.brain.litellm_conf import local_primary_route

    mock_hybrid.return_value = "local-result"

    result = await local_primary_route(
        user_prompt="classify intent",
        system_prompt="You are Jarvis.",
    )

    assert result == "local-result"
    assert mock_hybrid.call_count == 1
    _, kwargs = mock_hybrid.call_args
    assert kwargs.get("model_override") is not None


@pytest.mark.asyncio
async def test_local_primary__local_fails__falls_back_to_gemini(mock_hybrid):
    """When local call raises and Gemini key is set, fall back to cloud."""
    from app.models.brain.litellm_conf import local_primary_route

    mock_hybrid.side_effect = [RuntimeError("local down"), "cloud-result"]

    with patch(f"{CONFIG}.GEMINI_API_KEY", "fake-key"):
        result = await local_primary_route(
            user_prompt="classify intent",
            system_prompt="You are Jarvis.",
        )

    assert result == "cloud-result"
    assert mock_hybrid.call_count == 2
    _, kwargs = mock_hybrid.call_args
    assert kwargs["force_cloud"] is True


@pytest.mark.asyncio
async def test_local_primary__local_fails__no_api_key__raises(mock_hybrid):
    """When local fails and no Gemini key, re-raise the original error."""
    from app.models.brain.litellm_conf import local_primary_route

    mock_hybrid.side_effect = RuntimeError("local down")

    with patch(f"{CONFIG}.GEMINI_API_KEY", ""):
        with pytest.raises(RuntimeError, match="local down"):
            await local_primary_route(
                user_prompt="classify intent",
                system_prompt="You are Jarvis.",
            )


@pytest.mark.asyncio
async def test_local_primary__custom_model_override(mock_hybrid):
    """When model_override is provided, use it instead of SLM_ROUTER_MODEL."""
    from app.models.brain.litellm_conf import local_primary_route

    mock_hybrid.return_value = "ok"

    await local_primary_route(
        user_prompt="classify",
        system_prompt="sys",
        model_override="ollama/custom-model",
    )

    _, kwargs = mock_hybrid.call_args
    assert kwargs["model_override"] == "ollama/custom-model"


# ── hybrid_route_query: GEMINI_PRIMARY redirect ─────────────────────


@pytest.mark.asyncio
async def test_hybrid_route__gemini_primary_unstructured__routes_to_cloud(monkeypatch):
    """GEMINI_PRIMARY must redirect even when no response_schema is given."""
    import app.core.config as cfg
    from app.models.brain import litellm_conf

    monkeypatch.setattr(cfg, "GEMINI_PRIMARY", True)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "GEMINI_MODEL", "gemini/gemini-2.5-flash")

    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        class _Msg:  # minimal LiteLLM response shape
            content = "ok"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    monkeypatch.setattr(litellm_conf.litellm, "acompletion", fake_acompletion)
    await litellm_conf.hybrid_route_query(
        system_prompt="s", user_prompt="hello", response_schema=None
    )
    assert captured["model"] == "gemini/gemini-2.5-flash"
