"""Control Policy: master orchestrator for the unified /chat endpoint."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, time, timezone
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.endpoints.reasoning import ExecutionGraph, SYSTEM_PROMPT, _sanitize_llm_json
from app.api.v1.endpoints.schedule import run_schedule
from app.core.config import (
    DAY_START_HOUR,
    DEFAULT_HORIZON_MINUTES,
    GEMINI_API_KEY,
    MAX_HORIZON_MINUTES,
    SLM_ROUTER_MODEL,
)
from app.models.brain.litellm_conf import hybrid_route_query, run_deep_research
from app.schemas.context import (
    Availability,
    BrainDumpExtraction,
    ChatResponse,
    IntentClassification,
    IntentType,
    TimeSlot,
)
from app.services.analytical.habit_translator import translate_habits_to_slots
from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots
from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
from app.services.extraction.behavioral_store import (
    get_behavioral_context_for_calendar,
    store_behavioral_constraint,
)
from app.services.extraction.action_item_handler import propose_action_item
from app.services.extraction.orchestrator import process_ingestion
from app.utils.deadline_parser import compute_horizon_from_deadlines

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

BRAIN_DUMP_EXTRACTION_PROMPT = (
    "You extract components from a user's brain-dump message. "
    "Parse the message and populate each field. Use null/empty for missing categories.\n\n"
    "planning_goal: Schedule tasks, break down goal, plan day. Clean goal string only (e.g. 'Plan my day to write 3 posts').\n"
    "inline_habits: Extract the EXACT, VERBATIM phrase for each long-term constraint. "
    "DO NOT summarize or shorten. CRITICAL: Preserve all time anchors (e.g. 'before 11 AM', "
    "'after 2 PM', 'no work until noon') — the scheduler needs these for math. "
    "Example: 'I hate mornings, never schedule work before 11 AM' -> "
    "[\"I hate working in the mornings, so please never schedule work before 11 AM\"] (full phrase, not \"I hate mornings\"). "
    "Return as a list of complete phrases. Character, not mood.\n"
    "state_updates: Temporary today-only mood (I'm tired, feeling sick, take it easy, go light on difficulty). Never store.\n"
    "action_items: Reminders, tasks to schedule later (call my mom, apply for internship).\n"
    "search_queries: Look up, search, latest on, current events (e.g. 'latest updates on SpaceX launch').\n"
    "has_calendar/calendar_text: Timetables, meeting schedules, class schedules.\n"
    "has_knowledge: Stub; PDF/syllabus refs.\n\n"
    "Return strictly valid JSON."
)

UNIFIED_CLASSIFICATION_PROMPT = (
    "You are the Jarvis Semantic Router. Classify the user's message into exactly one of: "
    "PLAN_DAY, CALENDAR_SYNC, KNOWLEDGE_INGESTION, BEHAVIORAL_CONSTRAINT, ACTION_ITEM.\n\n"
    "PLAN_DAY: User wants to plan their day, schedule tasks, or break down a goal. "
    "Examples: 'Plan my day to study SARIMAX', 'Schedule my coding tasks', "
    "'Break down my goal into tasks', 'I need to prepare for my exam'.\n"
    "CALENDAR_SYNC: Timetables, meeting schedules, board meetings, class schedules, "
    "flight times, deep-work blocks.\n"
    "KNOWLEDGE_INGESTION: Syllabi, DPP, sample papers, business plans, study materials, "
    "financial PDFs.\n"
    "BEHAVIORAL_CONSTRAINT: 'I sit in back bench', 'no meetings before 10', "
    "work preferences, habits, e.g. 'I hate mornings'.\n"
    "ACTION_ITEM: 'Apply for internship', 'prepare pitch', tasks with deadlines, "
    "direct goals to be scheduled.\n\n"
    "If multiple intents apply, choose the dominant one. Return strictly valid JSON."
)

INLINE_HABIT_EXTRACTION_PROMPT = (
    "You are a VERBATIM extractor. Extract the user's behavioral habit or constraint "
    "using their EXACT words. DO NOT paraphrase, summarize, or shorten. "
    "CRITICAL: If the user mentions a time (e.g. 'before 11 AM', 'after noon', 'until 2 PM'), "
    "you MUST include it in the extracted phrase. The scheduler needs these anchors. "
    "Examples: 'I hate mornings and never want work before 11 AM' -> extract the full sentence. "
    "'No meetings before 10' -> extract exactly 'No meetings before 10'. "
    "If there are no general habits, return exactly 'NONE'. Return only the habit phrase(s), no preamble."
)


async def _extract_and_save_inline_habits(
    text: str, user_id: str, supabase_client: Any
) -> None:
    """Extract and save long-term habits from text. Used in fallback PLAN_DAY path."""
    if not supabase_client:
        return
    try:
        extracted = await hybrid_route_query(
            user_prompt=text,
            system_prompt=INLINE_HABIT_EXTRACTION_PROMPT,
            model_override=SLM_ROUTER_MODEL,
        )
        if not extracted:
            return
        raw = extracted if isinstance(extracted, str) else str(extracted)
        raw = raw.strip()
        if "NONE" in raw.upper() or len(raw) <= 5:
            return
        await store_behavioral_constraint(
            raw_text=raw,
            user_id=user_id,
            supabase_client=supabase_client,
        )
        print(f"[Memory] Inline habit saved: {raw}")
    except Exception as e:
        print(f"[Memory] Inline extraction failed: {e}")


def _persist_decomposition_to_user_tasks(
    user_id: Optional[str],
    graph: ExecutionGraph,
    supabase_client: Any,
) -> None:
    """Persist task decomposition to user_tasks for task-material linking."""
    if not supabase_client or not user_id:
        return
    try:
        plan_id = str(uuid.uuid4())
        for chunk in graph.decomposition:
            supabase_client.table("user_tasks").insert(
                {
                    "user_id": user_id,
                    "plan_id": plan_id,
                    "task_id": chunk.task_id,
                    "title": chunk.title,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
    except Exception as e:
        print(f"[Control Policy] Persist user_tasks failed (table may not exist): {e}")


INGESTION_MESSAGES = {
    IntentType.CALENDAR_SYNC: "Extracted your timetable. Review pending calendar updates to approve.",
    IntentType.KNOWLEDGE_INGESTION: "Saved your materials to knowledge base.",
    IntentType.BEHAVIORAL_CONSTRAINT: (
        "Got it, I've noted your preference. Your schedule constraints have been updated."
    ),
    IntentType.ACTION_ITEM: "Recorded your action item. You can schedule it when ready.",
}


def _is_extraction_empty(ext: BrainDumpExtraction) -> bool:
    """True if extraction has no actionable components."""
    return (
        not ext.planning_goal
        and not ext.inline_habits
        and not ext.state_updates
        and not ext.action_items
        and not ext.search_queries
        and not ext.has_calendar
        and not ext.has_knowledge
    )


async def _run_brain_dump_extraction(user_prompt: str) -> Optional[BrainDumpExtraction]:
    """Extract all components from brain-dump prompt. Returns None on failure."""
    try:
        result = await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=BRAIN_DUMP_EXTRACTION_PROMPT,
            response_schema=BrainDumpExtraction,
            model_override=SLM_ROUTER_MODEL,
        )
        if isinstance(result, dict):
            return BrainDumpExtraction.model_validate(result)
        return BrainDumpExtraction.model_validate_json(result)
    except Exception as e:
        print(f"[Brain Dump] Extraction failed: {e}")
        return None


async def _fallback_single_intent(
    user_prompt: str,
    user_id: str,
    db_client: Any,
    day_start_hour_override: Optional[int] = None,
) -> ChatResponse:
    """Fallback: use single-intent classifier when extraction fails or is empty."""
    classify_result = await hybrid_route_query(
        user_prompt=user_prompt,
        system_prompt=UNIFIED_CLASSIFICATION_PROMPT,
        response_schema=IntentClassification,
        model_override=SLM_ROUTER_MODEL,
    )
    if isinstance(classify_result, dict):
        classification = IntentClassification.model_validate(classify_result)
    else:
        classification = IntentClassification.model_validate_json(classify_result)

    intent = classification.intent

    if intent in (
        IntentType.CALENDAR_SYNC,
        IntentType.KNOWLEDGE_INGESTION,
        IntentType.BEHAVIORAL_CONSTRAINT,
        IntentType.ACTION_ITEM,
    ):
        result = await process_ingestion(
            payload=user_prompt,
            user_id=user_id,
            db_client=db_client,
            intent_override=intent,
        )
        execution_summary: dict[str, Any] = {}
        if result.calendar_result:
            execution_summary["calendar_extracted"] = True
            execution_summary["needs_end_date"] = getattr(
                result.calendar_result, "needs_end_date", False
            )
        if result.action_proposal:
            execution_summary["action_proposal"] = result.action_proposal.model_dump()
        if intent == IntentType.KNOWLEDGE_INGESTION and result.knowledge_result:
            execution_summary["knowledge_stored"] = True
        if execution_summary:
            message, thinking_process = await synthesize_jarvis_response(execution_summary)
        else:
            message = INGESTION_MESSAGES.get(intent, "Saved.")
            thinking_process = None
        suggested = "replan" if intent == IntentType.BEHAVIORAL_CONSTRAINT else None
        return ChatResponse(
            intent=intent.value,
            message=message,
            ingestion_result=result.model_dump(),
            suggested_action=suggested,
            thinking_process=thinking_process,
        )

    # PLAN_DAY fallback: run legacy plan-day flow
    return await _run_plan_day_flow(
        user_prompt=user_prompt,
        user_id=user_id,
        db_client=db_client,
        planning_goal=user_prompt,
        state_updates=None,
        use_voice_synthesis=False,
        day_start_hour_override=day_start_hour_override,
    )


async def _run_plan_day_flow(
    user_prompt: str,
    user_id: str,
    db_client: Any,
    planning_goal: str,
    state_updates: Optional[list[str]] = None,
    use_voice_synthesis: bool = True,
    execution_summary: Optional[dict] = None,
    action_proposals: Optional[list[dict]] = None,
    search_task: Optional[asyncio.Task] = None,
    day_start_hour_override: Optional[int] = None,
) -> ChatResponse:
    """Run PLAN_DAY pipeline: save habits, fetch habits, translate, decompose, schedule."""
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    # Inline habit extraction (for fallback path; main path saves from extraction)
    await _extract_and_save_inline_habits(planning_goal, user_id, supabase)

    habits = await get_behavioral_context_for_calendar(user_id, supabase)
    if state_updates:
        habits = habits + "\n\n[Temporary, today only — do not store]: " + "; ".join(state_updates)

    if not habits or not habits.strip():
        semantic_slots = []
    else:
        semantic_slots = await translate_habits_to_slots(habits)

    plan_start = datetime.now(timezone.utc)
    resolved_day_start = day_start_hour_override or DAY_START_HOUR
    plan_date = plan_start.date()
    # Logical Day Fix: If it's 1 AM, we are still in "yesterday's" schedule window
    if plan_start.hour < resolved_day_start:
        plan_date -= timedelta(days=1)
    horizon_start = datetime.combine(plan_date, time(resolved_day_start, 0), tzinfo=timezone.utc)
    past_minutes = max(0, int((plan_start - horizon_start).total_seconds() / 60))

    def _build_daily_context(horizon_minutes: int) -> list:
        ctx = expand_semantic_slots_to_time_slots(
            semantic_slots,
            horizon_minutes=horizon_minutes,
            plan_start=plan_start,
        )
        if past_minutes > 0:
            past_slot = TimeSlot(
                name="past",
                start_min=0,
                end_min=past_minutes,
                availability=Availability.BLOCKED,
                recurring=False,
            )
            ctx.insert(0, past_slot)
        return ctx

    async def _call_decompose(force_cloud: bool = False) -> dict:
        result = await hybrid_route_query(
            user_prompt=planning_goal,
            system_prompt=SYSTEM_PROMPT,
            response_schema=ExecutionGraph,
            force_cloud=force_cloud,
            lenient_validation=True,
        )
        if isinstance(result, dict):
            return result
        sanitized = _sanitize_llm_json(result)
        return json.loads(sanitized)

    try:
        data = await _call_decompose(force_cloud=False)
        if len(data.get("decomposition", [])) < 5 and GEMINI_API_KEY:
            data = await _call_decompose(force_cloud=True)
        graph = ExecutionGraph(**data)
        if         len(graph.decomposition) < 5:
            return ChatResponse(
                intent=IntentType.PLAN_DAY.value,
                message=(
                    "I struggled to break that goal down. "
                    "Could you clarify what exactly you want to achieve?"
                ),
                schedule=None,
                execution_graph=None,
                thinking_process=None,
            )
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return ChatResponse(
            intent=IntentType.PLAN_DAY.value,
            message=(
                "I struggled to break that goal down. "
                "Could you clarify what exactly you want to achieve?"
            ),
            schedule=None,
            execution_graph=None,
            thinking_process=None,
        )

    inferred_horizon = compute_horizon_from_deadlines(graph, plan_start)
    base_steps = [DEFAULT_HORIZON_MINUTES, 4320, 7200]
    if inferred_horizon is not None and inferred_horizon > DEFAULT_HORIZON_MINUTES:
        horizon_steps = sorted(
            h for h in set(base_steps + [inferred_horizon]) if h <= MAX_HORIZON_MINUTES
        )
    else:
        extended_steps = [2880, 4320, 7200, 10080, 20160, 43200]
        horizon_steps = [h for h in extended_steps if h <= MAX_HORIZON_MINUTES]

    schedule_response = None
    used_horizon_minutes = DEFAULT_HORIZON_MINUTES

    for horizon_min in horizon_steps:
        if horizon_min > MAX_HORIZON_MINUTES:
            break
        daily_context = _build_daily_context(horizon_min)
        try:
            schedule_response = run_schedule(
                graph,
                daily_context,
                horizon_minutes=horizon_min,
                horizon_start=horizon_start,
            )
            used_horizon_minutes = horizon_min
            break
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            continue

    if schedule_response is not None:
        summary = execution_summary or {}
        summary["schedule_generated"] = True
        if used_horizon_minutes > DEFAULT_HORIZON_MINUTES:
            summary["spread_across_days"] = True

        _persist_decomposition_to_user_tasks(user_id, graph, supabase)

        search_result: Optional[dict] = None
        if search_task is not None:
            try:
                search_result = await search_task
                summary["search_done"] = search_result.get("queries", [])
            except Exception as e:
                print(f"[Deep Research] Task failed: {e}")
                search_result = {"queries": [], "summaries": []}

        if use_voice_synthesis:
            message, thinking_process = await synthesize_jarvis_response(summary)
        else:
            message, thinking_process = "Here's your schedule.", None
        return ChatResponse(
            intent=IntentType.PLAN_DAY.value,
            message=message,
            schedule=schedule_response.model_dump(),
            execution_graph=graph.model_dump(),
            action_proposals=action_proposals,
            search_result=search_result,
            suggested_action="replan" if summary.get("habits_saved") else None,
            thinking_process=thinking_process,
        )

    return ChatResponse(
        intent=IntentType.PLAN_DAY.value,
        message=(
            "This schedule is mathematically impossible to fit into your day, "
            "especially considering your personal constraints. Try reducing the scope "
            "of your tasks or temporarily relaxing a habit."
        ),
        schedule=None,
        execution_graph=graph.model_dump(),
        suggested_action="replan",
        thinking_process=None,
    )


async def execute_agentic_flow(
    user_prompt: str,
    user_id: str,
    db_client: Any,
    day_start_hour_override: Optional[int] = None,
) -> ChatResponse:
    """Master orchestrator: brain dump extraction, multi-execution, Voice of Jarvis."""
    # Step 1: Brain dump extraction
    extraction = await _run_brain_dump_extraction(user_prompt)
    if extraction is None or _is_extraction_empty(extraction):
        return await _fallback_single_intent(
            user_prompt,
            user_id,
            db_client,
            day_start_hour_override=day_start_hour_override,
        )

    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    execution_summary: dict[str, Any] = {}
    action_proposals: list[dict] = []
    search_result: Optional[dict] = None
    ingestion_result: Optional[dict] = None

    # Step 2: Spawn search task immediately (runs in parallel)
    search_task: Optional[asyncio.Task] = None
    if extraction.search_queries:
        search_task = asyncio.create_task(run_deep_research(extraction.search_queries))

    # Step 3: Habits (persistent)
    if extraction.inline_habits:
        for h in extraction.inline_habits:
            if h and h.strip():
                await store_behavioral_constraint(
                    raw_text=h.strip(),
                    user_id=user_id,
                    supabase_client=supabase,
                )
                print(f"[Memory] Inline habit saved: {h.strip()}")
        execution_summary["habits_saved"] = extraction.inline_habits

    # Step 4: State updates (transient, logic injection happens in plan flow)
    if extraction.state_updates:
        execution_summary["state_applied"] = extraction.state_updates

    # Step 5: Action items
    if extraction.action_items:
        for item in extraction.action_items:
            if item and item.strip():
                try:
                    prop = await propose_action_item(item.strip())
                    action_proposals.append(prop.model_dump())
                except Exception as e:
                    print(f"[Action Item] Failed for '{item}': {e}")
        execution_summary["action_proposals"] = action_proposals

    # Step 6: Calendar
    if extraction.has_calendar and extraction.calendar_text:
        try:
            result = await process_ingestion(
                payload=extraction.calendar_text,
                user_id=user_id,
                db_client=db_client,
                intent_override=IntentType.CALENDAR_SYNC,
            )
            ingestion_result = result.model_dump()
            execution_summary["calendar_extracted"] = True
            if result.calendar_result and result.calendar_result.needs_end_date:
                execution_summary["needs_end_date"] = True
        except Exception as e:
            print(f"[Calendar] Extraction failed: {e}")

    # Step 7: Planning (if planning_goal) — search_task runs in parallel, awaited inside
    if extraction.planning_goal:
        return await _run_plan_day_flow(
            user_prompt=user_prompt,
            user_id=user_id,
            db_client=db_client,
            planning_goal=extraction.planning_goal,
            state_updates=extraction.state_updates or None,
            use_voice_synthesis=True,
            execution_summary=execution_summary,
            action_proposals=action_proposals if action_proposals else None,
            search_task=search_task,
            day_start_hour_override=day_start_hour_override,
        )

    # Step 8: Await search task for ingestion-only path
    if search_task is not None:
        try:
            search_result = await search_task
            execution_summary["search_done"] = search_result.get("queries", [])
        except Exception as e:
            print(f"[Deep Research] Task failed: {e}")
            search_result = {"queries": extraction.search_queries, "summaries": []}

    # Step 9: No planning goal — ingestion-only response with Voice of Jarvis
    message, thinking_process = await synthesize_jarvis_response(execution_summary)

    # Determine primary intent for response
    if execution_summary.get("habits_saved"):
        intent = IntentType.BEHAVIORAL_CONSTRAINT.value
        suggested = "replan"
    elif action_proposals:
        intent = IntentType.ACTION_ITEM.value
        suggested = None
    elif ingestion_result:
        intent = IntentType.CALENDAR_SYNC.value
        suggested = None
    else:
        intent = "MULTI"
        suggested = None

    return ChatResponse(
        intent=intent,
        message=message,
        ingestion_result=ingestion_result,
        action_proposals=action_proposals if action_proposals else None,
        search_result=search_result,
        suggested_action=suggested,
        thinking_process=thinking_process,
    )
