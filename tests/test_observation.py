# tests/test_observation.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.core.observation import run_observation_loop
from app.orchestrator.state import ConversationPhase, NegotiationPhase


def _make_state(**overrides):
    base = {
        "user_model": MagicMock(),
        "user_message": "plan my DSA study",
        "response_message": "Here's your schedule...",
        "brain_dump": None,
        "intent": "PLAN_DAY",
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "conversation_phase": ConversationPhase.PLANNING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": ["planning_module"],
        "needs_followup": False,
        "needs_consent": None,
        "error": None,
        "progress_callback": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_observation_loop_returns_state():
    state = _make_state()
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)
    state["user_model"].get_memory_store = AsyncMock(return_value=None)
    result = await run_observation_loop(state)
    assert result is not None
    assert result.get("needs_followup") is False


@pytest.mark.asyncio
async def test_observation_loop_calls_pearl():
    state = _make_state()
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)
    state["user_model"].get_memory_store = AsyncMock(return_value=None)
    await run_observation_loop(state)
    state["user_model"].get_pearl_patterns.assert_called_once()
