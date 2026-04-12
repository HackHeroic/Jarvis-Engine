from app.orchestrator.state import (
    ConversationPhase,
    NegotiationPhase,
    JarvisState,
)
from app.schemas.context import IntentType


def test_conversation_phase_values():
    assert ConversationPhase.GREETING == "greeting"
    assert ConversationPhase.PLANNING == "planning"
    assert ConversationPhase.NEGOTIATION == "negotiation"
    assert ConversationPhase.REVIEW == "review"
    assert ConversationPhase.CHAT == "chat"


def test_negotiation_phase_values():
    assert NegotiationPhase.NONE == "none"
    assert NegotiationPhase.PROPOSED == "proposed"
    assert NegotiationPhase.REVIEWING == "reviewing"
    assert NegotiationPhase.EDITING == "editing"
    assert NegotiationPhase.ACCEPTED == "accepted"


def test_jarvis_state_is_typed_dict():
    """JarvisState should be a TypedDict usable by LangGraph."""
    state: JarvisState = {
        "user_model": None,
        "user_message": "plan my day",
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
        "error": None,
    }
    assert state["user_message"] == "plan my day"
    assert state["conversation_phase"] == ConversationPhase.GREETING


def test_new_intent_types_exist():
    assert IntentType.EDIT_TASK == "EDIT_TASK"
    assert IntentType.REARRANGE == "REARRANGE"
    assert IntentType.ACCEPT_DRAFT == "ACCEPT_DRAFT"
    assert IntentType.REJECT_DRAFT == "REJECT_DRAFT"
    assert IntentType.ADD_CONSTRAINT == "ADD_CONSTRAINT"
    assert IntentType.CHECK_PROGRESS == "CHECK_PROGRESS"
    assert IntentType.RESEARCH == "RESEARCH"
    assert IntentType.CHAT == "CHAT"


# ---------------------------------------------------------------------------
# Routing tests (Task 5)
# ---------------------------------------------------------------------------

from app.orchestrator.routing import (
    route_to_module,
    check_negotiation_shortcut,
    check_needs_followup,
    INTENT_TO_MODULE,
)


def _make_state(**overrides) -> JarvisState:
    base: JarvisState = {
        "user_model": None,
        "user_message": "test",
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
        "response_message": None,
        "conversation_phase": ConversationPhase.CHAT,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "error": None,
    }
    base.update(overrides)
    return base


def test_route_plan_day():
    state = _make_state(intent="PLAN_DAY")
    assert route_to_module(state) == "planning_module"


def test_route_chat_fallback():
    state = _make_state(intent="UNKNOWN_INTENT")
    assert route_to_module(state) == "conversation_module"


def test_route_negotiation_overrides_intent():
    state = _make_state(intent="CHAT", conversation_phase=ConversationPhase.NEGOTIATION)
    assert route_to_module(state) == "planning_module"


def test_route_infeasible_fallback_to_coach():
    state = _make_state(intent="PLAN_DAY", modules_invoked=["planning_module"], error="INFEASIBLE")
    assert route_to_module(state) == "coach_module"


def test_negotiation_shortcut_active():
    state = _make_state(negotiation_state=NegotiationPhase.REVIEWING)
    assert check_negotiation_shortcut(state) == "negotiation_active"


def test_negotiation_shortcut_normal():
    state = _make_state(negotiation_state=NegotiationPhase.NONE)
    assert check_negotiation_shortcut(state) == "normal"


def test_needs_followup_false():
    state = _make_state(needs_followup=False)
    assert check_needs_followup(state) == "done"


def test_needs_followup_true():
    state = _make_state(needs_followup=True)
    assert check_needs_followup(state) == "continue"


# ---------------------------------------------------------------------------
# Graph tests (Task 6)
# ---------------------------------------------------------------------------

import pytest
from app.orchestrator.graph import build_jarvis_graph


@pytest.mark.asyncio
async def test_graph_compiles():
    graph = build_jarvis_graph()
    assert graph is not None


@pytest.mark.asyncio
async def test_graph_has_expected_nodes():
    graph = build_jarvis_graph()
    node_names = set(graph.nodes.keys())
    expected = {
        "load_context",
        "extract_brain_dump",
        "classify_intent",
        "planning_module",
        "research_agent",
        "coach_module",
        "knowledge_module",
        "conversation_module",
        "synthesize_response",
        "observation_loop",
    }
    assert expected.issubset(node_names), f"Missing nodes: {expected - node_names}"


@pytest.mark.asyncio
async def test_graph_runs_chat_end_to_end():
    graph = build_jarvis_graph()
    initial_state = _make_state(user_message="hello")
    result = await graph.ainvoke(initial_state)
    assert result["response_message"] is not None
    assert "conversation_module" in result["modules_invoked"]
    assert result["needs_followup"] is False
