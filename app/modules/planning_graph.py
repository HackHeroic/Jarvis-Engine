"""Planning sub-graph — wraps existing pipeline functions as LangGraph nodes."""

import operator
from typing import Annotated, Any, Optional, TypedDict
from app.core.jarvis_logger import JARVIS_LOGGER as logger


def _merge_lists(left: list, right: list) -> list:
    """Reducer: merge two lists (used for parallel fan-in on time_slots)."""
    return left + right


class PlanningState(TypedDict):
    user_id: str
    user_model: Any
    planning_goal: Optional[str]
    habits_text: str
    semantic_slots: list
    time_slots: Annotated[list, _merge_lists]  # parallel nodes can both add slots
    constraints: list
    task_chunks: list
    pending_tasks: list
    schedule: Optional[dict]
    horizon_minutes: int
    retry_count: int
    clarification_request: Optional[str]
    error: Optional[str]
    progress_callback: Any
    progress_queue: Any



async def fetch_constraints(state: PlanningState) -> dict:
    user_model = state["user_model"]
    if user_model:
        constraints = await user_model.get_behavioral_constraints()
        habits_text = "\n".join(
            c.get("raw_text", "") for c in constraints if c.get("constraint_type") == "habit"
        )
        return {
            "constraints": constraints,
            "habits_text": habits_text,
            "_tool_detail": {"rows": len(constraints)},
        }
    return {"constraints": [], "habits_text": ""}


async def translate_habits(state: PlanningState) -> dict:
    habits_text = state.get("habits_text", "")
    if not habits_text.strip():
        return {"semantic_slots": [], "_tool_detail": {"slots": 0}}
    try:
        from app.services.analytical.habit_translator import translate_habits_to_slots
        slots = await translate_habits_to_slots(habits_text)
        result = [s.model_dump() for s in slots] if slots else []
        return {"semantic_slots": result, "_tool_detail": {"slots": len(result)}}
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
    """Convert PEARL behavioral patterns into time slot constraints for OR-Tools."""
    user_model = state.get("user_model")
    if not user_model:
        return {"_tool_detail": {"formula": "quality×0.5 + SR×0.3 + reschedule_penalty×0.2"}}
    try:
        memory_store = await user_model.get_memory_store()
        if not memory_store:
            return {"_tool_detail": {"formula": "quality×0.5 + SR×0.3 + reschedule_penalty×0.2"}}
        from app.services.memory.constraint_bridge import memories_to_constraints
        import asyncio
        user_id = state.get("user_id", "demo")
        slots = await asyncio.to_thread(memories_to_constraints, user_id, memory_store)
        if not slots:
            return {"_tool_detail": {"formula": "quality×0.5 + SR×0.3 + reschedule_penalty×0.2"}}
        extra = [s.model_dump() if hasattr(s, 'model_dump') else s for s in slots]
        # Reducer _merge_lists handles combining with expand_slots output
        return {
            "time_slots": extra,
            "_tool_detail": {"formula": "quality×0.5 + SR×0.3 + reschedule_penalty×0.2"},
        }
    except Exception as e:
        logger.warning(f"memory_to_constraints failed (non-fatal): {e}")
        return {"_tool_detail": {"formula": "quality×0.5 + SR×0.3 + reschedule_penalty×0.2"}}


async def validate_goal(state: PlanningState) -> dict:
    goal = state.get("planning_goal", "")
    if not goal or len(goal.strip()) < 5:
        return {"clarification_request": "Could you tell me more about what you'd like to plan?"}
    return {"clarification_request": None}


def is_goal_clear(state: PlanningState) -> bool:
    return state.get("clarification_request") is None


async def decompose_goal(state: PlanningState) -> dict:
    goal = state.get("planning_goal", "")
    system_prompt = (
        "You are a task decomposition expert. Break the user's goal into 5-8 concrete, "
        "actionable micro-tasks of 15-25 minutes each. Each task must have clear completion criteria."
    )

    # Query ChromaDB for relevant knowledge to augment decomposition
    rag_context = ""
    try:
        from app.utils.chroma_client import query_knowledge
        user_id = state.get("user_id", "demo")
        chunks = query_knowledge(user_id, goal, n_results=3)
        if chunks:
            rag_context = "\n\nRelevant knowledge from user's documents:\n" + "\n---\n".join(chunks[:3])
    except Exception:
        pass  # ChromaDB unavailable — proceed without RAG

    if rag_context:
        system_prompt += rag_context

    try:
        from app.core.model_router import route_llm_call
        from app.api.v1.endpoints.reasoning import ExecutionGraph
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
        chunks = [tc.model_dump() for tc in graph.decomposition]
        return {"task_chunks": chunks, "_tool_detail": {"task_count": len(chunks)}}
    except Exception as e:
        logger.error(f"Decomposition failed: {e}")
        return {"error": f"Decomposition failed: {e}", "task_chunks": []}


async def fuse_tasks(state: PlanningState) -> dict:
    user_model = state.get("user_model")
    pending = []
    if user_model:
        pending = await user_model.get_pending_tasks()

    # Merge: add pending tasks that aren't already in new task_chunks
    new_chunks = state.get("task_chunks", [])
    new_ids = {c.get("task_id") for c in new_chunks if c.get("task_id")}
    merged = list(new_chunks)  # start with new tasks
    for p in pending:
        pid = p.get("task_id")
        if pid and pid not in new_ids:
            # Convert pending task row to TaskChunk-compatible dict
            merged.append({
                "task_id": pid,
                "title": p.get("title", ""),
                "duration_minutes": p.get("duration_minutes", 25),
                "difficulty_weight": p.get("difficulty_weight", 0.5),
                "dependencies": p.get("dependencies", []),
                "deadline_hint": p.get("deadline_hint"),
            })

    return {"task_chunks": merged, "pending_tasks": pending}


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
        return {
            "schedule": None,
            "error": "INFEASIBLE",
            "_tool_detail": {"status": "INFEASIBLE"},
        }
    return {
        "schedule": result,
        "error": None,
        "_tool_detail": {
            "status": "OPTIMAL",
            "task_count": len(chunks),
            "horizon_h": horizon // 60,
            "tmt_applied": True,
            "formula": "canonical_steel_konig",
        },
    }


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


from app.core.module_framework import ModuleStep, ModuleDefinition


def planning_state_in(state) -> dict:
    user_model = state.get("user_model")
    brain_dump = state.get("brain_dump")
    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "planning_goal": (
            brain_dump.planning_goal
            if brain_dump and hasattr(brain_dump, "planning_goal")
            else state.get("user_message", "")
        ),
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


def planning_state_out(result: dict, module_name: str) -> dict:
    return {
        "schedule": result.get("schedule"),
        "execution_graph": (
            {"decomposition": result.get("task_chunks", [])}
            if result.get("task_chunks")
            else None
        ),
        "clarification_request": result.get("clarification_request"),
        "error": result.get("error"),
    }


planning_module = ModuleDefinition(
    name="planning",
    state_class=PlanningState,
    state_in=planning_state_in,
    state_out=planning_state_out,
    steps=[
        ModuleStep(name="fetch_constraints", handler=fetch_constraints, concurrent_safe=True),
        ModuleStep(name="translate_habits", handler=translate_habits,
                   depends_on=["fetch_constraints"], timeout_ms=45_000),
        ModuleStep(name="expand_slots", handler=expand_slots,
                   depends_on=["translate_habits"], concurrent_safe=True, read_only=True),
        ModuleStep(name="memory_to_constraints", handler=memory_to_constraints,
                   depends_on=["fetch_constraints"], concurrent_safe=True,
                   feature_flag="ENABLE_PEARL"),
        ModuleStep(name="validate_goal", handler=validate_goal,
                   depends_on=["fetch_constraints"], concurrent_safe=True, read_only=True,
                   routes_to={is_goal_clear: {True: "decompose_goal", False: "__END__"}}),
        ModuleStep(name="decompose_goal", handler=decompose_goal,
                   depends_on=["expand_slots", "memory_to_constraints", "validate_goal"],
                   timeout_ms=60_000),
        ModuleStep(name="fuse_tasks", handler=fuse_tasks, depends_on=["decompose_goal"]),
        ModuleStep(name="solve_schedule", handler=solve_schedule,
                   depends_on=["fuse_tasks"],
                   routes_to={check_feasibility: {"OPTIMAL": "__END__", "INFEASIBLE": "handle_infeasible"}}),
        ModuleStep(name="handle_infeasible", handler=handle_infeasible,
                   routes_to={can_retry: {"retry": "solve_schedule", "exhausted": "__END__"}}),
    ],
)


def build_planning_graph():
    """Backward-compatible shim — delegates to build_module_graph(planning_module)."""
    from app.core.module_framework import build_module_graph
    return build_module_graph(planning_module)
