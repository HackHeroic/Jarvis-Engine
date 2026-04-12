"""End-to-end integration tests for the Jarvis orchestrator graph.

Tests that all real modules are wired correctly and the graph flows through
the full conversation path without errors.
"""

import pytest
from app.orchestrator.graph import build_jarvis_graph
from app.orchestrator.state import ConversationPhase, NegotiationPhase


def _make_initial_state(message="hello"):
    return {
        "user_model": None,
        "user_message": message,
        "brain_dump": None,
        "intent": None,
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "response_message": None,
        "conversation_phase": ConversationPhase.GREETING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "needs_consent": None,
        "error": None,
        "progress_callback": None,
    }


@pytest.mark.asyncio
async def test_chat_flow_end_to_end():
    graph = build_jarvis_graph()
    state = _make_initial_state("hello")
    result = await graph.ainvoke(state)
    assert result.get("response_message") is not None
    assert "conversation_module" in result.get("modules_invoked", [])


@pytest.mark.asyncio
async def test_graph_streams_events():
    graph = build_jarvis_graph()
    state = _make_initial_state("hello")
    config = {"configurable": {"thread_id": "test"}}
    events = []
    async for event in graph.astream(state, config):
        events.append(event)
    assert len(events) > 0
    node_names = [list(e.keys())[0] for e in events]
    assert "observation_loop" in node_names
