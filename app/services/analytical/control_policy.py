"""Control Policy: master orchestrator for the unified /chat endpoint.

DEPRECATED (2026-08-08): superseded by app/orchestrator/. Accept-schedule endpoints
still call into _persist_fused_tasks; do not add features here.
"""

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
import app.core.config as _cfg
from app.core.config import (
    DAY_START_HOUR,
    DEFAULT_HORIZON_MINUTES,
    GEMINI_API_KEY,
    MAX_HORIZON_MINUTES,
)
# IMPORTANT: _cfg.SLM_ROUTER_MODEL and LOCAL_LLM_MODEL are mutated at runtime by
# detect_loaded_models(). Always reference _cfg.X (not a captured import) so
# we pick up the actual loaded model name. Single-26B setups depend on this.
from app.models.brain.litellm_conf import gemini_primary_route, hybrid_route_query, local_primary_route, run_deep_research
from app.schemas.context import (
    Availability,
    BrainDumpExtraction,
    ChatResponse,
    IntentClassification,
    IntentType,
    SchedulePayload,
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
_DECOMPOSE_CACHE_TTL_S = 14400  # 4 hours (was 3600)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

BRAIN_DUMP_EXTRACTION_PROMPT = (
    "You extract components from a user's brain-dump message. "
    "Parse the message and populate each field. Use null/empty for missing categories.\n\n"
    "planning_goal: The user's scheduling intent WITH all specific subjects, topics, exams, contests, and deadlines preserved verbatim. "
    "Include domain details — never reduce to a generic summary. "
    "E.g. 'Plan my week for deep learning contest Friday and calculus exam Monday', NOT just 'Plan my week'. "
    "Use null for greetings, chitchat, questions, or very short non-goals (e.g. 'hi', 'hello', 'what is X', 'teach me X'). "
    "Only extract when the user clearly wants to plan or schedule.\n"
    "inline_habits: Extract the EXACT, VERBATIM phrase for each LONG-TERM behavioral RULE. "
    "DO NOT summarize or shorten. CRITICAL: Preserve all time anchors (e.g. 'before 11 AM', "
    "'after 2 PM', 'no work until noon') — the scheduler needs these for math. "
    "Example: 'I hate mornings, never schedule work before 11 AM' -> "
    "[\"I hate working in the mornings, so please never schedule work before 11 AM\"] (full phrase, not \"I hate mornings\"). "
    "Return as a list of complete phrases. Character, not mood.\n"
    "STRICT NEGATIVES — return null/empty for inline_habits when ANY of these apply:\n"
    "  - Emotional disclosure ('I am sad', 'I feel anxious', 'I'm tired', 'feeling low', 'I'm overwhelmed')\n"
    "  - Greetings or chitchat ('hi', 'hey', 'hmm', 'ok', 'thanks')\n"
    "  - Questions or knowledge queries ('what is X', 'teach me Y', 'explain Z')\n"
    "  - Single-word fragments or text under 5 characters\n"
    "  - Vague feelings without a concrete time/preference rule\n"
    "Emotional input belongs in state_updates, NOT inline_habits.\n"
    "state_updates: Temporary today-only mood OR emotional disclosure (I'm tired, feeling sick, "
    "feeling sad, anxious today, take it easy, go light on difficulty). Never stored as a habit.\n"
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
    "subject_context: List of specific subjects/topics/exams mentioned with any associated time references. "
    "E.g. [\"deep learning contest - Friday\", \"calculus exam - Monday\"]. Use null if no specific subjects mentioned.\n"
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
    "BEHAVIORAL_CONSTRAINT: A LONG-TERM behavioral RULE the user wants the scheduler to respect. "
    "Examples: 'I sit in back bench', 'no meetings before 10', 'I hate mornings', "
    "'never schedule deep work after 9 PM', 'I prefer light tasks on Sundays'. "
    "MUST contain a concrete rule or preference. "
    "NOT for emotional disclosure: 'I am sad', 'I feel anxious', 'I'm tired today', "
    "'feeling low', 'overwhelmed' — those are GENERAL_QA (the user wants support, not a constraint).\n"
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
            model_override=_cfg.SLM_ROUTER_MODEL,
        )
        if not extracted:
            return []
        raw = extracted if isinstance(extracted, str) else str(extracted)
        raw = raw.strip()
        if "NONE" in raw.upper() or len(raw) <= 5:
            return []
        staged = stage_habit(raw, user_id)
        return [staged] if staged is not None else []
    except Exception as e:
        print(f"[Memory] Inline extraction failed: {e}")
        return []


def _build_planning_context(
    user_id: str,
    planning_goal: str,
    supabase_client: Any,
    extraction: Any = None,
) -> str:
    """Enrich planning_goal with deadline context from user_plan_updates and extraction."""
    enriched = planning_goal
    if supabase_client and user_id:
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
            matched = False
            for r in rows:
                snippet = (r.get("context_snippet") or "").lower()
                if not snippet or not goal_words:
                    continue
                snippet_words = set(snippet.split())
                if goal_words & snippet_words:
                    deadline_date = r.get("deadline_date")
                    if deadline_date:
                        enriched = f"[Context: Known deadline for this goal: {deadline_date}.] {planning_goal}"
                        matched = True
                    break
            if not matched:
                for r in rows:
                    deadline_date = r.get("deadline_date")
                    if deadline_date:
                        enriched = f"[Context: Known deadline: {deadline_date}.] {planning_goal}"
                        break
        except Exception as e:
            print(f"[Control Policy] _build_planning_context failed: {e}")

    # Append deadline from current extraction
    if extraction and hasattr(extraction, "deadline_update") and extraction.deadline_update:
        enriched += f" [Deadline: {extraction.deadline_update}]"

    # Append subject context from current extraction
    if extraction and hasattr(extraction, "subject_context") and extraction.subject_context:
        enriched += f" [Subjects: {', '.join(extraction.subject_context)}]"

    return enriched


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


def derive_goal_id(goal_metadata: Any, fallback_objective: str = "") -> str:
    """The namespace every chunk of one goal is prefixed with.

    Order matters and is the same in both pipelines: the model's own
    ``goal_metadata.goal_id`` wins, else the objective (or the raw planning goal
    when the model left it blank) is slugified, else a uuid so two goals can
    never share a namespace by accident.

    Extracted from ``_run_plan_day_flow`` so the v2 planning sub-graph derives it
    identically — a second copy would drift and re-open F5, where un-namespaced
    positional ids let a new plan shadow (and then delete) the previous one's
    pending rows.
    """
    goal_id = getattr(goal_metadata, "goal_id", None) if goal_metadata else None
    if goal_id:
        return goal_id

    objective = (getattr(goal_metadata, "objective", "") if goal_metadata else "") or fallback_objective or ""
    slug = "".join(c if c.isalnum() or c == " " else "" for c in objective)[:30].replace(" ", "_").lower()
    return slug or f"plan_{uuid.uuid4().hex[:8]}"


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
    chunks: list,
    supabase_client: Any,
    schedule: dict | None = None,
    horizon_start: str | None = None,
) -> str | None:
    """Replace all pending user_tasks with the fused master chunk list.

    Deletes existing pending rows, then inserts fresh rows for each chunk.
    Chunks must have prefixed task_ids (goal_id_orig_id). Full TaskChunk fields
    are persisted for retrieval by get_all_pending_tasks.

    Args:
        schedule: Optional dict mapping task_id -> {"start_min": int, "end_min": int}
                  (OR-Tools output). Used to compute wall-clock scheduled_start/end.
        horizon_start: Optional ISO-8601 datetime string representing minute-0 of
                       the scheduling horizon.

    Returns:
        The ``plan_id`` stamped on every inserted row when the write completed,
        else ``None``. Exceptions are still swallowed — this function is called
        from paths that must not die on a persistence blip — so the return value
        is the only signal a caller has that anything happened. Callers that need
        proof (draft acceptance) re-read user_tasks filtered on this plan_id:
        task_ids alone cannot distinguish rows this call wrote from rows an
        earlier plan left behind, since fused chunk lists deliberately carry
        pre-existing pending tasks forward.

    Safety: will NOT delete if chunks list is empty (prevents accidental wipe
    when task retrieval fails upstream).
    """
    if not supabase_client or not user_id:
        return None
    if not chunks:
        print("[Control Policy] _persist_fused_tasks: empty chunks list, skipping to prevent data loss")
        return None
    try:
        plan_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        # Parse horizon_start for wall-clock computation
        horizon_dt: datetime | None = None
        if horizon_start:
            try:
                horizon_dt = datetime.fromisoformat(horizon_start)
            except (ValueError, TypeError):
                print(f"[Control Policy] Could not parse horizon_start: {horizon_start}")

        rows: list[dict[str, Any]] = []

        for chunk in chunks:
            # Support both Pydantic TaskChunk objects and plain dicts
            task_id = getattr(chunk, "task_id", None) or (chunk.get("task_id") if isinstance(chunk, dict) else None)
            title = getattr(chunk, "title", None) or (chunk.get("title") if isinstance(chunk, dict) else None)
            duration = getattr(chunk, "duration_minutes", None) or (chunk.get("duration_minutes") if isinstance(chunk, dict) else None)
            difficulty = getattr(chunk, "difficulty_weight", None)
            if difficulty is None and isinstance(chunk, dict):
                difficulty = chunk.get("difficulty_weight")
            deps = getattr(chunk, "dependencies", None)
            if deps is None and isinstance(chunk, dict):
                deps = chunk.get("dependencies", [])
            deadline = getattr(chunk, "deadline_hint", None)
            if deadline is None and isinstance(chunk, dict):
                deadline = chunk.get("deadline_hint")

            goal_id = _infer_goal_id_from_task_id(task_id) if task_id else None

            row = {
                "user_id": user_id,
                "plan_id": plan_id,
                "task_id": task_id,
                "title": title,
                "status": "pending",
                "duration_minutes": duration,
                "difficulty_weight": difficulty,
                "dependencies": deps,
                "deadline_hint": deadline,
                "created_at": now_iso,
            }
            if goal_id:
                row["goal_id"] = goal_id

            # --- Completion criteria ---
            cc = getattr(chunk, "completion_criteria", None)
            if cc is None and isinstance(chunk, dict):
                cc = chunk.get("completion_criteria")
            if cc:
                row["completion_criteria"] = cc

            # --- Implementation intention (WOOP) ---
            ii = getattr(chunk, "implementation_intention", None)
            if ii is None and isinstance(chunk, dict):
                ii = chunk.get("implementation_intention")
            if ii is not None:
                # Convert Pydantic model to dict if needed
                if hasattr(ii, "model_dump"):
                    ii = ii.model_dump()
                elif hasattr(ii, "dict"):
                    ii = ii.dict()
                row["implementation_intention"] = ii

            # --- Topic keywords ---
            tk = getattr(chunk, "topic_keywords", None)
            if tk is None and isinstance(chunk, dict):
                tk = chunk.get("topic_keywords")
            if tk:
                row["topic_keywords"] = tk

            # --- Scheduled start/end from OR-Tools schedule ---
            if schedule and task_id and task_id in schedule and horizon_dt:
                slot = schedule[task_id]
                # Support both "start"/"end" and "start_min"/"end_min" keys
                if isinstance(slot, dict):
                    start_min = slot.get("start_min") or slot.get("start")
                    end_min = slot.get("end_min") or slot.get("end")
                elif hasattr(slot, "start_min"):
                    start_min = slot.start_min
                    end_min = slot.end_min
                else:
                    start_min = None
                    end_min = None

                if start_min is not None and end_min is not None:
                    row["scheduled_start"] = (horizon_dt + timedelta(minutes=int(start_min))).isoformat()
                    row["scheduled_end"] = (horizon_dt + timedelta(minutes=int(end_min))).isoformat()

            rows.append(row)

        # Delete AFTER building rows — if row construction fails, existing tasks survive
        supabase_client.table("user_tasks").delete().eq(
            "user_id", user_id
        ).eq("status", "pending").execute()

        if rows:
            supabase_client.table("user_tasks").insert(rows).execute()
            print(f"[Control Policy] Persisted {len(rows)} fused tasks for user {user_id}")
            return plan_id
        return None
    except Exception as e:
        print(f"[Control Policy] Persist fused user_tasks failed: {e}")
        return None


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


def _needs_clarification(
    extraction: Optional[BrainDumpExtraction],
    user_prompt: str,
    conversation_history: list[dict] | None,
) -> Optional[ChatResponse]:
    """Return a clarification ChatResponse if the request is too ambiguous.

    Only triggers when:
    - Extraction is empty or None
    - No conversation history to resolve context
    - Prompt is very short (< 15 chars) or uses pronouns without antecedent
    """
    if conversation_history:
        return None  # Multi-turn context available

    if extraction and not _is_extraction_empty(extraction):
        return None  # Extraction succeeded

    prompt_lower = user_prompt.strip().lower()

    ambiguous_patterns = [
        "do it", "schedule it", "plan it", "ok", "yes", "sure",
        "go ahead", "the thing", "do the thing", "that",
    ]
    is_ambiguous = (
        len(prompt_lower) < 15
        and any(p in prompt_lower for p in ambiguous_patterns)
    )

    if not is_ambiguous:
        return None

    return ChatResponse(
        intent="CLARIFICATION",
        message="I'd like to help! Could you give me a bit more detail?",
        clarification_options=[
            "Plan my day",
            "Help me study for an exam",
            "I want to build a habit",
            "Break down a project into tasks",
        ],
    )


async def _run_brain_dump_extraction(
    user_prompt: str,
    conversation_history: list[dict] | None = None,
    system_prompt_override: str | None = None,
) -> Optional[BrainDumpExtraction]:
    """Extract all components from brain-dump prompt. Returns None on failure."""
    try:
        _slm_history = None
        if conversation_history:
            _slm_history = [
                {"role": m["role"], "content": m["content"][:200] + ("..." if len(m["content"]) > 200 else "")}
                for m in conversation_history[-4:]
            ]
        _system_prompt = system_prompt_override if system_prompt_override else BRAIN_DUMP_EXTRACTION_PROMPT
        result = await gemini_primary_route(
            user_prompt=user_prompt,
            system_prompt=_system_prompt,
            response_schema=BrainDumpExtraction,
            fallback_model=_cfg.SLM_ROUTER_MODEL,
            conversation_history=_slm_history,
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
    conversation_history: list[dict] | None = None,
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
            conversation_history=conversation_history,
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
        print(f"[Direct QA] Local model failed: {e}, falling back to Gemini...")
        from app.core.config import GEMINI_API_KEY
        if GEMINI_API_KEY:
            try:
                result = await hybrid_route_query(
                    user_prompt=user_prompt,
                    system_prompt=(
                        "You are Jarvis, an intelligent AI assistant. Answer the user's question clearly and helpfully. "
                        "If the question is about a concept, algorithm, or topic, explain it well with examples. "
                        "Be thorough but concise."
                    ),
                    response_schema=None,
                    force_cloud=True,
                    conversation_history=conversation_history,
                )
                msg = result if isinstance(result, str) else str(result)
                msg = msg.strip() if msg else "I couldn't generate a response."
                from app.services.analytical.voice_of_jarvis import _extract_thinking_process
                message, thinking_process = _extract_thinking_process(msg)
                return ChatResponse(
                    intent=IntentType.GENERAL_QA.value,
                    message=message,
                    thinking_process=thinking_process,
                )
            except Exception as gemini_err:
                print(f"[Direct QA] Gemini fallback also failed: {gemini_err}")
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
    conversation_history: list[dict] | None = None,
    draft_store: Optional[Any] = None,
    memory_store: Optional[Any] = None,
) -> ChatResponse:
    """Fallback: use single-intent classifier when extraction fails or is empty."""
    try:
        classify_result = await local_primary_route(
            user_prompt=user_prompt,
            system_prompt=UNIFIED_CLASSIFICATION_PROMPT,
            response_schema=IntentClassification,
            model_override=_cfg.SLM_ROUTER_MODEL,
        )
        if isinstance(classify_result, dict):
            classification = IntentClassification.model_validate(classify_result)
        else:
            classification = IntentClassification.model_validate_json(classify_result)
    except (ValidationError, json.JSONDecodeError) as e:
        print(f"[Fallback] Intent classification parse error: {e}")
        return await _direct_qa_response(
            user_prompt, progress_callback=progress_callback,
            conversation_history=conversation_history,
        )
    except Exception as e:
        print(f"[Fallback] Intent classification failed (model unavailable?): {e}")
        return ChatResponse(
            intent=IntentType.GENERAL_QA.value,
            message="I'm having trouble processing your request right now. Please try again in a moment.",
        )

    intent = classification.intent

    # Resolve which model will handle this intent
    from app.core.config import LOCAL_LLM_MODEL
    _fallback_model = _cfg.LOCAL_LLM_MODEL if intent == IntentType.GENERAL_QA else _cfg.SLM_ROUTER_MODEL
    if progress_callback:
        await progress_callback("intent_classified", {"intent": intent.value, "fallback": True, "model": _fallback_model})

    # --- Registry-based dispatch ---
    from app.schemas.context import IntentContext
    from app.services.intent_registry import intent_registry

    intent_key = str(intent.value) if hasattr(intent, "value") else str(intent)
    entry = intent_registry.get(intent_key)
    if entry is None:
        entry = intent_registry.get_or_fallback("CHAT")

    intent_ctx = IntentContext(
        user_id=user_id,
        user_prompt=user_prompt,
        db_client=db_client,
        progress_callback=progress_callback,
        conversation_history=conversation_history or [],
        draft_store=draft_store,
        memory_store=memory_store,
        extra={
            "day_start_hour_override": day_start_hour_override,
            "max_daily_deep_work_minutes": max_daily_deep_work_minutes,
            "min_daily_deep_work_minutes": min_daily_deep_work_minutes,
            "max_task_duration_minutes": max_task_duration_minutes,
            "min_task_duration_minutes": min_task_duration_minutes,
        },
    )

    result = await entry.handler(intent_ctx)

    # Convert dict result back to ChatResponse
    if isinstance(result, dict):
        return ChatResponse(**{k: v for k, v in result.items() if k in ChatResponse.model_fields})
    return result


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
    memory_store=None,
    extraction: Any = None,
    memory_context: str = "",
) -> ChatResponse:
    """Run PLAN_DAY pipeline: save habits, fetch habits, translate, decompose, schedule."""

    def _get_plan_memories():
        if not memory_store:
            return None
        try:
            active = memory_store.get_active_memories(user_id)
            return [
                {"memory_type": m.get("memory_type"), "content": m.get("content"),
                 "confidence": m.get("confidence", 0.5), "source": m.get("source", "inferred"),
                 "id": m.get("id")}
                for m in (active or [])[:20]
            ]
        except Exception:
            return None

    from app.core.config import LOCAL_LLM_MODEL as _LLM_27B
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    # Anti-Guilt auto-reschedule: scan for past-deadline pending tasks BEFORE planning.
    # Marks them `missed` so they don't poison TMT priority weights, surfaces a
    # blame-free framing in the synthesis.
    try:
        from app.services.analytical.missed_deadlines import detect_and_mark_missed, build_anti_guilt_message
        _overdue = await detect_and_mark_missed(user_id, supabase)
        if _overdue and execution_summary is not None:
            execution_summary["missed_deadlines"] = _overdue
            execution_summary["anti_guilt_message"] = build_anti_guilt_message(_overdue)
    except Exception as _e:
        logger.warning(f"Missed-deadline scan failed (non-fatal): {_e}")

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
    # Logical Day Fix: If it's before DAY_START_HOUR UTC, we are still in "yesterday's" schedule window
    if plan_start.hour < resolved_day_start:
        plan_date -= timedelta(days=1)
    horizon_start = datetime.combine(plan_date, time(resolved_day_start, 0), tzinfo=timezone.utc)

    # Safety: if horizon_start is more than 16 hours in the past, snap forward to today/tomorrow
    # This handles timezone mismatches where UTC date != user's local date
    past_minutes = max(0, int((plan_start - horizon_start).total_seconds() / 60))
    if past_minutes > 960:  # More than 16 hours ago — horizon is stale
        plan_date = plan_start.date()
        horizon_start = datetime.combine(plan_date, time(resolved_day_start, 0), tzinfo=timezone.utc)
        if horizon_start < plan_start:
            # DAY_START already passed today — start from tomorrow
            horizon_start = datetime.combine(plan_date + timedelta(days=1), time(resolved_day_start, 0), tzinfo=timezone.utc)
        past_minutes = max(0, int((plan_start - horizon_start).total_seconds() / 60))

    # Inject memory-derived constraints (PEARL patterns + explicit constraints)
    memory_constraints: list = []
    if memory_store:
        from app.services.memory.constraint_bridge import memories_to_constraints
        memory_constraints = await asyncio.to_thread(
            memories_to_constraints, user_id, memory_store
        )
        if memory_constraints:
            log_step(
                "P1_CONSTRAINT_BRIDGE",
                f"{len(memory_constraints)} constraints for user {user_id}",
                {"constraints": [f"{c.name}: {c.start_min}-{c.end_min} ({c.availability})" for c in memory_constraints]},
            )
        else:
            log_step("P1_CONSTRAINT_BRIDGE", "No memory constraints produced", {"user_id": user_id})
    else:
        log_step("P1_CONSTRAINT_BRIDGE", "Memory store unavailable — skipping constraint bridge", {"user_id": user_id})

    def _build_daily_context(horizon_minutes: int) -> list:
        ctx = expand_semantic_slots_to_time_slots(
            semantic_slots,
            horizon_minutes=horizon_minutes,
            plan_start=plan_start,
        )
        ctx.extend(memory_constraints)
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

    enriched_planning_goal = _build_planning_context(user_id, planning_goal, supabase, extraction=extraction)
    if max_task_duration_minutes or min_task_duration_minutes:
        parts = []
        if min_task_duration_minutes:
            parts.append(f"at least {min_task_duration_minutes} min")
        if max_task_duration_minutes:
            parts.append(f"at most {max_task_duration_minutes} min")
        constraint = "[Constraint: Each task must be " + " and ".join(parts) + ".] "
        enriched_planning_goal = constraint + enriched_planning_goal

    # Build decomposition inputs — memory context goes into system prompt, not user prompt
    decompose_input = enriched_planning_goal
    _decompose_system_prompt = SYSTEM_PROMPT
    if memory_context:
        _decompose_system_prompt = (
            SYSTEM_PROMPT
            + f"\n\nUser context from memory:\n{memory_context}"
        )

    async def _call_decompose(force_cloud: bool = False) -> dict:
        if force_cloud:
            # Explicit cloud retry (undersized decomposition fallback)
            result = await hybrid_route_query(
                user_prompt=decompose_input,
                system_prompt=_decompose_system_prompt,
                response_schema=ExecutionGraph,
                force_cloud=True,
                lenient_validation=True,
            )
        else:
            result = await gemini_primary_route(
                user_prompt=decompose_input,
                system_prompt=_decompose_system_prompt,
                response_schema=ExecutionGraph,
                fallback_model=_LLM_27B,
            )
        if isinstance(result, dict):
            return result
        sanitized = _sanitize_llm_json(result)
        return json.loads(sanitized)

    _decompose_start = time_mod.monotonic()

    # --- Decomposition cache check ---
    _decompose_cache_key = hashlib.sha256(decompose_input.encode()).hexdigest()
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
                memories=_get_plan_memories(),
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
            memories=_get_plan_memories(),
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
            memories=_get_plan_memories(),
        )

    goal_id = derive_goal_id(graph.goal_metadata, planning_goal)

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
            await progress_callback("synthesizing", {"model": _cfg.SLM_ROUTER_MODEL})
        # Skip VoJ when frontend will render structured schedule data directly.
        # VoJ was adding 5-10s for a message the user doesn't read when they can
        # see the visual schedule. Use deterministic fallback instead.
        message = "Here's your schedule."
        thinking_process = _build_thinking_fallback(summary)
        sched_dict = schedule_response.model_dump(mode='json')
        applied_constraints_data = [
            {"name": c.name, "start_min": c.start_min, "end_min": c.end_min,
             "availability": c.availability, "source": getattr(c, "source", "memory")}
            for c in memory_constraints
        ] if memory_constraints else None
        schedule_payload = SchedulePayload(
            schedule=sched_dict.get("schedule", {}),
            horizon_start=sched_dict.get("horizon_start", ""),
            horizon_minutes=used_horizon_minutes,
            daily_cap_minutes=sched_dict.get("daily_cap_minutes"),
            draft_id=draft.draft_id if draft else None,
            status="draft",
            applied_constraints=applied_constraints_data,
        )
        return ChatResponse(
            intent=IntentType.PLAN_DAY.value,
            message=message,
            schedule=schedule_payload,
            execution_graph=synthetic_graph.model_dump(mode='json'),
            action_proposals=action_proposals,
            search_result=search_result,
            suggested_action="replan" if summary.get("habits_saved") else None,
            thinking_process=thinking_process,
            schedule_status="draft",
            draft_id=draft.draft_id if draft else None,
            memories=_get_plan_memories(),
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
        memories=_get_plan_memories(),
    )


async def _run_schedule_modify_flow(
    user_prompt: str,
    user_id: str,
    draft_schedule: dict,
    db_client: Any,
    progress_callback: ProgressCallback = None,
    memory_store: Any = None,
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
            "model": _cfg.SLM_ROUTER_MODEL,
            "phase_summary": "Parsing your schedule change...",
        })

    # Parse the modification request
    modification = await parse_modification_request(user_prompt, decomposition)
    def _get_modify_memories():
        if not memory_store:
            return None
        try:
            active = memory_store.get_active_memories(user_id)
            return [
                {"memory_type": m.get("memory_type"), "content": m.get("content"),
                 "confidence": m.get("confidence", 0.5), "source": m.get("source", "inferred"),
                 "id": m.get("id")}
                for m in (active or [])[:20]
            ]
        except Exception:
            return None

    if modification is None:
        return ChatResponse(
            intent=IntentType.PLAN_DAY.value,
            message="I couldn't understand that modification. Could you rephrase? For example: 'make task 2 longer' or 'add a reading task'.",
            schedule=SchedulePayload(
                schedule=schedule_data,
                horizon_start=horizon_start_str or "",
                horizon_minutes=draft_schedule.get("horizon_minutes", 0),
                daily_cap_minutes=draft_schedule.get("daily_cap_minutes"),
                status="draft",
            ),
            execution_graph=execution_graph,
            schedule_status="draft",
            memories=_get_modify_memories(),
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
        schedule=SchedulePayload(
            schedule=modified_schedule if isinstance(modified_schedule, dict) else {},
            horizon_start=horizon_start.isoformat() if horizon_start else "",
            horizon_minutes=draft_schedule.get("horizon_minutes", 0),
            daily_cap_minutes=draft_schedule.get("daily_cap_minutes"),
            status="draft",
        ),
        execution_graph=modified_graph,
        schedule_status="draft",
        thinking_process=f"Applied {action_desc} modification to draft schedule.",
        memories=_get_modify_memories(),
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
    conversation_history: list[dict] | None = None,
    memory_context: str = "",
    memory_store=None,
) -> ChatResponse:
    """Master orchestrator: brain dump extraction, multi-execution, Voice of Jarvis."""

    def _get_response_memories():
        if not memory_store:
            return None
        try:
            active = memory_store.get_active_memories(user_id)
            return [
                {"memory_type": m.get("memory_type"), "content": m.get("content"),
                 "confidence": m.get("confidence", 0.5), "source": m.get("source", "inferred"),
                 "id": m.get("id")}
                for m in (active or [])[:20]
            ]
        except Exception:
            return None

    # Pre-check: if draft_schedule is provided, route to schedule modification flow
    if draft_schedule is not None:
        log_step("SCHEDULE_MODIFY", "Draft schedule provided, routing to modification flow")
        return await _run_schedule_modify_flow(
            user_prompt=user_prompt,
            user_id=user_id,
            draft_schedule=draft_schedule,
            db_client=db_client,
            progress_callback=progress_callback,
            memory_store=memory_store,
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

    # Build system prompt with memory context for brain dump extraction
    # Memory is injected into the system prompt to avoid contaminating the user message
    _brain_dump_system_prompt: str | None = None
    if memory_context:
        _brain_dump_system_prompt = (
            BRAIN_DUMP_EXTRACTION_PROMPT
            + f"\n\nUser context from memory:\n{memory_context}"
        )

    # Resolve model names for progress visibility
    from app.core.config import LOCAL_LLM_MODEL
    model_labels = {
        "auto": {"classifier": _cfg.SLM_ROUTER_MODEL, "reasoner": LOCAL_LLM_MODEL},
        "4b": {"classifier": _cfg.SLM_ROUTER_MODEL, "reasoner": _cfg.SLM_ROUTER_MODEL},
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
        _qa_resp = await _direct_qa_response(
            effective_prompt,
            model_override=LOCAL_LLM_MODEL,
            progress_callback=progress_callback,
            conversation_history=conversation_history,
        )
        _qa_resp.memories = _get_response_memories()
        return _qa_resp

    # Step 1: Brain dump extraction
    _pipeline_start = time_mod.monotonic()
    _phase_start = time_mod.monotonic()
    log_step("1_BRAIN_DUMP", "Extracting planning_goal, habits, action_items, search_queries")
    if progress_callback:
        await progress_callback("brain_dump_extraction", {
            "model": "gemini-2.5-flash",  # Primary model for brain dump
            "model_mode": model_mode,
            "started_at_ms": int(time_mod.time() * 1000),
        })
    extraction = await _run_brain_dump_extraction(
        effective_prompt,
        conversation_history=conversation_history,
        system_prompt_override=_brain_dump_system_prompt,
    )
    _extraction_duration = int((time_mod.monotonic() - _phase_start) * 1000)

    # Check if clarification is needed before proceeding
    clarification = _needs_clarification(extraction, effective_prompt, conversation_history)
    if clarification:
        if progress_callback:
            await progress_callback("intent_classified", {"intent": "CLARIFICATION"})
        clarification.memories = _get_response_memories()
        return clarification

    if extraction is None or _is_extraction_empty(extraction):
        _fallback_resp = await _fallback_single_intent(
            effective_prompt,
            user_id,
            db_client,
            day_start_hour_override=day_start_hour_override,
            max_daily_deep_work_minutes=max_daily_deep_work_minutes,
            min_daily_deep_work_minutes=min_daily_deep_work_minutes,
            max_task_duration_minutes=max_task_duration_minutes,
            min_task_duration_minutes=min_task_duration_minutes,
            progress_callback=progress_callback,
            conversation_history=conversation_history,
            draft_store=draft_store,
            memory_store=memory_store,
        )
        _fallback_resp.memories = _get_response_memories()
        return _fallback_resp

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
                _guard_resp = await _fallback_single_intent(
                    effective_prompt, user_id, db_client,
                    day_start_hour_override=day_start_hour_override,
                    max_daily_deep_work_minutes=max_daily_deep_work_minutes,
                    min_daily_deep_work_minutes=min_daily_deep_work_minutes,
                    max_task_duration_minutes=max_task_duration_minutes,
                    min_task_duration_minutes=min_task_duration_minutes,
                    progress_callback=progress_callback,
                    conversation_history=conversation_history,
                    draft_store=draft_store,
                    memory_store=memory_store,
                )
                _guard_resp.memories = _get_response_memories()
                return _guard_resp

    execution_summary: dict[str, Any] = {}
    # Inject memory context early so VoJ can personalize its response
    if memory_context:
        execution_summary["memory_context"] = memory_context
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
                staged = stage_habit(h.strip(), user_id)
                if staged is not None:  # filters fallback strings + trivially-short habits
                    staged_habits.append(staged)
                    print(f"[Memory] Inline habit staged: {h.strip()}")
                else:
                    print(f"[Memory] Inline habit rejected (fallback/too-short): {h.strip()!r}")
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
            memory_store=memory_store,
            extraction=extraction,
            memory_context=memory_context,
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
        memories=_get_response_memories(),
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

    # Load PEARL behavioral patterns as additional constraints
    pearl_constraint_slots: list[TimeSlot] = []
    try:
        from app.services.memory.store import MemoryStore
        from app.services.memory.constraint_bridge import memories_to_constraints
        memory_store = MemoryStore(supabase_client=supabase)
        pearl_constraint_slots = await asyncio.to_thread(
            memories_to_constraints, user_id, memory_store
        )
        if pearl_constraint_slots:
            log_step("REPLAN_PEARL", "Loaded PEARL constraint slots", {"count": len(pearl_constraint_slots)})
    except Exception as _pearl_err:
        log_step("REPLAN_PEARL_WARN", f"PEARL constraint load skipped: {_pearl_err}")

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

    # Build daily context (habit blocks + past time + PEARL pattern constraints)
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
        # Merge PEARL-inferred constraints after habit slots
        ctx.extend(pearl_constraint_slots)
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
        # Persist the updated schedule with timing data
        _persist_fused_tasks(
            user_id,
            pending,
            supabase,
            schedule=schedule_response.schedule if hasattr(schedule_response, "schedule") else None,
            horizon_start=horizon_start.isoformat() if horizon_start else None,
        )
        log_step("REPLAN_DONE", f"Background replan complete", {"reason": reason})
    else:
        log_step("REPLAN_INFEASIBLE", f"Background replan infeasible", {"reason": reason})

    return None
