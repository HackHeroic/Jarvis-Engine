"""Control Policy: master orchestrator for the unified /chat endpoint."""

import asyncio
import hashlib
import json
import time as time_mod
import uuid
from datetime import datetime, timedelta, time, timezone
from typing import Any, Callable, Optional

# Type alias for progress callback: async fn(phase_name, detail_dict)
ProgressCallback = Optional[Callable[..., Any]]

from fastapi import HTTPException

from app.core.jarvis_logger import log_step
from pydantic import ValidationError

from app.api.v1.endpoints.reasoning import ExecutionGraph, TaskChunk, SYSTEM_PROMPT, _sanitize_llm_json
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
from app.services.analytical.task_retrieval import get_all_pending_tasks
from app.services.analytical.voice_of_jarvis import (
    _build_thinking_fallback,
    synthesize_jarvis_response,
)
from app.services.extraction.behavioral_store import (
    get_behavioral_context_for_calendar,
)
from app.services.extraction.action_item_handler import propose_action_item
from app.services.extraction.orchestrator import process_ingestion
from app.utils.deadline_parser import compute_horizon_from_deadlines

# ---------------------------------------------------------------------------
# Decomposition cache — avoids redundant 27B calls for unchanged goals
# Key: SHA-256 of enriched planning goal. Value: (timestamp, dict).
# ---------------------------------------------------------------------------
_decompose_cache: dict[str, tuple[float, dict]] = {}
_DECOMPOSE_CACHE_TTL_S = 3600  # 1 hour

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

BRAIN_DUMP_EXTRACTION_PROMPT = (
    "You extract components from a user's brain-dump message. "
    "Parse the message and populate each field. Use null/empty for missing categories.\n\n"
    "planning_goal: Schedule tasks, break down goal, plan day. Clean goal string only (e.g. 'Plan my day to write 3 posts'). "
    "Use null for greetings, chitchat, questions, or very short non-goals (e.g. 'hi', 'hello', 'what is X', 'teach me X'). "
    "Only extract when the user clearly wants to plan or schedule.\n"
    "inline_habits: Extract the EXACT, VERBATIM phrase for each long-term constraint. "
    "DO NOT summarize or shorten. CRITICAL: Preserve all time anchors (e.g. 'before 11 AM', "
    "'after 2 PM', 'no work until noon') — the scheduler needs these for math. "
    "Example: 'I hate mornings, never schedule work before 11 AM' -> "
    "[\"I hate working in the mornings, so please never schedule work before 11 AM\"] (full phrase, not \"I hate mornings\"). "
    "Return as a list of complete phrases. Character, not mood.\n"
    "state_updates: Temporary today-only mood (I'm tired, feeling sick, take it easy, go light on difficulty). Never store.\n"
    "action_items: Reminders, tasks to create or schedule later. "
    "Examples: 'call my mom', 'apply for internship', 'create a task to study X', "
    "'add a task for Y', 'make a task to do Z', 'remind me to do X'.\n"
    "search_queries: Look up, search, latest on, current events (e.g. 'latest updates on SpaceX launch').\n"
    "has_calendar/calendar_text: Timetables, meeting schedules, class schedules.\n"
    "has_knowledge: ONLY set true when user uploads or references a specific DOCUMENT "
    "(PDF, syllabus, sample paper, study material file). "
    "NEVER set true for questions like 'teach me X', 'explain X', 'what is X' — those are questions, NOT documents.\n"
    "deadline_update: If the user mentions a deadline, due date, or exam date, extract it as ISO-8601 (YYYY-MM-DD). "
    "E.g. 'exam on March 20' -> '2026-03-20'. Use null if none.\n\n"
    "When the message contains extracted document text (long text from a PDF/file), "
    "set has_knowledge=true. If it looks like a timetable, set has_calendar=true instead.\n\n"
    "Return strictly valid JSON."
)

UNIFIED_CLASSIFICATION_PROMPT = (
    "You are the Jarvis Semantic Router. Classify the user's message into exactly one of: "
    "GREETING, GENERAL_QA, PLAN_DAY, CALENDAR_SYNC, KNOWLEDGE_INGESTION, BEHAVIORAL_CONSTRAINT, ACTION_ITEM.\n\n"
    "GREETING: ONLY short greetings with no question or substance. Examples: 'hi', 'hello', 'hey', "
    "'how are you', 'what's up', 'good morning', 'yo', 'sup'. ONLY use when the message is literally "
    "just a greeting with no question, topic, or request.\n"
    "GENERAL_QA: Questions, explanations, knowledge queries, conceptual discussions, 'teach me X'. "
    "Examples: 'What is Dijkstra algorithm?', 'Explain binary search', 'How does TCP work?', "
    "'Tell me about machine learning', 'What are design patterns?', 'How to solve this problem', "
    "'Can you teach me sieve of eratosthenes?', 'What is quicksort?'. "
    "Use when the user is asking a question or seeking an explanation — NOT planning or scheduling.\n"
    "PLAN_DAY: User wants to plan their day, schedule tasks, break down a goal, or study something on a schedule. "
    "Examples: 'Plan my day to study SARIMAX', 'Schedule my coding tasks', "
    "'Break down my goal into tasks', 'I need to prepare for my exam', "
    "'I want to study algorithms today', 'help me learn calculus this week'.\n"
    "CALENDAR_SYNC: Timetables, meeting schedules, board meetings, class schedules, "
    "flight times, deep-work blocks.\n"
    "KNOWLEDGE_INGESTION: ONLY for document uploads — syllabi, DPP, sample papers, PDFs, study material files. "
    "NOT for questions about topics.\n"
    "BEHAVIORAL_CONSTRAINT: 'I sit in back bench', 'no meetings before 10', "
    "work preferences, habits, e.g. 'I hate mornings'.\n"
    "ACTION_ITEM: 'Apply for internship', 'prepare pitch', 'create a task to study X', "
    "'add a task for Y', 'make a task to do Z', 'remind me to X', tasks with deadlines, "
    "direct goals to be created or scheduled.\n\n"
    "IMPORTANT: If the user asks a question about any topic (algorithm, concept, how-to, 'teach me'), "
    "classify as GENERAL_QA, NOT KNOWLEDGE_INGESTION or GREETING.\n"
    "KNOWLEDGE_INGESTION is ONLY for uploaded documents/files, never for questions.\n\n"
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


async def _extract_and_stage_inline_habits(
    text: str, user_id: str,
) -> list[dict]:
    """Extract and stage habits from text. Returns staged habit dicts."""
    from app.services.extraction.behavioral_store import stage_habit
    try:
        extracted = await hybrid_route_query(
            user_prompt=text,
            system_prompt=INLINE_HABIT_EXTRACTION_PROMPT,
            model_override=SLM_ROUTER_MODEL,
        )
        if not extracted:
            return []
        raw = extracted if isinstance(extracted, str) else str(extracted)
        raw = raw.strip()
        if "NONE" in raw.upper() or len(raw) <= 5:
            return []
        return [stage_habit(raw, user_id)]
    except Exception as e:
        print(f"[Memory] Inline extraction failed: {e}")
        return []


def _build_planning_context(
    user_id: str,
    planning_goal: str,
    supabase_client: Any,
) -> str:
    """Enrich planning_goal with deadline context from user_plan_updates."""
    if not supabase_client or not user_id:
        return planning_goal
    try:
        result = (
            supabase_client.table("user_plan_updates")
            .select("deadline_date, deadline_raw, context_snippet")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        rows = result.data or []
        goal_words = set(planning_goal.lower().split())
        for r in rows:
            snippet = (r.get("context_snippet") or "").lower()
            if not snippet or not goal_words:
                continue
            snippet_words = set(snippet.split())
            if goal_words & snippet_words:
                deadline_date = r.get("deadline_date")
                if deadline_date:
                    return f"[Context: Known deadline for this goal: {deadline_date}.] {planning_goal}"
                break
        for r in rows:
            deadline_date = r.get("deadline_date")
            if deadline_date:
                return f"[Context: Known deadline: {deadline_date}.] {planning_goal}"
    except Exception as e:
        print(f"[Control Policy] _build_planning_context failed: {e}")
    return planning_goal


def _get_plan_deadlines_from_db(
    user_id: str,
    goal_id: Optional[str],
    supabase_client: Any,
) -> list[str]:
    """Fetch deadline strings from user_plan_updates for horizon computation."""
    if not supabase_client or not user_id:
        return []
    try:
        result = (
            supabase_client.table("user_plan_updates")
            .select("deadline_date, deadline_raw, goal_id")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        rows = result.data or []
        out: list[str] = []
        seen: set[str] = set()
        for r in rows:
            row_goal = r.get("goal_id")
            if goal_id and row_goal is not None and row_goal != goal_id:
                continue
            for key in ("deadline_date", "deadline_raw"):
                val = r.get(key)
                if val and isinstance(val, str) and val not in seen:
                    out.append(val)
                    seen.add(val)
        return out
    except Exception as e:
        print(f"[Control Policy] Query user_plan_updates failed: {e}")
        return []


def _infer_goal_id_from_task_id(task_id: str) -> Optional[str]:
    """Extract goal_id from prefixed task_id (e.g. thesis_task_1 -> thesis)."""
    if not task_id or "_" not in task_id:
        return None
    return task_id.split("_", 1)[0]


def _namespace_chunk(chunk: TaskChunk, goal_id: str) -> TaskChunk:
    """Prefix task_id and dependencies with goal_id for multi-goal fusion.

    CRITICAL: dependencies must use prefixed refs so the OR-Tools solver's
    build_dependencies() receives consistent IDs for precedence constraints.
    """
    prefixed_id = f"{goal_id}_{chunk.task_id}"
    prefixed_deps = [f"{goal_id}_{d}" for d in chunk.dependencies]
    return TaskChunk(
        task_id=prefixed_id,
        title=chunk.title,
        duration_minutes=chunk.duration_minutes,
        difficulty_weight=chunk.difficulty_weight,
        dependencies=prefixed_deps,
        completion_criteria=chunk.completion_criteria,
        implementation_intention=chunk.implementation_intention,
        deadline_hint=chunk.deadline_hint,
    )


def _validate_master_chunk_dependencies(chunks: list[TaskChunk]) -> None:
    """Validate master_chunk_list before run_schedule.

    - Every dep in c.dependencies exists as d.task_id for some d in chunks.
    - No task_id appears twice (prefixed IDs unique across goals).
    """
    task_ids = {c.task_id for c in chunks}
    if len(task_ids) != len(chunks):
        seen: set[str] = set()
        for c in chunks:
            if c.task_id in seen:
                raise ValueError(f"Duplicate task_id in master_chunk_list: {c.task_id}")
            seen.add(c.task_id)

    for c in chunks:
        for dep in c.dependencies:
            if dep not in task_ids:
                raise ValueError(
                    f"Dependency reference invalid: chunk {c.task_id} depends on {dep!r} "
                    f"which is not in master_chunk_list"
                )


def _persist_fused_tasks(
    user_id: str,
    chunks: list[TaskChunk],
    supabase_client: Any,
) -> None:
    """Replace all pending user_tasks with the fused master chunk list.

    Deletes existing pending rows, then inserts fresh rows for each chunk.
    Chunks must have prefixed task_ids (goal_id_orig_id). Full TaskChunk fields
    are persisted for retrieval by get_all_pending_tasks.

    Safety: will NOT delete if chunks list is empty (prevents accidental wipe
    when task retrieval fails upstream).
    """
    if not supabase_client or not user_id:
        return
    if not chunks:
        print("[Control Policy] _persist_fused_tasks: empty chunks list, skipping to prevent data loss")
        return
    try:
        plan_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        rows: list[dict[str, Any]] = []

        for chunk in chunks:
            goal_id = _infer_goal_id_from_task_id(chunk.task_id)
            row = {
                "user_id": user_id,
                "plan_id": plan_id,
                "task_id": chunk.task_id,
                "title": chunk.title,
                "status": "pending",
                "duration_minutes": chunk.duration_minutes,
                "difficulty_weight": chunk.difficulty_weight,
                "dependencies": chunk.dependencies,
                "deadline_hint": chunk.deadline_hint,
                "created_at": now_iso,
            }
            if goal_id:
                row["goal_id"] = goal_id
            rows.append(row)

        # Delete AFTER building rows — if row construction fails, existing tasks survive
        supabase_client.table("user_tasks").delete().eq(
            "user_id", user_id
        ).eq("status", "pending").execute()

        if rows:
            supabase_client.table("user_tasks").insert(rows).execute()
            print(f"[Control Policy] Persisted {len(rows)} fused tasks for user {user_id}")
    except Exception as e:
        print(f"[Control Policy] Persist fused user_tasks failed: {e}")


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
        and not ext.deadline_update
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


async def _direct_qa_response(
    user_prompt: str,
    model_override: str | None = None,
    progress_callback: ProgressCallback = None,
) -> ChatResponse:
    """Answer a question directly using the specified model (default: 27B).

    Used for GENERAL_QA intent and 27B direct mode. No pipeline routing.
    """
    from app.core.config import LOCAL_LLM_MODEL
    model = model_override or LOCAL_LLM_MODEL

    if progress_callback:
        await progress_callback("synthesizing", {
            "model": model,
            "phase_summary": "Answering your question...",
        })

    try:
        result = await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=(
                "You are Jarvis, an intelligent AI assistant. Answer the user's question clearly and helpfully. "
                "If the question is about a concept, algorithm, or topic, explain it well with examples. "
                "Be thorough but concise."
            ),
            response_schema=None,
            model_override=model,
        )
        msg = result if isinstance(result, str) else str(result)
        msg = msg.strip() if msg else "I couldn't generate a response."

        # Extract thinking if present
        from app.services.analytical.voice_of_jarvis import _extract_thinking_process
        message, thinking_process = _extract_thinking_process(msg)

        return ChatResponse(
            intent=IntentType.GENERAL_QA.value,
            message=message,
            thinking_process=thinking_process,
        )
    except Exception as e:
        print(f"[Direct QA] Failed: {e}")
        return ChatResponse(
            intent=IntentType.GENERAL_QA.value,
            message=f"I encountered an error answering your question: {e}",
        )


async def _fallback_single_intent(
    user_prompt: str,
    user_id: str,
    db_client: Any,
    day_start_hour_override: Optional[int] = None,
    max_daily_deep_work_minutes: Optional[int] = None,
    min_daily_deep_work_minutes: Optional[int] = None,
    max_task_duration_minutes: Optional[int] = None,
    min_task_duration_minutes: Optional[int] = None,
    progress_callback: ProgressCallback = None,
) -> ChatResponse:
    """Fallback: use single-intent classifier when extraction fails or is empty."""
    try:
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
    except (ValidationError, json.JSONDecodeError) as e:
        print(f"[Fallback] Intent classification parse error: {e}")
        return await _direct_qa_response(
            user_prompt, progress_callback=progress_callback
        )
    except Exception as e:
        print(f"[Fallback] Intent classification failed (model unavailable?): {e}")
        return ChatResponse(
            intent=IntentType.GENERAL_QA.value,
            message="I'm having trouble processing your request right now. Please try again in a moment.",
        )

    intent = classification.intent

    if progress_callback:
        await progress_callback("intent_classified", {"intent": intent.value, "fallback": True})

    if intent == IntentType.GREETING:
        message, thinking_process = await synthesize_jarvis_response({"greeting": True})
        return ChatResponse(
            intent=IntentType.GREETING.value,
            message=message,
            thinking_process=thinking_process,
        )

    # GENERAL_QA: answer the question directly using 27B
    if intent == IntentType.GENERAL_QA:
        return await _direct_qa_response(user_prompt, progress_callback=progress_callback)

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
            thinking_process = "I processed your input and saved it to your profile."
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
        max_daily_deep_work_minutes=max_daily_deep_work_minutes,
        min_daily_deep_work_minutes=min_daily_deep_work_minutes,
        max_task_duration_minutes=max_task_duration_minutes,
        min_task_duration_minutes=min_task_duration_minutes,
        progress_callback=progress_callback,
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
    deadline_override: Optional[str] = None,
    max_daily_deep_work_minutes: Optional[int] = None,
    min_daily_deep_work_minutes: Optional[int] = None,
    max_task_duration_minutes: Optional[int] = None,
    min_task_duration_minutes: Optional[int] = None,
    progress_callback: ProgressCallback = None,
    skip_scheduling: bool = False,
    inline_habits_already_saved: bool = False,
    draft: Optional[Any] = None,
    draft_store: Optional[Any] = None,
) -> ChatResponse:
    """Run PLAN_DAY pipeline: save habits, fetch habits, translate, decompose, schedule."""
    from app.core.config import LOCAL_LLM_MODEL as _LLM_27B
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    log_step("P1_HABITS_FETCH", "Fetching behavioral context for calendar")
    # Inline habit extraction (skip if primary path already staged from extraction)
    staged_from_extraction: list[dict] = []
    if not inline_habits_already_saved:
        staged_from_extraction = await _extract_and_stage_inline_habits(planning_goal, user_id)
    if execution_summary is None:
        execution_summary = {}
    if staged_from_extraction:
        existing = execution_summary.get("habits_staged", [])
        execution_summary["habits_staged"] = existing + staged_from_extraction

    habits = await get_behavioral_context_for_calendar(user_id, supabase)
    log_step("P2_HABITS_LOADED", f"Loaded habits: {len(habits)} chars")
    if progress_callback:
        await progress_callback("habits_fetched", {"chars": len(habits), "has_habits": bool(habits.strip())})
    if state_updates:
        habits = habits + "\n\n[Temporary, today only — do not store]: " + "; ".join(state_updates)

    if not habits or not habits.strip():
        semantic_slots = []
        log_step("P3_TRANSLATE", "No habits to translate")
    else:
        _translate_start = time_mod.monotonic()
        semantic_slots = await translate_habits_to_slots(habits)
        _translate_dur = int((time_mod.monotonic() - _translate_start) * 1000)
        log_step("P3_TRANSLATE", "Translated habits to slots", {"slots": len(semantic_slots)})
        if progress_callback:
            await progress_callback("habits_translated", {
                "slot_count": len(semantic_slots),
                "model": _LLM_27B,
                "duration_ms": _translate_dur,
                "phase_summary": f"Converted {len(habits.splitlines())} constraints into {len(semantic_slots)} scheduling slots",
            })

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

    enriched_planning_goal = _build_planning_context(user_id, planning_goal, supabase)
    if max_task_duration_minutes or min_task_duration_minutes:
        parts = []
        if min_task_duration_minutes:
            parts.append(f"at least {min_task_duration_minutes} min")
        if max_task_duration_minutes:
            parts.append(f"at most {max_task_duration_minutes} min")
        constraint = "[Constraint: Each task must be " + " and ".join(parts) + ".] "
        enriched_planning_goal = constraint + enriched_planning_goal

    async def _call_decompose(force_cloud: bool = False) -> dict:
        result = await hybrid_route_query(
            user_prompt=enriched_planning_goal,
            system_prompt=SYSTEM_PROMPT,
            response_schema=ExecutionGraph,
            force_cloud=force_cloud,
            lenient_validation=True,
        )
        if isinstance(result, dict):
            return result
        sanitized = _sanitize_llm_json(result)
        return json.loads(sanitized)

    _decompose_start = time_mod.monotonic()

    # --- Decomposition cache check ---
    _decompose_cache_key = hashlib.sha256(enriched_planning_goal.encode()).hexdigest()
    _cached_decomp = _decompose_cache.get(_decompose_cache_key)
    if _cached_decomp and (time_mod.time() - _cached_decomp[0]) < _DECOMPOSE_CACHE_TTL_S:
        data = _cached_decomp[1]
        log_step("P4_DECOMPOSE", "Cache HIT — skipping 27B decomposition")
        if progress_callback:
            await progress_callback("decomposing", {
                "goal": enriched_planning_goal[:120],
                "model": "cache",
                "started_at_ms": int(time_mod.time() * 1000),
                "phase_summary": "Decomposition cache hit — reusing previous result",
            })
    else:
        log_step("P4_DECOMPOSE", "Breaking goal into micro-tasks (local 27B)")
        if progress_callback:
            await progress_callback("decomposing", {
                "goal": enriched_planning_goal[:120],
                "model": _LLM_27B,
                "started_at_ms": int(time_mod.time() * 1000),
            })
        data = None

    try:
        if data is None:
            data = await _call_decompose(force_cloud=False)
        num_tasks = len(data.get("decomposition", []))
        log_step("P4_DECOMPOSE_RESULT", "Decomposition complete", {"tasks": num_tasks})
        if num_tasks < 5 and GEMINI_API_KEY:
            log_step("P4_DECOMPOSE_RETRY", "Undersized decomposition, retrying with Gemini")
            data = await _call_decompose(force_cloud=True)
            num_tasks = len(data.get("decomposition", []))
        graph = ExecutionGraph(**data)
        # Cache successful decomposition
        if len(data.get("decomposition", [])) >= 5:
            _decompose_cache[_decompose_cache_key] = (time_mod.time(), data)
        _decompose_dur = int((time_mod.monotonic() - _decompose_start) * 1000)
        if progress_callback:
            _task_titles = [t.title for t in graph.decomposition]
            _task_data = [t.model_dump() for t in graph.decomposition]
            await progress_callback("decomposition_done", {
                "task_count": len(graph.decomposition),
                "total_minutes": sum(t.duration_minutes for t in graph.decomposition),
                "duration_ms": _decompose_dur,
                "task_titles": _task_titles,
                "tasks": _task_data,  # Full task data for progressive render
                "phase_summary": f"Created {len(graph.decomposition)} tasks ({sum(t.duration_minutes for t in graph.decomposition)} min): {', '.join(_task_titles[:3])}{'...' if len(_task_titles) > 3 else ''}",
            })
        if len(graph.decomposition) < 5:
            return ChatResponse(
                intent=IntentType.PLAN_DAY.value,
                message=(
                    "I struggled to break that goal down. "
                    "Could you clarify what exactly you want to achieve?"
                ),
                schedule=None,
                execution_graph=None,
                thinking_process=(
                    "I tried to decompose your goal into micro-tasks but couldn't break it "
                    "into enough actionable steps. A clearer goal (e.g. 'Plan my day to study "
                    "for math midterm') helps me schedule better."
                ),
                draft_id=draft.draft_id if draft else None,
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
            thinking_process=(
                "I tried to decompose your goal into micro-tasks but couldn't break it "
                "into enough actionable steps. A clearer goal (e.g. 'Plan my day to study "
                "for math midterm') helps me schedule better."
            ),
            draft_id=draft.draft_id if draft else None,
        )

    # If skip_scheduling is True, return the decomposition for user review
    if skip_scheduling:
        return ChatResponse(
            intent=IntentType.PLAN_DAY.value,
            message="Here are your tasks. Review and edit, then click Schedule to continue.",
            schedule=None,
            execution_graph=graph.model_dump() if hasattr(graph, "model_dump") else {
                "goal_metadata": graph.goal_metadata.__dict__ if graph.goal_metadata else {},
                "decomposition": [t.model_dump() for t in graph.decomposition],
                "cognitive_load_estimate": graph.cognitive_load_estimate,
            },
            awaiting_task_confirmation=True,
            thinking_process=f"Decomposed your goal into {len(graph.decomposition)} micro-tasks totaling {sum(t.duration_minutes for t in graph.decomposition)} minutes. Waiting for your review before scheduling.",
            draft_id=draft.draft_id if draft else None,
        )

    goal_id = graph.goal_metadata.goal_id if graph.goal_metadata else None
    if not goal_id:
        obj = (graph.goal_metadata.objective if graph.goal_metadata else "") or planning_goal
        slug = "".join(c if c.isalnum() or c == " " else "" for c in obj)[:30].replace(" ", "_").lower()
        goal_id = slug or f"plan_{uuid.uuid4().hex[:8]}"

    # Ensure dependencies are prefixed; _namespace_chunk does this
    new_prefixed = [_namespace_chunk(c, goal_id) for c in graph.decomposition]
    all_pending = get_all_pending_tasks(user_id, supabase)
    # Keep tasks from OTHER goals, drop tasks from THIS goal (will be replaced by new decomposition)
    pending_chunks = [c for c in all_pending if not c.task_id.startswith(f"{goal_id}_")]
    master_chunk_list = list(pending_chunks) + list(new_prefixed)
    log_step("P5_FUSION", "Multi-goal fusion", {
        "existing_pending": len(all_pending),
        "kept_from_other_goals": len(pending_chunks),
        "new_tasks": len(new_prefixed),
        "total_fused": len(master_chunk_list),
    })

    plan_deadlines = _get_plan_deadlines_from_db(user_id, None, supabase)
    inferred_horizon = compute_horizon_from_deadlines(
        plan_start=plan_start,
        chunks=master_chunk_list,
        external_deadline=deadline_override,
        plan_deadlines=plan_deadlines,
    )
    if inferred_horizon is not None:
        horizon_steps = [min(inferred_horizon, MAX_HORIZON_MINUTES)]
    else:
        extended_steps = [2880, 4320, 7200, 10080, 20160, 43200]
        horizon_steps = [h for h in extended_steps if h <= MAX_HORIZON_MINUTES]

    schedule_response = None
    used_horizon_minutes = DEFAULT_HORIZON_MINUTES
    total_task_min = sum(c.duration_minutes for c in master_chunk_list)
    synthetic_graph = ExecutionGraph(
        goal_metadata=graph.goal_metadata,
        decomposition=master_chunk_list,
        cognitive_load_estimate=graph.cognitive_load_estimate,
    )
    _validate_master_chunk_dependencies(master_chunk_list)
    _schedule_start = time_mod.monotonic()
    if progress_callback:
        await progress_callback("scheduling", {
            "task_count": len(master_chunk_list),
            "total_minutes": total_task_min,
            "pending_from_other_goals": len(pending_chunks),
            "started_at_ms": int(time_mod.time() * 1000),
        })
    log_step(
        "P5_SCHEDULE",
        "Attempting OR-Tools solve (fused)",
        {"horizon_steps": horizon_steps, "total_task_min": total_task_min, "master_chunks": len(master_chunk_list), "blocks": len(_build_daily_context(horizon_steps[0]))},
    )

    for horizon_min in horizon_steps:
        if horizon_min > MAX_HORIZON_MINUTES:
            break
        daily_context = _build_daily_context(horizon_min)
        log_step("P5_SCHEDULE_TRY", f"Trying horizon={horizon_min}min ({horizon_min//1440}d)")
        try:
            schedule_response = run_schedule(
                synthetic_graph,
                daily_context,
                horizon_minutes=horizon_min,
                horizon_start=horizon_start,
                max_daily_deep_work_minutes=max_daily_deep_work_minutes,
                min_daily_deep_work_minutes=min_daily_deep_work_minutes,
                max_task_duration_minutes=max_task_duration_minutes,
                min_task_duration_minutes=min_task_duration_minutes,
            )
            used_horizon_minutes = horizon_min
            log_step("P5_SCHEDULE_OK", f"Feasible with horizon={horizon_min}min")
            break
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            log_step("P5_SCHEDULE_INFEASIBLE", f"Horizon {horizon_min}min failed (422), retrying with longer horizon")
            continue

    if schedule_response is not None:
        _schedule_dur = int((time_mod.monotonic() - _schedule_start) * 1000)
        _sched_task_count = len(schedule_response.schedule) if hasattr(schedule_response, 'schedule') else 0
        if progress_callback:
            await progress_callback("schedule_done", {
                "status": "OPTIMAL",
                "task_count": _sched_task_count,
                "schedule": schedule_response.model_dump(mode='json'),  # Full schedule data
                "horizon_hours": round(used_horizon_minutes / 60, 1),
                "duration_ms": _schedule_dur,
                "phase_summary": f"Scheduled {_sched_task_count} tasks across {round(used_horizon_minutes / 60, 1)}h (OPTIMAL)",
            })
        summary = execution_summary or {}
        summary["schedule_generated"] = True
        if used_horizon_minutes > DEFAULT_HORIZON_MINUTES:
            summary["spread_across_days"] = True

        # Defer persistence — schedule is returned as draft for user to accept
        # _persist_fused_tasks is called only via POST /chat/accept-schedule

        if draft and draft_store:
            from app.services.draft_store import DraftComponent
            draft_store.add_component(
                draft.draft_id, user_id, "tasks",
                DraftComponent(
                    component_type="tasks",
                    data=[c.model_dump() for c in master_chunk_list],
                    status="pending",
                ),
            )
        if schedule_response is not None and draft and draft_store:
            from app.services.draft_store import DraftComponent
            draft_store.add_component(
                draft.draft_id, user_id, "schedule",
                DraftComponent(
                    component_type="schedule",
                    data=schedule_response.model_dump(mode='json'),
                    status="pending",
                ),
            )

        search_result: Optional[dict] = None
        if search_task is not None:
            try:
                search_result = await search_task
                summary["search_done"] = search_result.get("queries", [])
            except Exception as e:
                print(f"[Deep Research] Task failed: {e}")
                search_result = {"queries": [], "summaries": []}

        if progress_callback:
            await progress_callback("synthesizing", {"model": SLM_ROUTER_MODEL})
        # Skip VoJ when frontend will render structured schedule data directly.
        # VoJ was adding 5-10s for a message the user doesn't read when they can
        # see the visual schedule. Use deterministic fallback instead.
        message = "Here's your schedule."
        thinking_process = _build_thinking_fallback(summary)
        return ChatResponse(
            intent=IntentType.PLAN_DAY.value,
            message=message,
            schedule=schedule_response.model_dump(mode='json'),
            execution_graph=synthetic_graph.model_dump(mode='json'),
            action_proposals=action_proposals,
            search_result=search_result,
            suggested_action="replan" if summary.get("habits_saved") else None,
            thinking_process=thinking_process,
            schedule_status="draft",
            draft_id=draft.draft_id if draft else None,
        )

    log_step(
        "P5_SCHEDULE_FAIL",
        "All horizon retries exhausted, returning INFEASIBLE to user",
        {"horizons_tried": horizon_steps, "total_task_min": total_task_min},
    )
    return ChatResponse(
        intent=IntentType.PLAN_DAY.value,
        message=(
            "This schedule is mathematically impossible to fit into your day, "
            "especially considering your personal constraints. Try reducing the scope "
            "of your tasks or temporarily relaxing a habit."
        ),
        schedule=None,
        execution_graph=synthetic_graph.model_dump(mode='json'),
        suggested_action="replan",
        thinking_process=(
            "I built a schedule from your tasks and habits, but OR-Tools couldn't find "
            "a feasible solution. Your constraints may be too tight—try reducing scope "
            "or relaxing a habit."
        ),
        draft_id=draft.draft_id if draft else None,
    )


async def _run_schedule_modify_flow(
    user_prompt: str,
    user_id: str,
    draft_schedule: dict,
    db_client: Any,
    progress_callback: ProgressCallback = None,
) -> ChatResponse:
    """Handle schedule modification without re-running the full pipeline.

    Uses 4B SLM to parse modification intent, applies it to the draft,
    and re-schedules with OR-Tools if needed.
    """
    from app.services.analytical.schedule_modifier import (
        apply_modification,
        parse_modification_request,
    )
    from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots

    execution_graph = draft_schedule.get("execution_graph", {})
    schedule_data = draft_schedule.get("schedule", {})
    horizon_start_str = draft_schedule.get("horizon_start")
    decomposition = execution_graph.get("decomposition", [])

    if progress_callback:
        await progress_callback("modifying_schedule", {
            "model": SLM_ROUTER_MODEL,
            "phase_summary": "Parsing your schedule change...",
        })

    # Parse the modification request
    modification = await parse_modification_request(user_prompt, decomposition)
    if modification is None:
        return ChatResponse(
            intent=IntentType.PLAN_DAY.value,
            message="I couldn't understand that modification. Could you rephrase? For example: 'make task 2 longer' or 'add a reading task'.",
            schedule=schedule_data,
            execution_graph=execution_graph,
            schedule_status="draft",
        )

    # Parse horizon_start
    horizon_start = None
    if horizon_start_str:
        try:
            horizon_start = datetime.fromisoformat(horizon_start_str)
        except (ValueError, TypeError):
            horizon_start = datetime.now(timezone.utc)
    else:
        horizon_start = datetime.now(timezone.utc)

    # Build time slots for re-scheduling
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    habits = await get_behavioral_context_for_calendar(user_id, supabase)
    semantic_slots = []
    if habits and habits.strip():
        semantic_slots = await translate_habits_to_slots(habits)
    time_slots = expand_semantic_slots_to_time_slots(
        semantic_slots,
        horizon_minutes=DEFAULT_HORIZON_MINUTES,
        plan_start=datetime.now(timezone.utc),
    )

    # Apply the modification
    modified_graph, modified_schedule = apply_modification(
        modification, execution_graph, schedule_data, time_slots, horizon_start,
    )

    action_desc = modification.action.replace("_", " ")
    target = modification.target_task_title or modification.add_task_title or ""
    msg = f"Updated your schedule ({action_desc}"
    if target:
        msg += f": {target}"
    msg += "). Review the changes below."

    return ChatResponse(
        intent=IntentType.PLAN_DAY.value,
        message=msg,
        schedule=modified_schedule,
        execution_graph=modified_graph,
        schedule_status="draft",
        thinking_process=f"Applied {action_desc} modification to draft schedule.",
    )


async def execute_agentic_flow(
    user_prompt: str,
    user_id: str,
    db_client: Any,
    day_start_hour_override: Optional[int] = None,
    deadline_override: Optional[str] = None,
    file_base64: Optional[str] = None,
    media_type: Optional[str] = None,
    max_daily_deep_work_minutes: Optional[int] = None,
    min_daily_deep_work_minutes: Optional[int] = None,
    max_task_duration_minutes: Optional[int] = None,
    min_task_duration_minutes: Optional[int] = None,
    progress_callback: ProgressCallback = None,
    model_mode: str = "auto",
    skip_scheduling: bool = False,
    file_name: Optional[str] = None,
    draft_schedule: Optional[dict] = None,
    draft_store: Optional[Any] = None,
) -> ChatResponse:
    """Master orchestrator: brain dump extraction, multi-execution, Voice of Jarvis."""
    # Pre-check: if draft_schedule is provided, route to schedule modification flow
    if draft_schedule is not None:
        log_step("SCHEDULE_MODIFY", "Draft schedule provided, routing to modification flow")
        return await _run_schedule_modify_flow(
            user_prompt=user_prompt,
            user_id=user_id,
            draft_schedule=draft_schedule,
            db_client=db_client,
            progress_callback=progress_callback,
        )

    # Resolve prompt: combine user_prompt with extracted file text if provided
    effective_prompt = user_prompt
    file_bytes: Optional[bytes] = None
    if file_base64 and media_type:
        try:
            import base64
            from app.utils.docling_helper import extract_document
            raw_bytes = base64.b64decode(file_base64)
            file_bytes = raw_bytes
            file_text = extract_document(raw_bytes, media_type)
            if file_text and file_text.strip():
                effective_prompt = (user_prompt + "\n\n" + file_text.strip()) if user_prompt.strip() else file_text.strip()
        except Exception as e:
            print(f"[Control Policy] File extraction failed: {e}")

    # Resolve model names for progress visibility
    from app.core.config import LOCAL_LLM_MODEL
    model_labels = {
        "auto": {"classifier": SLM_ROUTER_MODEL, "reasoner": LOCAL_LLM_MODEL},
        "4b": {"classifier": SLM_ROUTER_MODEL, "reasoner": SLM_ROUTER_MODEL},
        "27b": {"classifier": LOCAL_LLM_MODEL, "reasoner": LOCAL_LLM_MODEL},
    }
    active_models = model_labels.get(model_mode, model_labels["auto"])

    # 27B direct mode: bypass pipeline, send directly to 27B
    if model_mode == "27b":
        log_step("DIRECT_27B", "27B mode: bypassing pipeline, direct response")
        if progress_callback:
            await progress_callback("intent_classified", {
                "intent": "GENERAL_QA",
                "model": LOCAL_LLM_MODEL,
                "model_mode": "27b",
                "phase_summary": "Direct 27B mode — answering with full reasoning power",
            })
        return await _direct_qa_response(
            effective_prompt,
            model_override=LOCAL_LLM_MODEL,
            progress_callback=progress_callback,
        )

    # Step 1: Brain dump extraction
    _pipeline_start = time_mod.monotonic()
    _phase_start = time_mod.monotonic()
    log_step("1_BRAIN_DUMP", "Extracting planning_goal, habits, action_items, search_queries")
    if progress_callback:
        await progress_callback("brain_dump_extraction", {
            "model": active_models["classifier"],
            "model_mode": model_mode,
            "started_at_ms": int(time_mod.time() * 1000),
        })
    extraction = await _run_brain_dump_extraction(effective_prompt)
    _extraction_duration = int((time_mod.monotonic() - _phase_start) * 1000)
    if extraction is None or _is_extraction_empty(extraction):
        return await _fallback_single_intent(
            effective_prompt,
            user_id,
            db_client,
            day_start_hour_override=day_start_hour_override,
            max_daily_deep_work_minutes=max_daily_deep_work_minutes,
            min_daily_deep_work_minutes=min_daily_deep_work_minutes,
            max_task_duration_minutes=max_task_duration_minutes,
            min_task_duration_minutes=min_task_duration_minutes,
            progress_callback=progress_callback,
        )

    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    # Guard: if has_knowledge=True but no file attached and prompt looks like a question,
    # the 4B model misclassified a question as knowledge ingestion. Override and fallback.
    if extraction.has_knowledge and not file_bytes:
        _prompt_lower = effective_prompt.strip().lower()
        _is_question = (
            _prompt_lower.startswith((
                "what ", "how ", "why ", "when ", "where ", "who ",
                "explain ", "teach ", "tell me", "describe ", "define ",
                "can you", "could you", "help me understand",
            ))
            or "?" in _prompt_lower
        )
        if _is_question and not extraction.planning_goal:
            extraction.has_knowledge = False
            log_step("GUARD", "Overrode has_knowledge=False — prompt is a question, not a document")
            if _is_extraction_empty(extraction):
                return await _fallback_single_intent(
                    effective_prompt, user_id, db_client,
                    day_start_hour_override=day_start_hour_override,
                    max_daily_deep_work_minutes=max_daily_deep_work_minutes,
                    min_daily_deep_work_minutes=min_daily_deep_work_minutes,
                    max_task_duration_minutes=max_task_duration_minutes,
                    min_task_duration_minutes=min_task_duration_minutes,
                    progress_callback=progress_callback,
                )

    execution_summary: dict[str, Any] = {}
    action_proposals: list[dict] = []
    search_result: Optional[dict] = None
    ingestion_result: Optional[dict] = None

    # Emit intent classification with real extraction data + timing
    _intent = "PLAN_DAY" if extraction.planning_goal else (
        "BEHAVIORAL_CONSTRAINT" if extraction.inline_habits else
        "KNOWLEDGE_INGESTION" if extraction.has_knowledge else
        "CALENDAR_SYNC" if extraction.has_calendar else
        "ACTION_ITEM" if extraction.action_items else
        "GENERAL_QA" if extraction.search_queries else "GREETING"
    )
    _habit_count = len(extraction.inline_habits) if extraction.inline_habits else 0
    _action_count = len(extraction.action_items) if extraction.action_items else 0
    _parts = [f"Classified as {_intent}"]
    if _habit_count > 0:
        _parts.append(f"{_habit_count} habit{'s' if _habit_count > 1 else ''}")
    if _action_count > 0:
        _parts.append(f"{_action_count} action item{'s' if _action_count > 1 else ''}")
    if extraction.planning_goal:
        _parts.append("planning goal detected")

    if progress_callback:
        await progress_callback("intent_classified", {
            "intent": _intent,
            "has_planning_goal": bool(extraction.planning_goal),
            "habit_count": _habit_count,
            "action_count": _action_count,
            "has_search": bool(extraction.search_queries),
            "has_calendar": extraction.has_calendar or False,
            "has_knowledge": extraction.has_knowledge or False,
            "model": active_models["classifier"],
            "model_mode": model_mode,
            "duration_ms": _extraction_duration,
            "phase_summary": " — ".join(_parts),
        })

    # Create draft to hold pipeline output for review
    draft = None
    if draft_store is not None:
        draft = draft_store.create(user_id, metadata={
            "prompt": user_prompt[:200],
            "intent": _intent,
        })

    # Step 2: Spawn search task immediately (runs in parallel)
    search_task: Optional[asyncio.Task] = None
    if extraction.search_queries:
        log_step("2_SEARCH", "Spawning parallel search", {"queries": extraction.search_queries})
        search_task = asyncio.create_task(run_deep_research(extraction.search_queries))

    # Step 3: Habits (staged — not auto-committed; user must approve via draft review)
    if extraction.inline_habits:
        log_step("3_HABITS", "Staging inline habits for review", {"count": len(extraction.inline_habits)})
        from app.services.extraction.behavioral_store import stage_habit
        staged_habits = []
        for h in extraction.inline_habits:
            if h and h.strip():
                staged_habits.append(stage_habit(h.strip(), user_id))
                print(f"[Memory] Inline habit staged: {h.strip()}")
        execution_summary["habits_staged"] = staged_habits
        if progress_callback:
            await progress_callback("habits_staged", {
                "count": len(staged_habits),
                "habits": staged_habits,  # Full data, not just count
                "phase_summary": f"Found {len(staged_habits)} habit(s) for your review",
            })

    if extraction.inline_habits and draft and draft_store:
        from app.services.draft_store import DraftComponent
        draft_store.add_component(
            draft.draft_id, user_id, "habits",
            DraftComponent(
                component_type="habits",
                data=execution_summary.get("habits_staged", []),
                status="pending",
            ),
        )

    # Step 4: State updates (transient, logic injection happens in plan flow)
    if extraction.state_updates:
        execution_summary["state_applied"] = extraction.state_updates

    # Step 4b: Deadline update from chat (e.g. "exam is March 20")
    if extraction.deadline_update and supabase and user_id:
        from app.utils.deadline_parser import parse_deadline_to_date

        parsed = parse_deadline_to_date(extraction.deadline_update, datetime.now(timezone.utc))
        if parsed and parsed.date() > datetime.now(timezone.utc).date():
            goal_id_val = None
            try:
                result = (
                    supabase.table("user_tasks")
                    .select("goal_id")
                    .eq("user_id", user_id)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if result.data and result.data[0].get("goal_id"):
                    goal_id_val = result.data[0]["goal_id"]
            except Exception:
                pass
            try:
                supabase.table("user_plan_updates").insert(
                    {
                        "user_id": user_id,
                        "goal_id": goal_id_val,
                        "source": "chat",
                        "deadline_date": parsed.date().isoformat(),
                        "deadline_raw": extraction.deadline_update,
                        "context_snippet": None,
                    }
                ).execute()
            except Exception as e:
                print(f"[Control Policy] Store deadline_update failed: {e}")

    # Step 5: Action items
    if extraction.action_items:
        log_step("5_ACTION_ITEMS", "Proposing action items", {"count": len(extraction.action_items)})
        for item in extraction.action_items:
            if item and item.strip():
                try:
                    prop = await propose_action_item(item.strip())
                    action_proposals.append(prop.model_dump())
                    # Store deadline from action item in user_plan_updates
                    if prop.deadline_date and supabase and user_id:
                        from app.utils.deadline_parser import parse_deadline_to_date

                        parsed = parse_deadline_to_date(prop.deadline_date, datetime.now(timezone.utc))
                        if parsed and parsed.date() > datetime.now(timezone.utc).date():
                            goal_id_val = None
                            try:
                                result = (
                                    supabase.table("user_tasks")
                                    .select("goal_id")
                                    .eq("user_id", user_id)
                                    .order("created_at", desc=True)
                                    .limit(1)
                                    .execute()
                                )
                                if result.data and result.data[0].get("goal_id"):
                                    goal_id_val = result.data[0]["goal_id"]
                            except Exception:
                                pass
                            try:
                                supabase.table("user_plan_updates").insert(
                                    {
                                        "user_id": user_id,
                                        "goal_id": goal_id_val,
                                        "source": "action_item",
                                        "deadline_date": parsed.date().isoformat(),
                                        "deadline_raw": prop.deadline_date,
                                        "context_snippet": prop.title[:200] if prop.title else None,
                                    }
                                ).execute()
                            except Exception as e:
                                print(f"[Control Policy] Store action_item deadline failed: {e}")
                except Exception as e:
                    print(f"[Action Item] Failed for '{item}': {e}")
        execution_summary["action_proposals"] = action_proposals

    if action_proposals and draft and draft_store:
        from app.services.draft_store import DraftComponent
        draft_store.add_component(
            draft.draft_id, user_id, "action_items",
            DraftComponent(
                component_type="action_items",
                data=action_proposals,
                status="pending",
            ),
        )

    # Step 6: Calendar
    if extraction.has_calendar and extraction.calendar_text:
        log_step("6_CALENDAR", "Extracting calendar slots")
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

    # Step 6b: Knowledge ingestion (sample papers, DPP, syllabi from chat or file)
    if extraction.has_knowledge or (file_bytes and media_type and not extraction.planning_goal):
        log_step("6b_KNOWLEDGE", "Ingesting knowledge/syllabus")
        try:
            payload_for_ingest = effective_prompt if (extraction.has_knowledge or effective_prompt.strip()) else None
            result = await process_ingestion(
                payload=payload_for_ingest,
                file_bytes=file_bytes,
                media_type=media_type,
                user_id=user_id,
                db_client=db_client,
                intent_override=IntentType.KNOWLEDGE_INGESTION,
                file_name=file_name,
            )
            ingestion_result = result.model_dump()
            execution_summary["knowledge_ingested"] = True
            if result.knowledge_result and result.knowledge_result.get("linked_task_ids"):
                execution_summary["materials_linked"] = result.knowledge_result["linked_task_ids"]
        except Exception as e:
            print(f"[Control Policy] Knowledge ingestion failed: {e}")

    # Step 7: Planning (if planning_goal) — search_task runs in parallel, awaited inside
    if extraction.planning_goal:
        log_step("7_PLAN_DAY", "Running plan-day flow", {"goal": extraction.planning_goal[:80]})
        if progress_callback:
            await progress_callback("plan_day_start", {"goal": extraction.planning_goal[:120]})
        return await _run_plan_day_flow(
            user_prompt=effective_prompt,
            user_id=user_id,
            db_client=db_client,
            planning_goal=extraction.planning_goal,
            state_updates=extraction.state_updates or None,
            use_voice_synthesis=False,
            execution_summary=execution_summary,
            action_proposals=action_proposals if action_proposals else None,
            search_task=search_task,
            day_start_hour_override=day_start_hour_override,
            deadline_override=deadline_override,
            max_daily_deep_work_minutes=max_daily_deep_work_minutes,
            min_daily_deep_work_minutes=min_daily_deep_work_minutes,
            max_task_duration_minutes=max_task_duration_minutes,
            min_task_duration_minutes=min_task_duration_minutes,
            progress_callback=progress_callback,
            skip_scheduling=skip_scheduling,
            inline_habits_already_saved=bool(extraction.inline_habits),
            draft=draft,
            draft_store=draft_store,
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
    elif execution_summary.get("knowledge_ingested"):
        intent = IntentType.KNOWLEDGE_INGESTION.value
        suggested = None
    elif ingestion_result:
        intent = IntentType.CALENDAR_SYNC.value
        suggested = None
    elif search_result:
        intent = IntentType.GENERAL_QA.value
        suggested = None
    else:
        intent = IntentType.GREETING.value
        suggested = None

    return ChatResponse(
        intent=intent,
        message=message,
        ingestion_result=ingestion_result,
        action_proposals=action_proposals if action_proposals else None,
        search_result=search_result,
        suggested_action=suggested,
        thinking_process=thinking_process,
        draft_id=draft.draft_id if draft else None,
    )


# ---------------------------------------------------------------------------
# Background Replan Trigger
# ---------------------------------------------------------------------------


async def trigger_replan(
    user_id: str,
    db_client: Any,
    reason: str,
) -> Optional[ChatResponse]:
    """Trigger a headless replan of pending tasks for a user.

    Called from task endpoints (complete, skip, delete) and future event bus
    handlers. Runs _run_plan_day_flow with existing pending tasks — no new
    decomposition, just re-solve the schedule with the current master chunk list.

    Args:
        user_id: The user whose schedule should be replanned.
        db_client: DatabaseClient from app state.
        reason: Human-readable trigger reason (e.g. "task_completed:task_123").

    Returns:
        ChatResponse if replan succeeded, None if no pending tasks.
    """
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        log_step("REPLAN_SKIP", f"No DB for replan (reason={reason})")
        return None

    pending = get_all_pending_tasks(user_id, supabase)
    if not pending:
        log_step("REPLAN_SKIP", f"No pending tasks for user {user_id} (reason={reason})")
        return None

    log_step("REPLAN_START", f"Background replan triggered", {"reason": reason, "pending_count": len(pending), "user_id": user_id})

    # Re-solve schedule with existing pending tasks (no LLM decomposition)
    from app.api.v1.endpoints.schedule import run_schedule
    from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots
    from app.services.analytical.habit_translator import translate_habits_to_slots
    from app.services.extraction.behavioral_store import get_behavioral_context_for_calendar
    from app.utils.deadline_parser import compute_horizon_from_deadlines

    plan_start = datetime.now(timezone.utc)
    resolved_day_start = DAY_START_HOUR
    plan_date = plan_start.date()
    if plan_start.hour < resolved_day_start:
        plan_date -= timedelta(days=1)
    horizon_start = datetime.combine(plan_date, time(resolved_day_start, 0), tzinfo=timezone.utc)
    past_minutes = max(0, int((plan_start - horizon_start).total_seconds() / 60))

    # Fetch and translate habits
    habits = await get_behavioral_context_for_calendar(user_id, supabase)
    if habits and habits.strip():
        semantic_slots = await translate_habits_to_slots(habits)
    else:
        semantic_slots = []

    # Compute horizon from deadlines
    plan_deadlines = _get_plan_deadlines_from_db(user_id, None, supabase)
    inferred_horizon = compute_horizon_from_deadlines(
        plan_start=plan_start,
        chunks=pending,
        external_deadline=None,
        plan_deadlines=plan_deadlines,
    )
    if inferred_horizon is not None:
        horizon_steps = [min(inferred_horizon, MAX_HORIZON_MINUTES)]
    else:
        horizon_steps = [h for h in [2880, 4320, 7200, 10080] if h <= MAX_HORIZON_MINUTES]

    # Build daily context (habit blocks + past time)
    def _build_ctx(horizon_minutes: int) -> list:
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

    # Build synthetic graph from pending tasks
    synthetic_graph = ExecutionGraph(
        goal_metadata=None,
        decomposition=pending,
        cognitive_load_estimate={"intrinsic_load": 0.5},
    )

    schedule_response = None
    for horizon_min in horizon_steps:
        daily_context = _build_ctx(horizon_min)
        try:
            schedule_response = run_schedule(
                synthetic_graph,
                daily_context,
                horizon_minutes=horizon_min,
                horizon_start=horizon_start,
            )
            log_step("REPLAN_OK", f"Background replan feasible", {"horizon": horizon_min, "reason": reason})
            break
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            continue

    if schedule_response is not None:
        # Persist the updated schedule (pending tasks unchanged, just re-ordered)
        _persist_fused_tasks(user_id, pending, supabase)
        log_step("REPLAN_DONE", f"Background replan complete", {"reason": reason})
    else:
        log_step("REPLAN_INFEASIBLE", f"Background replan infeasible", {"reason": reason})

    return None
