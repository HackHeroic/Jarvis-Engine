"""Research agent sub-graph — autonomous, can iterate."""

from typing import Any, Optional, TypedDict
from langgraph.graph import END, StateGraph
from app.core.jarvis_logger import JARVIS_LOGGER as logger


class ResearchState(TypedDict):
    user_id: str
    user_model: Any
    query: str
    search_results: list
    iteration_count: int
    max_iterations: int
    summary: Optional[str]
    linked_tasks: list
    error: Optional[str]


async def plan_research(state: ResearchState) -> dict:
    return {"search_results": [], "iteration_count": 0, "max_iterations": 3}


async def execute_search(state: ResearchState) -> dict:
    query = state.get("query", "")
    try:
        from app.services.analytical.workspace_builder import perform_learning_style_search
        results = await perform_learning_style_search(query, "reader")
        return {
            "search_results": state.get("search_results", []) + [r.model_dump() for r in results],
            "iteration_count": state.get("iteration_count", 0) + 1,
        }
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return {"error": str(e), "iteration_count": state.get("iteration_count", 0) + 1}


def needs_more(state: ResearchState) -> bool:
    if state.get("error"):
        return False  # don't retry on error
    results = state.get("search_results", [])
    iterations = state.get("iteration_count", 0)
    max_iter = state.get("max_iterations", 3)
    return len(results) < 3 and iterations < max_iter


async def summarize(state: ResearchState) -> dict:
    results = state.get("search_results", [])
    return {"summary": f"Found {len(results)} results for your query."}


async def link_to_tasks(state: ResearchState) -> dict:
    return {"linked_tasks": []}


def build_research_graph():
    graph = StateGraph(ResearchState)
    graph.add_node("plan_research", plan_research)
    graph.add_node("execute_search", execute_search)
    graph.add_node("evaluate_results", lambda s: {})
    graph.add_node("summarize", summarize)
    graph.add_node("link_to_tasks", link_to_tasks)

    graph.set_entry_point("plan_research")
    graph.add_edge("plan_research", "execute_search")
    graph.add_edge("execute_search", "evaluate_results")
    graph.add_conditional_edges("evaluate_results", needs_more, {True: "execute_search", False: "summarize"})
    graph.add_edge("summarize", "link_to_tasks")
    graph.add_edge("link_to_tasks", END)
    return graph.compile()
