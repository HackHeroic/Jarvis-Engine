from app.orchestrator.state import (
    ConversationPhase,
    NegotiationPhase,
    JarvisState,
)


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
