"""Research agent sub-graph — autonomous, can iterate."""

from typing import Any, Optional, TypedDict
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
    from app.core.module_framework import build_module_graph
    return build_module_graph(research_module)


from app.core.module_framework import ModuleStep, ModuleDefinition


def research_state_in(state) -> dict:
    user_model = state.get("user_model")
    return {
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


def research_state_out(result: dict, module_name: str) -> dict:
    return {
        "research_results": result.get("search_results"),
        "error": result.get("error"),
    }


research_module = ModuleDefinition(
    name="research_agent",
    state_class=ResearchState,
    state_in=research_state_in,
    state_out=research_state_out,
    steps=[
        ModuleStep(name="plan_research", handler=plan_research),
        ModuleStep(name="execute_search", handler=execute_search,
                   depends_on=["plan_research"], timeout_ms=30_000),
        ModuleStep(name="evaluate_results", handler=lambda s: {},
                   depends_on=["execute_search"], read_only=True,
                   routes_to={needs_more: {True: "execute_search", False: "summarize"}}),
        ModuleStep(name="summarize", handler=summarize, timeout_ms=45_000),
        ModuleStep(name="link_to_tasks", handler=link_to_tasks,
                   depends_on=["summarize"]),
    ],
)
