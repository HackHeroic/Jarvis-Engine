"""Planning sub-graph — wraps existing pipeline functions as LangGraph nodes."""

import asyncio
import operator
from typing import Annotated, Any, Optional, TypedDict
from app.core.jarvis_logger import JARVIS_LOGGER as logger

# Shared with the orchestrator's draft_action node and the /drafts REST API —
# the tasks this module hands the solver are the same tasks those two read back
# off the draft, so the row-shape and coercion rules live in one place.
from app.services.draft_actions import (
    draft_id_of as _draft_id_of,
    to_task_chunk as _to_task_chunk,
)


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
    horizon_start: Optional[str]
    horizon_minutes: int
    retry_count: int
    clarification_request: Optional[str]
    error: Optional[str]
    progress_callback: Any
    progress_queue: Any
    # Live Supabase-backed store, re-supplied per turn from app.state — never
    # checkpointed (see _TRANSIENT_KEYS in app/orchestrator/checkpoint.py).
    draft_store: Any
    draft_id: Optional[str]



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
        elif isinstance(result, dict):
            # route_llm_call normalises this, but a dict must never reach the
            # str() branch again: `str({...})` is a Python repr with single
            # quotes and json.loads dies at char 1 (F1, live run 2026-08-08).
            graph = ExecutionGraph.model_validate(result)
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
    """Delegate to the reusable run_schedule.

    TMT deadline priority, the adaptive daily cap, hard/soft calendar blocks and
    the biological sleep fallback all live inside `run_schedule` — per
    .claude/rules/code-style.md it is imported and called, never reimplemented.
    """
    from pydantic import ValidationError
    from fastapi import HTTPException
    from app.api.v1.endpoints import schedule as schedule_ep
    from app.api.v1.endpoints.reasoning import ExecutionGraph, GoalMetadata
    from app.schemas.context import TimeSlot

    chunks = state.get("task_chunks", [])
    horizon = state.get("horizon_minutes", 2880)
    if not chunks:
        return {"error": "No tasks to schedule", "schedule": None}

    try:
        decomposition = [_to_task_chunk(c, i) for i, c in enumerate(chunks)]
        graph = ExecutionGraph(
            goal_metadata=GoalMetadata(),
            decomposition=decomposition,
            cognitive_load_estimate={"intrinsic_load": 0.5},
        )
    except (ValidationError, TypeError, ValueError, KeyError) as exc:
        logger.error(f"Could not build ExecutionGraph for scheduling: {exc}")
        return {"error": f"Invalid tasks for scheduling: {exc}", "schedule": None}

    # Fresh list: run_schedule appends the biological sleep fallback in place,
    # and PlanningState.time_slots is a reducer-managed list we must not mutate.
    daily_context: list[TimeSlot] = []
    for slot in state.get("time_slots", []):
        try:
            daily_context.append(
                slot if isinstance(slot, TimeSlot) else TimeSlot.model_validate(slot)
            )
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(f"Skipping unparseable time slot {slot}: {exc}")

    try:
        # horizon_start is left to run_schedule (Optional[datetime], never a
        # str) so its 8 AM-of-plan-date convention stays the single source of
        # truth; the resolved value comes back on the response.
        resp = schedule_ep.run_schedule(
            graph,
            daily_context,
            horizon_minutes=horizon,
        )
    except HTTPException as exc:
        if exc.status_code == 422:
            return {
                "schedule": None,
                "error": "INFEASIBLE",
                "_tool_detail": {"status": "INFEASIBLE", "horizon_h": horizon // 60},
            }
        raise

    payload = resp.model_dump(mode="json")
    return {
        "schedule": payload,
        # UI contract: wall time = horizon_start + timedelta(minutes=start_min).
        "horizon_start": payload.get("horizon_start") if isinstance(payload, dict) else None,
        "error": None,
        "_tool_detail": {
            "status": "OPTIMAL",
            "task_count": len(decomposition),
            "horizon_h": horizon // 60,
            "tmt_applied": True,
            "formula": "canonical_steel_konig",
        },
    }


async def create_draft(state: PlanningState) -> dict:
    """v1 parity: a feasible schedule is *proposed* as a draft, not committed.

    Nothing is written to ``user_tasks`` here — ``_persist_fused_tasks`` runs
    only when the user accepts. Failure is non-fatal by design: a draft id is a
    convenience for the review turn, and losing it must never cost the user the
    schedule the solver just spent seconds producing.
    """
    draft_store = state.get("draft_store")
    schedule = state.get("schedule")
    if not draft_store or not schedule:
        return {"draft_id": None}

    # draft_schedules.horizon_start is TEXT NOT NULL (20260329000002), so a
    # None here is a round-trip that can only fail. solve_schedule echoes the
    # value onto the state, but read it back off the payload too — that is
    # where it originates, and ScheduleResponse types it as required.
    horizon_start = state.get("horizon_start")
    if not horizon_start and isinstance(schedule, dict):
        horizon_start = schedule.get("horizon_start")
    if not horizon_start:
        logger.warning("Skipping draft creation: schedule carries no horizon_start")
        return {"draft_id": None}

    try:
        # DraftStore.create_draft is a sync Supabase HTTP round-trip. Called
        # bare it would stall the event loop for the whole insert — and
        # _wrap_step's asyncio.wait_for cannot interrupt a blocking call, so the
        # step's timeout would be advisory only. Same to_thread pattern as
        # UserModel.get_behavioral_constraints (user_model.py:48).
        row = await asyncio.to_thread(
            draft_store.create_draft,
            user_id=state.get("user_id", "demo"),
            tasks=state.get("task_chunks", []),
            horizon_start=horizon_start,
        )
        draft_id = _draft_id_of(row)
        return {"draft_id": draft_id, "_tool_detail": {"draft_id": draft_id}}
    except Exception as e:
        logger.warning(f"Draft creation failed (non-fatal): {e}")
        return {"draft_id": None}


# v1 parity ladder: 3d -> 5d -> 7d -> 14d -> 30d (MAX_HORIZON_MINUTES).
HORIZON_RETRY_SEQUENCE = [4320, 7200, 10080, 20160, 43200]


async def handle_infeasible(state: PlanningState) -> dict:
    retry_count = state.get("retry_count", 0)
    if retry_count < len(HORIZON_RETRY_SEQUENCE):
        return {
            "horizon_minutes": HORIZON_RETRY_SEQUENCE[retry_count],
            "retry_count": retry_count + 1,
            "error": None,
        }
    return {
        "error": "INFEASIBLE_EXHAUSTED",
        "clarification_request": (
            "I couldn't fit everything in even with a 30-day window. "
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
        "horizon_start": None,
        "horizon_minutes": 2880,
        "retry_count": 0,
        "clarification_request": None,
        "error": None,
        "progress_callback": state.get("progress_callback"),
        "progress_queue": state.get("progress_queue"),
        # Live store handed down from the parent turn (chat.py reads it off
        # app.state); None in degraded startup and in tests.
        "draft_store": state.get("draft_store"),
        "draft_id": None,
    }


def planning_state_out(result: dict, module_name: str) -> dict:
    schedule = result.get("schedule")
    task_chunks = result.get("task_chunks") or []
    clarification = result.get("clarification_request")
    error = result.get("error")

    # Defensive default: if the sub-graph produced NOTHING actionable AND no
    # error/clarification, emit a clarification so synthesis has a signal to
    # surface — prevents silent fallback to "Standing by, sir." on an empty plan.
    if not schedule and not task_chunks and not clarification and not error:
        clarification = (
            "I couldn't put a plan together from that, sir. "
            "Could you give me a bit more — the deadline, the subject, or what you're aiming at?"
        )

    draft_id = result.get("draft_id")

    out = {
        "schedule": schedule,
        "execution_graph": (
            {"decomposition": task_chunks} if task_chunks else None
        ),
        "clarification_request": clarification,
        "error": error,
        "draft_id": draft_id,
        # NOTE: anti_guilt_message and missed_deadlines are not yet computed in
        # the v2 sub-graph — placeholder for the upcoming port of
        # detect_and_mark_missed (currently lives in control_policy._run_plan_day_flow).
        "anti_guilt_message": result.get("anti_guilt_message"),
        "missed_deadlines": result.get("missed_deadlines"),
    }

    # A proposed draft is the whole point of the negotiation phase: the next
    # turn on this thread must take the shortcut in routing.py straight back to
    # planning instead of being re-classified as a fresh PLAN_DAY. The key is
    # set *only* when a draft exists — LangGraph merges node output by key, so
    # unconditionally returning NONE would reset a live review every time a
    # draft could not be created.
    if draft_id:
        from app.orchestrator.state import NegotiationPhase

        out["negotiation_state"] = NegotiationPhase.REVIEWING

    return out


planning_module = ModuleDefinition(
    name="planning_module",
    state_class=PlanningState,
    state_in=planning_state_in,
    state_out=planning_state_out,
    entry_step="validate_goal",
    steps=[
        # The gate runs FIRST, ahead of the fan-out it protects.
        #
        # It used to sit alongside translate_habits/memory_to_constraints and
        # route into decompose_goal, which cannot work: LangGraph's AND-join
        # (`add_edge([a, b], target)`) waits on node *completion* and ignores
        # its members' branches, so decompose_goal fired as soon as the two
        # constraint branches finished — including on the arm where the goal
        # was too vague to plan and validate_goal had routed to __END__. That
        # is a wasted 27B decomposition on a turn whose only output is a
        # clarification question. Gating up front also skips translate_habits,
        # a second 27B call, for the same reason.
        ModuleStep(name="validate_goal", handler=validate_goal, read_only=True,
                   routes_to={is_goal_clear: {True: "fetch_constraints", False: "__END__"}}),
        # No depends_on: reached only via validate_goal's True branch.
        ModuleStep(name="fetch_constraints", handler=fetch_constraints, concurrent_safe=True),
        ModuleStep(name="translate_habits", handler=translate_habits,
                   depends_on=["fetch_constraints"], timeout_ms=45_000),
        ModuleStep(name="expand_slots", handler=expand_slots,
                   depends_on=["translate_habits"], concurrent_safe=True, read_only=True),
        ModuleStep(name="memory_to_constraints", handler=memory_to_constraints,
                   depends_on=["fetch_constraints"], concurrent_safe=True,
                   feature_flag="ENABLE_PEARL"),
        # AND-join over the two constraint branches: both must land before the
        # decomposition runs, and it must run exactly once (two 27B calls in
        # one turn is an OOM risk on 24 GB).
        ModuleStep(name="decompose_goal", handler=decompose_goal,
                   depends_on=["expand_slots", "memory_to_constraints"],
                   timeout_ms=60_000),
        ModuleStep(name="fuse_tasks", handler=fuse_tasks, depends_on=["decompose_goal"]),
        ModuleStep(name="solve_schedule", handler=solve_schedule,
                   depends_on=["fuse_tasks"],
                   routes_to={check_feasibility: {"OPTIMAL": "create_draft", "INFEASIBLE": "handle_infeasible"}}),
        # No depends_on: reached only via solve_schedule's OPTIMAL branch, the
        # same way handle_infeasible is reached via INFEASIBLE.
        ModuleStep(name="create_draft", handler=create_draft),
        ModuleStep(name="handle_infeasible", handler=handle_infeasible,
                   routes_to={can_retry: {"retry": "solve_schedule", "exhausted": "__END__"}}),
    ],
)


def build_planning_graph():
    """Backward-compatible shim — delegates to build_module_graph(planning_module)."""
    from app.core.module_framework import build_module_graph
    return build_module_graph(planning_module)
