"""State-aware routing for the Jarvis orchestrator."""

from app.orchestrator.state import ConversationPhase, JarvisState, NegotiationPhase

# The four verbs a user aims at a draft already on the table. They do NOT go to
# planning_module: re-running the planner would propose a *second* draft instead
# of resolving the one under review.
DRAFT_ACTION_INTENTS: frozenset[str] = frozenset(
    {"ACCEPT_DRAFT", "REJECT_DRAFT", "EDIT_TASK", "REARRANGE"}
)

INTENT_TO_MODULE: dict[str, str] = {
    "PLAN_DAY": "planning_module",
    "EDIT_TASK": "draft_action",
    "REARRANGE": "draft_action",
    "ACCEPT_DRAFT": "draft_action",
    "REJECT_DRAFT": "draft_action",
    "ADD_CONSTRAINT": "planning_module",
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


def _intent_value(state: JarvisState) -> str:
    """Intent as a plain string — it may be an ``IntentType`` or already a str."""
    intent = state.get("intent", "CHAT")
    return getattr(intent, "value", intent)


def route_to_module(state: JarvisState) -> str:
    intent = _intent_value(state)
    phase = state.get("conversation_phase", ConversationPhase.CHAT)
    invoked = state.get("modules_invoked", [])
    error = state.get("error")
    # Checked before the phase and error overrides: "accept" must resolve the
    # draft even mid-negotiation, and especially after a planning failure —
    # sending it to planning_module or coach_module would strand the review.
    if intent in DRAFT_ACTION_INTENTS:
        return "draft_action"
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


def route_draft_action(state: JarvisState) -> str:
    """Branch taken after ``_negotiation_precheck`` on an active-negotiation turn.

    The negotiation shortcut used to send *every* such turn straight to
    planning_module, which meant a bare "accept" re-ran the whole planner. The
    pre-check node classifies the message with a regex first (no LLM); only a
    real draft verb diverts here, everything else keeps the original
    fall-through so "also add chemistry revision" still re-plans.
    """
    if _intent_value(state) in DRAFT_ACTION_INTENTS:
        return "draft_action"
    return "planning_module"


def check_needs_followup(state: JarvisState) -> str:
    if state.get("needs_followup", False):
        return "continue"
    return "done"
