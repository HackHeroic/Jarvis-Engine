import pytest
from unittest.mock import AsyncMock, MagicMock
from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis

from tests.fakes import CANNED_LLM_REPLY


@pytest.mark.asyncio
async def test_conversation_module_returns_message(no_llm):
    state = {
        "user_model": MagicMock(),
        "user_message": "hello",
        "modules_invoked": [],
    }
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)
    result = await run_general_chat(state)
    assert result["response_message"] == CANNED_LLM_REPLY
    assert "conversation_module" in result["modules_invoked"]
    assert ("route_llm_call", "voice_of_jarvis") in no_llm


@pytest.mark.asyncio
async def test_synthesize_response_wraps_module_output(no_llm):
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
    assert result["response_message"] == CANNED_LLM_REPLY
    assert any(name == "hybrid_route_query" for name, _ in no_llm)


@pytest.mark.asyncio
async def test_coach_module_returns_message(no_llm):
    from app.modules.coach import run_coaching_response
    state = {
        "user_model": MagicMock(),
        "user_message": "how am I doing?",
        "modules_invoked": [],
        "error": None,
    }
    state["user_model"].get_all_tasks = AsyncMock(return_value=[
        {"status": "completed", "title": "Study DSA"},
        {"status": "pending", "title": "Read chapter 5"},
    ])
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.7)
    result = await run_coaching_response(state)
    assert result["response_message"] == CANNED_LLM_REPLY
    assert "coach_module" in result["modules_invoked"]
    assert ("route_llm_call", "voice_of_jarvis") in no_llm


def test_knowledge_graph_compiles():
    from app.modules.knowledge_graph import build_knowledge_graph
    graph = build_knowledge_graph()
    assert graph is not None


def test_knowledge_graph_has_expected_nodes():
    from app.modules.knowledge_graph import build_knowledge_graph
    graph = build_knowledge_graph()
    node_names = set(graph.nodes.keys())
    expected = {"classify_content", "extract_calendar", "ingest_document", "link_to_tasks", "file_operations", "propose_actions"}
    assert expected.issubset(node_names)


def test_research_graph_compiles():
    from app.modules.research_graph import build_research_graph
    graph = build_research_graph()
    assert graph is not None


def test_research_graph_has_expected_nodes():
    from app.modules.research_graph import build_research_graph
    graph = build_research_graph()
    node_names = set(graph.nodes.keys())
    expected = {"plan_research", "execute_search", "evaluate_results", "summarize", "link_to_tasks"}
    assert expected.issubset(node_names)
