"""Main LangGraph StateGraph — the Jarvis orchestrator.

Replaces execute_agentic_flow() in control_policy.py.
Nodes are stubs that will be replaced by real module implementations in later tasks.
"""

from langgraph.graph import END, StateGraph

from app.orchestrator.state import JarvisState
from app.orchestrator.routing import (
    check_needs_followup,
    check_negotiation_shortcut,
    route_to_module,
)
from app.modules.planning_graph import build_planning_graph


async def _stub_load_context(state: JarvisState) -> dict:
    return {}


async def _stub_extract_brain_dump(state: JarvisState) -> dict:
    return {"brain_dump": None}


async def _stub_classify_intent(state: JarvisState) -> dict:
    return {"intent": "CHAT"}


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


async def _stub_research(state: JarvisState) -> dict:
    return {"modules_invoked": state.get("modules_invoked", []) + ["research_agent"]}


async def _stub_coach(state: JarvisState) -> dict:
    return {"modules_invoked": state.get("modules_invoked", []) + ["coach_module"]}


async def _stub_knowledge(state: JarvisState) -> dict:
    return {"modules_invoked": state.get("modules_invoked", []) + ["knowledge_module"]}


async def _stub_conversation(state: JarvisState) -> dict:
    return {
        "response_message": "Hello! I'm Jarvis.",
        "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
    }


async def _stub_synthesize(state: JarvisState) -> dict:
    return {"response_message": state.get("response_message", "Synthesized response.")}


async def _stub_observation(state: JarvisState) -> dict:
    return {"needs_followup": False}


def build_jarvis_graph(checkpointer=None):
    """Build and compile the Jarvis orchestrator graph."""
    graph = StateGraph(JarvisState)

    graph.add_node("load_context", _stub_load_context)
    graph.add_node("extract_brain_dump", _stub_extract_brain_dump)
    graph.add_node("classify_intent", _stub_classify_intent)
    graph.add_node("planning_module", _planning_module_node)
    graph.add_node("research_agent", _stub_research)
    graph.add_node("coach_module", _stub_coach)
    graph.add_node("knowledge_module", _stub_knowledge)
    graph.add_node("conversation_module", _stub_conversation)
    graph.add_node("synthesize_response", _stub_synthesize)
    graph.add_node("observation_loop", _stub_observation)

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
        {True: "classify_intent", False: END},
    )

    return graph.compile(checkpointer=checkpointer)
