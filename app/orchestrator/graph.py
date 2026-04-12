"""Main LangGraph StateGraph — the Jarvis orchestrator.

Replaces execute_agentic_flow() in control_policy.py.
LLM-dependent nodes (load_context, extract_brain_dump, classify_intent) remain as stubs
until LM Studio is available. All other modules are wired to real implementations.
"""

from langgraph.graph import END, StateGraph

from app.orchestrator.state import JarvisState
from app.orchestrator.hooks import ActionHooks, register_all_hooks
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


# --- LLM-dependent stubs (kept until LM Studio is available) ---

async def _stub_load_context(state: JarvisState) -> dict:
    return {}


async def _stub_extract_brain_dump(state: JarvisState) -> dict:
    return {"brain_dump": None}


async def _stub_classify_intent(state: JarvisState) -> dict:
    return {"intent": "CHAT"}


# --- Planning sub-graph wrapper ---

_planning_compiled = build_planning_graph()


async def _planning_module_node(state: JarvisState) -> dict:
    """Wrap the planning sub-graph as an orchestrator node."""
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
        "progress_callback": None,
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
    user_model = state.get("user_model")
    knowledge_state = {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "content": state.get("user_message", ""),
        "file_bytes": None,
        "media_type": None,
        "file_name": None,
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
    hooks = ActionHooks()
    register_all_hooks(hooks)

    graph = StateGraph(JarvisState)

    # LLM-dependent nodes (stubs until LM Studio is available)
    graph.add_node("load_context", _stub_load_context)
    graph.add_node("extract_brain_dump", _stub_extract_brain_dump)
    graph.add_node("classify_intent", _stub_classify_intent)

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
