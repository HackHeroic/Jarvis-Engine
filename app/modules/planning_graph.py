"""Planning sub-graph — wraps existing pipeline functions as LangGraph nodes."""

from typing import Any, Optional, TypedDict
from langgraph.graph import END, StateGraph
from app.core.jarvis_logger import JARVIS_LOGGER as logger


class PlanningState(TypedDict):
    user_id: str
    user_model: Any
    planning_goal: Optional[str]
    habits_text: str
    semantic_slots: list
    time_slots: list
    constraints: list
    task_chunks: list
    pending_tasks: list
    schedule: Optional[dict]
    horizon_minutes: int
    retry_count: int
    clarification_request: Optional[str]
    error: Optional[str]
    progress_callback: Any


async def fetch_constraints(state: PlanningState) -> dict:
    cb = state.get("progress_callback")
    if cb:
        cb("habits_fetched")
    user_model = state["user_model"]
    if user_model:
        constraints = await user_model.get_behavioral_constraints()
        habits_text = "\n".join(c.get("raw_text", "") for c in constraints if c.get("constraint_type") == "habit")
        return {"constraints": constraints, "habits_text": habits_text}
    return {"constraints": [], "habits_text": ""}


async def translate_habits(state: PlanningState) -> dict:
    cb = state.get("progress_callback")
    if cb:
        cb("translating")
    habits_text = state.get("habits_text", "")
    if not habits_text.strip():
        return {"semantic_slots": []}
    try:
        from app.services.analytical.habit_translator import translate_habits_to_slots
        slots = await translate_habits_to_slots(habits_text)
        return {"semantic_slots": [s.model_dump() for s in slots] if slots else []}
    except Exception as e:
        logger.warning(f"Habit translation failed: {e}")
        return {"semantic_slots": []}


async def expand_slots(state: PlanningState) -> dict:
    """Expand semantic slots to concrete time slots for OR-Tools."""
    semantic_slots = state.get("semantic_slots", [])
    if not semantic_slots:
        return {"time_slots": []}
    try:
        from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots
        from app.schemas.context import SemanticTimeSlot
        # Convert dicts back to SemanticTimeSlot objects
        slot_objects = [SemanticTimeSlot.model_validate(s) for s in semantic_slots]
        horizon = state.get("horizon_minutes", 2880)
        time_slots = expand_semantic_slots_to_time_slots(slot_objects, horizon)
        return {"time_slots": [ts.model_dump() if hasattr(ts, 'model_dump') else ts for ts in time_slots]}
    except Exception as e:
        logger.warning(f"Horizon expansion failed: {e}")
        return {"time_slots": []}


async def memory_to_constraints(state: PlanningState) -> dict:
    return {}


async def validate_goal(state: PlanningState) -> dict:
    goal = state.get("planning_goal", "")
    if not goal or len(goal.strip()) < 5:
        return {"clarification_request": "Could you tell me more about what you'd like to plan?"}
    return {"clarification_request": None}


def is_goal_clear(state: PlanningState) -> bool:
    return state.get("clarification_request") is None


async def decompose_goal(state: PlanningState) -> dict:
    cb = state.get("progress_callback")
    if cb:
        cb("decomposing")
    goal = state.get("planning_goal", "")
    system_prompt = (
        "You are a task decomposition expert. Break the user's goal into 5-8 concrete, "
        "actionable micro-tasks of 15-25 minutes each. Each task must have clear completion criteria."
    )
    try:
        from app.core.model_router import route_llm_call
        from app.schemas.context import ExecutionGraph
        result = await route_llm_call(
            task="socratic_chunker",
            prompt=goal,
            system_prompt=system_prompt,
            response_schema=ExecutionGraph,
        )
        if isinstance(result, ExecutionGraph):
            graph = result
        else:
            import json, re
            clean = re.sub(r"```json|```", "", str(result)).strip()
            data = json.loads(clean)
            graph = ExecutionGraph.model_validate(data)
        return {"task_chunks": [tc.model_dump() for tc in graph.decomposition]}
    except Exception as e:
        logger.error(f"Decomposition failed: {e}")
        return {"error": f"Decomposition failed: {e}", "task_chunks": []}


async def fuse_tasks(state: PlanningState) -> dict:
    cb = state.get("progress_callback")
    if cb:
        cb("scheduling")
    user_model = state.get("user_model")
    pending = []
    if user_model:
        pending = await user_model.get_pending_tasks()
    return {"pending_tasks": pending}


async def solve_schedule(state: PlanningState) -> dict:
    from app.core.or_tools.solver import JarvisScheduler
    chunks = state.get("task_chunks", [])
    horizon = state.get("horizon_minutes", 2880)
    if not chunks:
        return {"error": "No tasks to schedule", "schedule": None}
    scheduler = JarvisScheduler(horizon_minutes=horizon)
    for slot in state.get("time_slots", []):
        if slot.get("availability") == "blocked":
            scheduler.add_hard_block(slot["start_min"], slot["end_min"], slot.get("name", "block"))
        elif slot.get("availability") == "minimal_work":
            scheduler.add_soft_block(slot["start_min"], slot["end_min"], slot.get("name", "soft"),
                                     max_task_duration=slot.get("max_task_duration", 15),
                                     max_difficulty=slot.get("max_difficulty", 0.4))
    for i, chunk in enumerate(chunks):
        scheduler.add_task(
            task_id=chunk.get("task_id", f"t{i}"),
            duration=chunk.get("duration_minutes", 25),
            priority_score=len(chunks) - i,
            dependencies=chunk.get("dependencies", []),
            difficulty_weight=chunk.get("difficulty_weight", 0.5),
        )
    scheduler.build_dependencies()
    result, status = scheduler.solve()
    if status == "INFEASIBLE":
        return {"schedule": None, "error": "INFEASIBLE"}
    return {"schedule": result, "error": None}


async def handle_infeasible(state: PlanningState) -> dict:
    retry_count = state.get("retry_count", 0)
    HORIZON_RETRY_SEQUENCE = [4320, 7200]
    if retry_count < len(HORIZON_RETRY_SEQUENCE):
        return {"horizon_minutes": HORIZON_RETRY_SEQUENCE[retry_count], "retry_count": retry_count + 1, "error": None}
    return {
        "error": "INFEASIBLE_EXHAUSTED",
        "clarification_request": (
            "I couldn't fit everything in even with a 5-day window. "
            "This is a scope problem, not a you problem. "
            "Want to reduce scope or extend the deadline?"
        ),
    }


def check_feasibility(state: PlanningState) -> str:
    if state.get("error") == "INFEASIBLE":
        return "INFEASIBLE"
    return "OPTIMAL"


def can_retry(state: PlanningState) -> str:
    if state.get("error") == "INFEASIBLE_EXHAUSTED":
        return "exhausted"
    return "retry"


def build_planning_graph():
    graph = StateGraph(PlanningState)
    graph.add_node("fetch_constraints", fetch_constraints)
    graph.add_node("translate_habits", translate_habits)
    graph.add_node("expand_slots", expand_slots)
    graph.add_node("memory_to_constraints", memory_to_constraints)
    graph.add_node("validate_goal", validate_goal)
    graph.add_node("decompose_goal", decompose_goal)
    graph.add_node("fuse_tasks", fuse_tasks)
    graph.add_node("solve_schedule", solve_schedule)
    graph.add_node("handle_infeasible", handle_infeasible)

    graph.set_entry_point("fetch_constraints")
    graph.add_edge("fetch_constraints", "translate_habits")
    graph.add_edge("translate_habits", "expand_slots")
    graph.add_edge("expand_slots", "memory_to_constraints")
    graph.add_edge("memory_to_constraints", "validate_goal")
    graph.add_conditional_edges("validate_goal", is_goal_clear, {True: "decompose_goal", False: END})
    graph.add_edge("decompose_goal", "fuse_tasks")
    graph.add_edge("fuse_tasks", "solve_schedule")
    graph.add_conditional_edges("solve_schedule", check_feasibility, {"OPTIMAL": END, "INFEASIBLE": "handle_infeasible"})
    graph.add_conditional_edges("handle_infeasible", can_retry, {"retry": "solve_schedule", "exhausted": END})

    return graph.compile()
