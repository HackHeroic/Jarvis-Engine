"""Main LangGraph StateGraph — the Jarvis orchestrator.

Replaces execute_agentic_flow() in control_policy.py.
"""

import re

from langgraph.graph import END, StateGraph

from app.orchestrator.state import JarvisState
from app.orchestrator.routing import (
    check_needs_followup,
    check_negotiation_shortcut,
    route_to_module,
)
from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis
from app.modules.coach import run_coaching_response
from app.core.observation import run_observation_loop
from app.modules import module_registry
from app.orchestrator.module_wrapper import create_module_wrapper

# Trivial inputs that should ALWAYS be classified as CHAT — bypass 26B brain-dump extraction
# (which over-extracts greetings into search_queries / action_items when E4B isn't loaded).
_TRIVIAL_INPUT_RE = re.compile(
    r"^\s*(hi+|hey+|hello+|yo+|sup|hola|namaste|hmm+|huh|aha|ah|oh|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"how\s+(are\s+you|r\s+u|you\s+doing)|"
    r"what'?s\s+up|wassup|"
    r"thanks?|thank\s+you|ty|cheers|"
    r"ok+|okay|cool|nice|great|alright|"
    r"bye|goodbye|see\s+ya|cya"
    r")[\s!.?]*$",
    re.IGNORECASE,
)

# Emotional disclosure — the LLM should respond with empathy via the conversation
# module, NOT extract this as a behavioral habit constraint.
_EMOTIONAL_INPUT_RE = re.compile(
    r"\b(i\s*(am|'m|m)\s+|i\s+(feel|felt|feeling)\s+|"
    r"feeling\s+|i\s+got\s+|today\s+i'?m?\s+)"
    r"(sad|happy|anxious|tired|exhausted|overwhelmed|stressed|angry|frustrated|"
    r"depressed|low|down|lonely|scared|afraid|worried|nervous|hopeful|"
    r"excited|grateful|content|relaxed|calm|peaceful|blah|meh|off|fine|okay|ok)"
    r"\b",
    re.IGNORECASE,
)


# Planning / task verbs — if any of these appear, the message is NOT trivial
# even if it ALSO contains emotional language (e.g. "I'm sad, please plan my week").
# NOTE: bare "today" intentionally excluded — appears in "I am sad today"
# without any planning intent. "tomorrow" and "tonight" stay (clearer signals).
_PLANNING_VERBS_RE = re.compile(
    r"\b(plan|schedule|organize|organise|prepare|prep|study|learn|build|create|"
    r"design|write|review|finish|complete|deliver|tomorrow|tonight|"
    r"this\s+(week|month|weekend|morning|afternoon|evening)|"
    r"next\s+(week|month|day|weekend)|deadline|exam|test|interview|presentation|"
    r"contest|hackathon|assignment|homework|task|goal|project|chapter|"
    r"add\s+a\s+task|remind\s+me|create\s+a\s+task)\b",
    re.IGNORECASE,
)


def _is_trivial_input(text: str) -> bool:
    """Return True for inputs that should bypass 26B brain-dump extraction.

    Three categories route straight to CHAT (which DOES call the LLM via
    conversation module — this never returns hardcoded text):
      1. Empty/very short messages
      2. Greetings, acks, fillers
      3. SHORT emotional disclosure with NO planning verb
         (avoids dropping legitimate work like "I'm sad, plan my week")
    """
    if not text or not text.strip():
        return True
    s = text.strip()
    if len(s) < 3:
        return True
    if _TRIVIAL_INPUT_RE.match(s):
        return True
    # Emotional fast path is only safe when there's no work intent in the message
    if _EMOTIONAL_INPUT_RE.search(s):
        if _PLANNING_VERBS_RE.search(s):
            return False  # legitimate planning request that happens to be emotional
        if len(s) <= 80:
            return True
    return False


# --- Real LLM-powered nodes (replacing stubs) ---

async def _load_context(state: JarvisState) -> dict:
    """Load user context. UserModel is already created in chat_stream_v2."""
    return {}


async def _extract_brain_dump(state: JarvisState) -> dict:
    """Extract planning goal, habits, action items from user message using LLM.

    Trivial inputs (greetings, acks) skip the LLM entirely → routes to CHAT.
    Avoids 26B over-extraction when E4B isn't loaded.
    """
    from app.schemas.context import BrainDumpExtraction
    from app.services.analytical.control_policy import BRAIN_DUMP_EXTRACTION_PROMPT
    from app.core.model_router import route_llm_call

    user_msg = state.get("user_message", "")

    # Greeting / ack fast path — no LLM call, returns null brain_dump → CHAT intent
    if _is_trivial_input(user_msg):
        return {"brain_dump": None, "trivial_input": True}

    if not user_msg.strip():
        return {"brain_dump": None}

    try:
        result = await route_llm_call(
            task="brain_dump_extraction",
            prompt=user_msg,
            system_prompt=BRAIN_DUMP_EXTRACTION_PROMPT,
            response_schema=BrainDumpExtraction,
            conversation_history=state.get("conversation_history"),
        )
        if isinstance(result, BrainDumpExtraction):
            return {"brain_dump": result}
        if isinstance(result, dict):
            return {"brain_dump": BrainDumpExtraction.model_validate(result)}
        return {"brain_dump": None}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Brain dump extraction failed: {e}")
        return {"brain_dump": None}


async def _classify_intent(state: JarvisState) -> dict:
    """Classify intent from extracted brain dump fields. Rule-based, no LLM needed."""
    from app.schemas.context import IntentType

    # File upload → always KNOWLEDGE_INGESTION regardless of brain dump
    if state.get("file_base64"):
        return {"intent": IntentType.KNOWLEDGE_INGESTION}

    bd = state.get("brain_dump")
    if not bd:
        return {"intent": IntentType.CHAT}

    # Rule-based classification matching control_policy.py logic
    if bd.planning_goal:
        return {"intent": IntentType.PLAN_DAY}
    if bd.has_calendar:
        return {"intent": IntentType.CALENDAR_SYNC}
    if bd.has_knowledge:
        return {"intent": IntentType.KNOWLEDGE_INGESTION}
    if bd.inline_habits:
        return {"intent": IntentType.BEHAVIORAL_CONSTRAINT}
    if bd.action_items:
        return {"intent": IntentType.ACTION_ITEM}
    if bd.search_queries:
        return {"intent": IntentType.RESEARCH}
    # Check for progress-related keywords in the original message
    user_msg = (state.get("user_message") or "").lower()
    progress_keywords = ["how am i doing", "my progress", "check progress", "how's my", "mastery", "how far", "status update", "how much have i"]
    if any(kw in user_msg for kw in progress_keywords):
        return {"intent": IntentType.CHECK_PROGRESS}
    return {"intent": IntentType.CHAT}


def build_jarvis_graph(checkpointer=None):
    """Build and compile the Jarvis orchestrator graph."""
    graph = StateGraph(JarvisState)

    # LLM-powered nodes
    graph.add_node("load_context", _load_context)
    graph.add_node("extract_brain_dump", _extract_brain_dump)
    graph.add_node("classify_intent", _classify_intent)

    # Real module nodes (from registry)
    for name in module_registry.registered_names():
        graph.add_node(name, create_module_wrapper(name, module_registry))
    graph.add_node("coach_module", run_coaching_response)
    graph.add_node("conversation_module", run_general_chat)
    graph.add_node("synthesize_response", voice_of_jarvis_synthesis)
    graph.add_node("observation_loop", run_observation_loop)

    graph.set_entry_point("load_context")

    graph.add_conditional_edges(
        "load_context",
        check_negotiation_shortcut,
        {"negotiation_active": "planning_module", "normal": "extract_brain_dump"},
    )

    graph.add_edge("extract_brain_dump", "classify_intent")

    graph.add_conditional_edges(
        "classify_intent",
        route_to_module,
        {
            "planning_module": "planning_module",
            "research_agent": "research_agent",
            "coach_module": "coach_module",
            "knowledge_module": "knowledge_module",
            "conversation_module": "conversation_module",
        },
    )

    for name in module_registry.registered_names():
        graph.add_edge(name, "synthesize_response")
    graph.add_edge("coach_module", "synthesize_response")
    graph.add_edge("synthesize_response", "observation_loop")

    graph.add_edge("conversation_module", "observation_loop")

    graph.add_conditional_edges(
        "observation_loop",
        check_needs_followup,
        {"continue": "classify_intent", "done": END},
    )

    return graph.compile(checkpointer=checkpointer)
