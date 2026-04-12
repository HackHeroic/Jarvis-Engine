import pytest
from unittest.mock import AsyncMock, MagicMock
from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis


@pytest.mark.asyncio
async def test_conversation_module_returns_message():
    state = {
        "user_model": MagicMock(),
        "user_message": "hello",
        "modules_invoked": [],
    }
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)
    result = await run_general_chat(state)
    assert "response_message" in result
    assert "conversation_module" in result["modules_invoked"]


@pytest.mark.asyncio
async def test_synthesize_response_wraps_module_output():
    state = {
        "user_model": MagicMock(),
        "schedule": {"t1": {"start": 480, "end": 510}},
        "execution_graph": {"decomposition": [{"title": "Study DSA"}]},
        "response_message": None,
        "conversation_phase": "planning",
        "intent": "PLAN_DAY",
        "user_message": "plan my day",
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "error": None,
    }
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)
    result = await voice_of_jarvis_synthesis(state)
    assert "response_message" in result


@pytest.mark.asyncio
async def test_coach_module_returns_message():
    from app.modules.coach import run_coaching_response
    state = {
        "user_model": MagicMock(),
        "user_message": "how am I doing?",
        "modules_invoked": [],
        "error": None,
    }
    state["user_model"].get_pending_tasks = AsyncMock(return_value=[
        {"status": "completed", "title": "Study DSA"},
        {"status": "pending", "title": "Read chapter 5"},
    ])
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.7)
    result = await run_coaching_response(state)
    assert "response_message" in result
    assert "coach_module" in result["modules_invoked"]
