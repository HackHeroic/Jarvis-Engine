"""State-aware routing for the Jarvis orchestrator."""

from app.orchestrator.state import ConversationPhase, JarvisState, NegotiationPhase

INTENT_TO_MODULE: dict[str, str] = {
    "PLAN_DAY": "planning_module",
    "EDIT_TASK": "planning_module",
    "REARRANGE": "planning_module",
    "ACCEPT_DRAFT": "planning_module",
    "REJECT_DRAFT": "planning_module",
    "ADD_CONSTRAINT": "planning_module",
    "INGEST_DOCUMENT": "knowledge_module",
    "CALENDAR_SYNC": "knowledge_module",
    "KNOWLEDGE_INGESTION": "knowledge_module",
    "CHECK_PROGRESS": "coach_module",
    "RESEARCH": "research_agent",
    "CHAT": "conversation_module",
    "GREETING": "conversation_module",
    "GENERAL_QA": "conversation_module",
    "BEHAVIORAL_CONSTRAINT": "planning_module",
    "ACTION_ITEM": "knowledge_module",
}


def route_to_module(state: JarvisState) -> str:
    intent = state.get("intent", "CHAT")
    phase = state.get("conversation_phase", ConversationPhase.CHAT)
    invoked = state.get("modules_invoked", [])
    error = state.get("error")
    if phase == ConversationPhase.NEGOTIATION:
        return "planning_module"
    if "planning_module" in invoked and error:
        return "coach_module"
    return INTENT_TO_MODULE.get(intent, "conversation_module")


def check_negotiation_shortcut(state: JarvisState) -> str:
    neg = state.get("negotiation_state", NegotiationPhase.NONE)
    if neg not in (NegotiationPhase.NONE, NegotiationPhase.ACCEPTED):
        return "negotiation_active"
    return "normal"


def check_needs_followup(state: JarvisState) -> bool:
    return state.get("needs_followup", False)
