"""Main LangGraph StateGraph — the Jarvis orchestrator.

Replaces execute_agentic_flow() in control_policy.py.
LLM-dependent nodes (load_context, extract_brain_dump, classify_intent) remain as stubs
until LM Studio is available. All other modules are wired to real implementations.
"""

from langgraph.graph import END, StateGraph

from app.orchestrator.state import JarvisState
from app.orchestrator.hooks import get_hooks
from app.orchestrator.routing import (
    check_needs_followup,
    check_negotiation_shortcut,
    route_to_module,
)
from app.modules.planning_graph import build_planning_graph
from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis
from app.modules.coach import run_coaching_response
from app.modules.knowledge_graph import build_knowledge_graph
from app.modules.research_graph import build_research_graph
from app.core.observation import run_observation_loop


# --- Real LLM-powered nodes (replacing stubs) ---

async def _load_context(state: JarvisState) -> dict:
    """Load user context. UserModel is already created in chat_stream_v2."""
    return {}


async def _extract_brain_dump(state: JarvisState) -> dict:
    """Extract planning goal, habits, action items from user message using LLM."""
    from app.schemas.context import BrainDumpExtraction
    from app.services.analytical.control_policy import BRAIN_DUMP_EXTRACTION_PROMPT
    from app.core.model_router import route_llm_call

    user_msg = state.get("user_message", "")
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
    return {"intent": IntentType.CHAT}


# --- Planning sub-graph wrapper ---

_planning_compiled = build_planning_graph()


async def _planning_module_node(state: JarvisState) -> dict:
    """Wrap the planning sub-graph as an orchestrator node."""
    hooks = get_hooks()
    pre = await hooks.execute("PreModuleExecution", module="planning_module", initiated_by=state.get("initiated_by", "user"))
    if pre.decision.value == "deny":
        return {
            "response_message": pre.reason or "Action blocked.",
            "modules_invoked": state.get("modules_invoked", []) + ["planning_module"],
        }
    if pre.decision.value == "ask":
        return {
            "response_message": pre.reason or "Action requires consent.",
            "needs_consent": True,
            "modules_invoked": state.get("modules_invoked", []) + ["planning_module"],
        }

    user_model = state.get("user_model")
    brain_dump = state.get("brain_dump")

    planning_state = {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "planning_goal": brain_dump.planning_goal if brain_dump and hasattr(brain_dump, 'planning_goal') else state.get("user_message", ""),
        "habits_text": "",
        "semantic_slots": [],
        "time_slots": [],
        "constraints": [],
        "task_chunks": [],
        "pending_tasks": [],
        "schedule": None,
        "horizon_minutes": 2880,
        "retry_count": 0,
        "clarification_request": None,
        "error": None,
        "progress_callback": state.get("progress_callback"),
        "progress_queue": state.get("progress_queue"),
    }

    result = await _planning_compiled.ainvoke(planning_state)
    return {
        "schedule": result.get("schedule"),
        "execution_graph": {"decomposition": result.get("task_chunks", [])} if result.get("task_chunks") else None,
        "clarification_request": result.get("clarification_request"),
        "error": result.get("error"),
        "modules_invoked": state.get("modules_invoked", []) + ["planning_module"],
    }


# --- Knowledge sub-graph wrapper ---

_knowledge_compiled = build_knowledge_graph()


async def _knowledge_module_node(state: JarvisState) -> dict:
    """Wrap the knowledge sub-graph as an orchestrator node."""
    hooks = get_hooks()
    pre = await hooks.execute("PreModuleExecution", module="knowledge_module", initiated_by=state.get("initiated_by", "user"))
    if pre.decision.value == "deny":
        return {
            "response_message": pre.reason or "Action blocked.",
            "modules_invoked": state.get("modules_invoked", []) + ["knowledge_module"],
        }
    if pre.decision.value == "ask":
        return {
            "response_message": pre.reason or "Action requires consent.",
            "needs_consent": True,
            "modules_invoked": state.get("modules_invoked", []) + ["knowledge_module"],
        }

    user_model = state.get("user_model")

    # Decode file if present
    _file_bytes = None
    _file_b64 = state.get("file_base64")
    if _file_b64:
        import base64
        _file_bytes = base64.b64decode(_file_b64)

    # Pass db_client through so process_ingestion can write to ingested_documents
    _db_client = getattr(user_model, '_db', None) if user_model else None

    knowledge_state = {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "db_client": _db_client,
        "content": state.get("user_message", ""),
        "file_bytes": _file_bytes,
        "media_type": state.get("file_media_type"),
        "file_name": state.get("file_name"),
        "content_type": None,
        "ingestion_result": None,
        "calendar_result": None,
        "linked_tasks": [],
        "action_proposals": [],
        "error": None,
    }
    result = await _knowledge_compiled.ainvoke(knowledge_state)
    return {
        "ingestion_result": result.get("ingestion_result"),
        "error": result.get("error"),
        "modules_invoked": state.get("modules_invoked", []) + ["knowledge_module"],
    }


# --- Research sub-graph wrapper ---

_research_compiled = build_research_graph()


async def _research_agent_node(state: JarvisState) -> dict:
    """Wrap the research sub-graph as an orchestrator node."""
    hooks = get_hooks()
    pre = await hooks.execute("PreModuleExecution", module="research_agent", initiated_by=state.get("initiated_by", "user"))
    if pre.decision.value == "deny":
        return {
            "response_message": pre.reason or "Action blocked.",
            "modules_invoked": state.get("modules_invoked", []) + ["research_agent"],
        }
    if pre.decision.value == "ask":
        return {
            "response_message": pre.reason or "Action requires consent.",
            "needs_consent": True,
            "modules_invoked": state.get("modules_invoked", []) + ["research_agent"],
        }

    user_model = state.get("user_model")
    research_state = {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "query": state.get("user_message", ""),
        "search_results": [],
        "iteration_count": 0,
        "max_iterations": 3,
        "summary": None,
        "linked_tasks": [],
        "error": None,
    }
    result = await _research_compiled.ainvoke(research_state)
    return {
        "research_results": result.get("search_results"),
        "error": result.get("error"),
        "modules_invoked": state.get("modules_invoked", []) + ["research_agent"],
    }


def build_jarvis_graph(checkpointer=None):
    """Build and compile the Jarvis orchestrator graph."""
    graph = StateGraph(JarvisState)

    # LLM-powered nodes
    graph.add_node("load_context", _load_context)
    graph.add_node("extract_brain_dump", _extract_brain_dump)
    graph.add_node("classify_intent", _classify_intent)

    # Real module nodes
    graph.add_node("planning_module", _planning_module_node)
    graph.add_node("research_agent", _research_agent_node)
    graph.add_node("coach_module", run_coaching_response)
    graph.add_node("knowledge_module", _knowledge_module_node)
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

    for module in ["planning_module", "research_agent", "coach_module", "knowledge_module"]:
        graph.add_edge(module, "synthesize_response")
    graph.add_edge("synthesize_response", "observation_loop")

    graph.add_edge("conversation_module", "observation_loop")

    graph.add_conditional_edges(
        "observation_loop",
        check_needs_followup,
        {"continue": "classify_intent", "done": END},
    )

    return graph.compile(checkpointer=checkpointer)
