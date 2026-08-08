"""Unified chat endpoint: single entry point for Control Policy orchestration."""

import asyncio
import json
import json as json_mod
import re
import time as time_mod
from collections.abc import AsyncGenerator
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.jarvis_logger import log_step
from app.schemas.context import ChatResponse
from app.services.analytical.control_policy import execute_agentic_flow
from app.services.analytical.voice_of_jarvis import (
    _build_thinking_fallback,
    _extract_thinking_process,
    _summary_capture,
    synthesize_jarvis_response_stream,
)
from app.models.brain.litellm_conf import hybrid_route_query

router = APIRouter()

NODE_TO_PHASE = {
    "load_context": "loading_context",
    "extract_brain_dump": "brain_dump_extraction",
    "classify_intent": "intent_classified",
    "planning_module": "planning",
    "research_agent": "researching",
    "coach_module": "coaching",
    "knowledge_module": "ingesting",
    "conversation_module": "responding",
    "synthesize_response": "synthesizing",
    "observation_loop": "learning",
}


class ChatRequest(BaseModel):
    """Request body for POST /chat."""

    user_prompt: str = Field(..., description="The user's message or goal")
    user_id: str = Field(..., description="User identifier for habits and storage")
    day_start_hour: Optional[int] = Field(
        default=None,
        ge=0,
        le=23,
        description="Override planning day start hour (0-23). Default from config.",
    )
    deadline_override: Optional[str] = Field(
        default=None,
        description="Manual deadline (ISO-8601 YYYY-MM-DD). Used for horizon if provided.",
    )
    max_daily_deep_work_minutes: Optional[int] = Field(
        default=None,
        ge=30,
        le=600,
        description="Cap on deep work per day; None uses adaptive formula.",
    )
    min_daily_deep_work_minutes: Optional[int] = Field(
        default=None,
        ge=15,
        le=240,
        description="Avoid days with less than X min; constrains spread.",
    )
    max_task_duration_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=25,
        description="Clamp per-chunk duration ceiling.",
    )
    min_task_duration_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=25,
        description="Clamp per-chunk duration floor.",
    )
    file_base64: Optional[str] = Field(
        default=None,
        description="Base64-encoded PDF or image. Combined with user_prompt for brain dump.",
    )
    media_type: Optional[str] = Field(
        default=None,
        description="One of 'pdf', 'image', 'png', 'jpeg'. Required when file_base64 is set.",
    )
    model_mode: Optional[str] = Field(
        default="auto",
        description="LLM routing mode: 'auto' (default pipeline routing), '4b' (force SLM), '27b' (force local 27B).",
    )
    file_name: Optional[str] = Field(
        default=None,
        description="Original filename of the uploaded file (for document management).",
    )
    confirm_before_schedule: Optional[bool] = Field(
        default=False,
        description="If true, pause after decomposition for user to review tasks before scheduling.",
    )
    draft_schedule: Optional[dict] = Field(
        default=None,
        description="Current draft schedule + execution_graph for modification. Keys: schedule, execution_graph, horizon_start.",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="Session ID for conversation continuity. If None, auto-creates a new session.",
    )


@router.post(
    "/",
    response_model=ChatResponse,
    summary="Unified chat endpoint",
    description=(
        "Single entry point: routes to ingestion or plan-day pipeline via SLM classification. "
        "Returns ChatResponse with intent, message, and pipeline-specific results (schedule, "
        "execution_graph, or ingestion_result)."
    ),
)
async def chat(request: ChatRequest, http_request: Request) -> ChatResponse:
    """Execute the agentic flow: classify intent, then run ingestion or plan-day pipeline."""
    log_step("REQUEST", "Chat received", {"user_id": request.user_id, "prompt_preview": request.user_prompt[:60]})
    db_client = getattr(http_request.app.state, "db_client", None)
    draft_store = getattr(http_request.app.state, "draft_store", None)
    memory_store = getattr(http_request.app.state, "memory_store", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    # Session management
    from app.services.chat_history import (
        get_or_create_session,
        save_user_message,
        save_assistant_message,
        build_context_messages,
    )

    session_id = await get_or_create_session(
        request.user_id, request.conversation_id, supabase
    )
    await save_user_message(session_id, request.user_id, request.user_prompt, supabase)
    conversation_history = await build_context_messages(
        session_id, request.user_id, max_messages=10, max_chars_per_message=500, supabase=supabase
    )

    # Inject archival memory into conversation context
    memory_context = ""
    if memory_store:
        from app.services.memory.retriever import build_memory_context
        memory_context = await asyncio.to_thread(build_memory_context, request.user_id, memory_store)

    response = await execute_agentic_flow(
        user_prompt=request.user_prompt,
        user_id=request.user_id,
        db_client=db_client,
        day_start_hour_override=request.day_start_hour,
        deadline_override=request.deadline_override,
        file_base64=request.file_base64,
        media_type=request.media_type,
        max_daily_deep_work_minutes=request.max_daily_deep_work_minutes,
        min_daily_deep_work_minutes=request.min_daily_deep_work_minutes,
        max_task_duration_minutes=request.max_task_duration_minutes,
        min_task_duration_minutes=request.min_task_duration_minutes,
        file_name=request.file_name,
        draft_schedule=request.draft_schedule,
        draft_store=draft_store,
        conversation_history=conversation_history,
        memory_context=memory_context,
        memory_store=memory_store,
    )

    # Save assistant response
    msg_id = await save_assistant_message(
        session_id, request.user_id, response.message, response.intent,
        {"schedule": response.schedule.model_dump(mode='json') if response.schedule else None, "draft_id": response.draft_id, "intent": response.intent},
        supabase,
    )

    # Fire-and-forget: extract memories from this turn
    if memory_store:
        from app.services.memory.extractor import safe_extract_memories
        asyncio.create_task(safe_extract_memories(
            request.user_id, request.user_prompt, response.message, memory_store,
            db_client=supabase,
        ))

    response.conversation_id = session_id
    response.message_id = msg_id
    return response


@router.post(
    "/stream",
    summary="Streaming chat endpoint (SSE)",
    description=(
        "SSE endpoint. Runs the full pipeline then streams Voice of Jarvis output token by token. "
        "Events: step (pipeline done), thinking (think-block tokens), message (response tokens), "
        "complete (full ChatResponse JSON)."
    ),
)
async def chat_stream(request: ChatRequest, http_request: Request) -> StreamingResponse:
    """Run pipeline, then stream Voice of Jarvis as SSE events."""
    log_step("STREAM_REQUEST", "Stream chat received", {"user_id": request.user_id})
    db_client = getattr(http_request.app.state, "db_client", None)
    draft_store = getattr(http_request.app.state, "draft_store", None)
    memory_store = getattr(http_request.app.state, "memory_store", None)
    model_mode = request.model_mode or "auto"
    # Frontend sends "4b" or "cloud" when user picks Gemini in the picker
    # (legacy: "4b" was the SLM label, now repurposed as cloud override).
    # We treat both as "force cloud Gemini for this turn".
    force_cloud_request = model_mode in ("4b", "cloud", "gemini")
    if force_cloud_request:
        # Set the per-request cloud override so every route_llm_call in this
        # request hits Gemini, regardless of LM Studio loaded models.
        try:
            from app.core.model_router import force_cloud_var
            force_cloud_var.set(True)
            print(f"[chat] model_mode={model_mode} → force_cloud=True (Gemini for this turn)")
        except Exception as _e:
            print(f"[chat] Failed to set force_cloud_var: {_e}")

    # --- Session management (before any early returns) ---
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    from app.services.chat_history import (
        get_or_create_session,
        save_user_message,
        save_assistant_message,
        build_context_messages,
    )

    session_id = await get_or_create_session(
        request.user_id, request.conversation_id, supabase
    )
    await save_user_message(session_id, request.user_id, request.user_prompt, supabase)
    conversation_history = await build_context_messages(
        session_id, request.user_id, max_messages=10, max_chars_per_message=500, supabase=supabase
    )

    # Inject archival memory into conversation context
    memory_context = ""
    if memory_store:
        from app.services.memory.retriever import build_memory_context
        memory_context = await asyncio.to_thread(build_memory_context, request.user_id, memory_store)

    # --- 27B DIRECT MODE: bypass pipeline, stream 27B directly ---
    if model_mode == "27b":
        return StreamingResponse(
            _stream_direct_27b(request, db_client, session_id=session_id, supabase=supabase, memory_store=memory_store),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # --- NORMAL PIPELINE MODE (auto / 4b) ---
    async def event_stream():
        from app.services.memory.extractor import safe_extract_memories

        # Fetch existing memories for frontend MemoryPanel
        _existing_memories = []
        if memory_store:
            _existing_memories = await asyncio.to_thread(
                memory_store.get_active_memories, request.user_id
            )

        # Progress queue: pipeline emits phase events, we yield them as SSE
        progress_queue: asyncio.Queue = asyncio.Queue()
        # Track the primary model used during pipeline (for final metrics)
        _primary_model: list[str] = []  # mutable container for closure

        async def progress_cb(phase: str, detail: dict | None = None):
            detail = detail or {}
            # Capture the model from heavyweight phases (decomposing, habits_translated)
            if detail.get("model") and phase in ("decomposing", "habits_translated", "decomposition_done"):
                _primary_model.clear()
                _primary_model.append(detail["model"])
            await progress_queue.put((phase, detail))

        # Step 1: Run full pipeline with _summary_capture + progress_callback
        captured_summary: dict = {}
        ctx_token = _summary_capture.set(captured_summary)
        pipeline_error: Exception | None = None
        partial: ChatResponse | None = None

        async def run_pipeline():
            nonlocal partial, pipeline_error
            try:
                partial = await execute_agentic_flow(
                    user_prompt=request.user_prompt,
                    user_id=request.user_id,
                    db_client=db_client,
                    day_start_hour_override=request.day_start_hour,
                    deadline_override=request.deadline_override,
                    file_base64=request.file_base64,
                    media_type=request.media_type,
                    max_daily_deep_work_minutes=request.max_daily_deep_work_minutes,
                    min_daily_deep_work_minutes=request.min_daily_deep_work_minutes,
                    max_task_duration_minutes=request.max_task_duration_minutes,
                    min_task_duration_minutes=request.min_task_duration_minutes,
                    progress_callback=progress_cb,
                    model_mode=model_mode,
                    skip_scheduling=request.confirm_before_schedule or False,
                    file_name=request.file_name,
                    draft_schedule=request.draft_schedule,
                    draft_store=draft_store,
                    conversation_history=conversation_history,
                    memory_context=memory_context,
                    memory_store=memory_store,
                )
            except Exception as exc:
                pipeline_error = exc
            finally:
                await progress_queue.put(("__done__", {}))

        pipeline_task = asyncio.create_task(run_pipeline())

        while True:
            phase, detail = await progress_queue.get()
            if phase == "__done__":
                break
            yield f"event: phase\ndata: {json.dumps({'phase': phase, **detail})}\n\n"

        _summary_capture.reset(ctx_token)

        if pipeline_error is not None:
            yield f"event: error\ndata: {json.dumps({'error': str(pipeline_error)})}\n\n"
            return
        if partial is None:
            yield f"event: error\ndata: {json.dumps({'error': 'Pipeline returned no result'})}\n\n"
            return

        import app.core.config as _cfg_v1
        from app.core.config import SLM_ROUTER_MODEL, LOCAL_LLM_MODEL
        try:
            from app.core.model_router import force_cloud_var as _fc_v1
            _v1_force_cloud = _fc_v1.get()
        except Exception:
            _v1_force_cloud = False
        if _cfg_v1.GEMINI_PRIMARY or _v1_force_cloud:
            _synthesis_model = _cfg_v1.GEMINI_MODEL
        else:
            _synthesis_model = LOCAL_LLM_MODEL if partial.intent == "GENERAL_QA" else SLM_ROUTER_MODEL
        yield f"event: step\ndata: {json.dumps({'intent': partial.intent, 'stage': 'pipeline_done', 'model_mode': model_mode, 'synthesis_model': _synthesis_model, 'force_cloud': _v1_force_cloud})}\n\n"

        if partial.awaiting_task_confirmation:
            partial_dict = partial.model_dump()
            # Save assistant response and attach session info
            _assistant_msg = partial.message or "Standing by, sir."
            _msg_id = await save_assistant_message(
                session_id, request.user_id, _assistant_msg, partial.intent or "",
                {},
                supabase,
            )
            partial_dict["conversation_id"] = session_id
            partial_dict["message_id"] = _msg_id
            # Fire-and-forget memory extraction
            if memory_store:
                _resp_text = partial.message or ""
                asyncio.create_task(safe_extract_memories(
                    request.user_id, request.user_prompt, _resp_text, memory_store,
                    db_client=supabase,
                ))
            if _existing_memories:
                partial_dict["memories"] = [
                    {"memory_type": m.get("memory_type"), "content": m.get("content"), "confidence": m.get("confidence", 0.5)}
                    for m in _existing_memories[:20]
                ]
            yield f"event: complete\ndata: {json.dumps(partial_dict)}\n\n"
            return

        # Step 2: For GENERAL_QA and GREETING, the pipeline already computed the
        # response (27B for QA, VoJ for greeting). Stream the 27B live for QA,
        # or emit the pre-computed message for greeting. VoJ streaming is only
        # for pipeline intents (PLAN_DAY, ingestion, habits, etc.).
        _is_qa = partial.intent in ("GENERAL_QA", "GREETING")

        if _is_qa and partial.intent == "GENERAL_QA":
            # Stream 27B response live (like direct 27B mode) for best UX
            _qa_start = time_mod.monotonic()
            _qa_ttft: float | None = None
            _qa_token_count = 0
            thinking_full = ""
            message_full = ""

            try:
                token_gen = await hybrid_route_query(
                    user_prompt=request.user_prompt,
                    system_prompt=(
                        "You are Jarvis, an intelligent AI assistant. Answer the user's question clearly and helpfully. "
                        "If the question is about a concept, algorithm, or topic, explain it well with examples. "
                        "Be thorough but concise."
                    ),
                    response_schema=None,
                    model_override=LOCAL_LLM_MODEL,
                    stream=True,
                )

                async for evt_type, tok in token_gen:
                    if _qa_ttft is None:
                        _qa_ttft = (time_mod.monotonic() - _qa_start) * 1000
                    _qa_token_count += 1
                    if evt_type == "reasoning":
                        thinking_full += tok
                        yield f"event: thinking\ndata: {json.dumps({'token': tok})}\n\n"
                    else:
                        message_full += tok
                        yield f"event: message\ndata: {json.dumps({'token': tok})}\n\n"
            except Exception as exc:
                print(f"[Stream] QA 27B streaming failed: {exc}")
                message_full = partial.message or "I encountered an error."
                yield f"event: message\ndata: {json.dumps({'token': message_full})}\n\n"

            _qa_total_s = time_mod.monotonic() - _qa_start
            message_clean = re.sub(r"</?think>", "", message_full, flags=re.IGNORECASE).strip()
            thinking_clean = re.sub(r"</?think>", "", thinking_full, flags=re.IGNORECASE).strip()

            # If no separate reasoning, try regex extraction
            if not thinking_clean and message_clean:
                message_clean, thinking_extracted = _extract_thinking_process(message_clean)
                thinking_clean = thinking_extracted or ""

            partial_dict = partial.model_dump()
            partial_dict["message"] = message_clean or partial.message or "Standing by, sir."
            partial_dict["thinking_process"] = thinking_clean or None
            partial_dict["generation_metrics"] = {
                "total_tokens": _qa_token_count,
                "total_time_s": round(_qa_total_s, 2),
                "tok_per_sec": round(_qa_token_count / _qa_total_s, 2) if _qa_total_s > 0 else 0,
                "ttft_ms": round(_qa_ttft, 1) if _qa_ttft is not None else None,
                "model": LOCAL_LLM_MODEL,
            }
            # Save assistant response and attach session info
            _assistant_msg = message_clean or partial.message or "Standing by, sir."
            _msg_id = await save_assistant_message(
                session_id, request.user_id, _assistant_msg, "GENERAL_QA",
                {},
                supabase,
            )
            partial_dict["conversation_id"] = session_id
            partial_dict["message_id"] = _msg_id
            # Fire-and-forget memory extraction
            if memory_store:
                _resp_text = message_clean or partial.message or ""
                asyncio.create_task(safe_extract_memories(
                    request.user_id, request.user_prompt, _resp_text, memory_store,
                    db_client=supabase,
                ))
            if _existing_memories:
                partial_dict["memories"] = [
                    {"memory_type": m.get("memory_type"), "content": m.get("content"), "confidence": m.get("confidence", 0.5)}
                    for m in _existing_memories[:20]
                ]
            yield f"event: complete\ndata: {json.dumps(partial_dict)}\n\n"
            return

        if _is_qa and partial.intent == "GREETING":
            # Greeting: re-stream through the LLM if pre-computed message is empty.
            # Never hardcode a greeting reply — user explicit constraint.
            if not partial.message:
                from app.modules.conversation import run_general_chat as _run_gc
                _gc_state = {"user_message": request.user_prompt, "user_model": None,
                             "memory_context": "", "conversation_history": []}
                _gc_out = await _run_gc(_gc_state)
                partial.message = _gc_out.get("response_message") or "Standing by, sir."
                partial.thinking_process = _gc_out.get("thinking_process") or partial.thinking_process

            if partial.thinking_process:
                yield f"event: thinking\ndata: {json.dumps({'token': partial.thinking_process})}\n\n"
            yield f"event: message\ndata: {json.dumps({'token': partial.message})}\n\n"
            partial_dict = partial.model_dump()
            partial_dict["generation_metrics"] = {
                "total_tokens": 1, "total_time_s": 0, "tok_per_sec": 0, "ttft_ms": None,
                "model": SLM_ROUTER_MODEL,
            }
            _assistant_msg = partial.message
            _msg_id = await save_assistant_message(
                session_id, request.user_id, _assistant_msg, "GREETING",
                {},
                supabase,
            )
            partial_dict["conversation_id"] = session_id
            partial_dict["message_id"] = _msg_id
            if memory_store:
                _resp_text = partial.message
                asyncio.create_task(safe_extract_memories(
                    request.user_id, request.user_prompt, _resp_text, memory_store,
                    db_client=supabase,
                ))
            if _existing_memories:
                partial_dict["memories"] = [
                    {"memory_type": m.get("memory_type"), "content": m.get("content"), "confidence": m.get("confidence", 0.5)}
                    for m in _existing_memories[:20]
                ]
            yield f"event: complete\ndata: {json.dumps(partial_dict)}\n\n"
            return

        # Skip VoJ for PLAN_DAY when schedule data exists — frontend renders it directly
        if partial.schedule and partial.intent == "PLAN_DAY":
            partial_dict = partial.model_dump()
            partial_dict["message"] = partial.message or "Here's your schedule."
            partial_dict["thinking_process"] = partial.thinking_process or _build_thinking_fallback(captured_summary)
            partial_dict["generation_metrics"] = {
                "total_tokens": 0, "total_time_s": 0, "tok_per_sec": 0, "ttft_ms": None,
                "model": "none (structured response)",
            }
            # Save assistant response and attach session info
            _assistant_msg = partial.message or "Here's your schedule."
            _msg_id = await save_assistant_message(
                session_id, request.user_id, _assistant_msg, "PLAN_DAY",
                {},
                supabase,
            )
            partial_dict["conversation_id"] = session_id
            partial_dict["message_id"] = _msg_id
            # Fire-and-forget memory extraction
            if memory_store:
                _resp_text = partial.message or "Here's your schedule."
                asyncio.create_task(safe_extract_memories(
                    request.user_id, request.user_prompt, _resp_text, memory_store,
                    db_client=supabase,
                ))
            if _existing_memories:
                partial_dict["memories"] = [
                    {"memory_type": m.get("memory_type"), "content": m.get("content"), "confidence": m.get("confidence", 0.5)}
                    for m in _existing_memories[:20]
                ]
            yield f"event: complete\ndata: {json.dumps(partial_dict)}\n\n"
            return

        # Step 2b: For pipeline intents (PLAN_DAY, ingestion, habits), stream VoJ
        # Guard: if pipeline already has a meaningful message (e.g. error, INFEASIBLE,
        # or decomposition failure) and captured_summary is empty, skip VoJ streaming
        # and use the pipeline's message directly.
        _pipeline_has_message = (
            partial.message
            and partial.message not in ("", "Standing by, sir.")
            and not captured_summary
        )

        _voj_start = time_mod.monotonic()
        _voj_ttft: float | None = None
        _voj_token_count = 0
        thinking_full = ""
        message_full = ""

        if _pipeline_has_message:
            # Pipeline computed its own message (error/fallback) — emit it directly
            if partial.thinking_process:
                yield f"event: thinking\ndata: {json.dumps({'token': partial.thinking_process})}\n\n"
                thinking_full = partial.thinking_process
            yield f"event: message\ndata: {json.dumps({'token': partial.message})}\n\n"
            message_full = partial.message
            _voj_token_count = 1
        else:
            try:
                async for event_type, tok in synthesize_jarvis_response_stream(captured_summary):
                    if _voj_ttft is None:
                        _voj_ttft = (time_mod.monotonic() - _voj_start) * 1000
                    _voj_token_count += 1
                    if event_type == "thinking":
                        thinking_full += tok
                    else:
                        message_full += tok
                    yield f"event: {event_type}\ndata: {json.dumps({'token': tok})}\n\n"
            except Exception as exc:
                print(f"[Stream] VoJ streaming failed: {exc}")
                fallback_thinking = _build_thinking_fallback(captured_summary)
                if fallback_thinking:
                    yield f"event: thinking\ndata: {json.dumps({'token': fallback_thinking})}\n\n"
                    thinking_full = fallback_thinking
                fallback_msg = partial.message or "Standing by, sir."
                yield f"event: message\ndata: {json.dumps({'token': fallback_msg})}\n\n"
                message_full = fallback_msg

        _voj_total_s = time_mod.monotonic() - _voj_start
        message_clean = re.sub(r"</?think>", "", message_full, flags=re.IGNORECASE).strip()
        thinking_clean = re.sub(r"</?think>", "", thinking_full, flags=re.IGNORECASE).strip()

        partial_dict = partial.model_dump()
        partial_dict["message"] = message_clean or partial.message or "Standing by, sir."
        partial_dict["thinking_process"] = thinking_clean or partial.thinking_process or _build_thinking_fallback(captured_summary)
        # Add generation metrics
        partial_dict["generation_metrics"] = {
            "total_tokens": _voj_token_count,
            "total_time_s": round(_voj_total_s, 2),
            "tok_per_sec": round(_voj_token_count / _voj_total_s, 2) if _voj_total_s > 0 else 0,
            "ttft_ms": round(_voj_ttft, 1) if _voj_ttft is not None else None,
            "model": _primary_model[0] if _primary_model else SLM_ROUTER_MODEL,
        }
        # Save assistant response and attach session info
        _assistant_msg = message_clean or partial.message or "Standing by, sir."
        _msg_id = await save_assistant_message(
            session_id, request.user_id, _assistant_msg, partial.intent or "",
            {},
            supabase,
        )
        partial_dict["conversation_id"] = session_id
        partial_dict["message_id"] = _msg_id
        # Fire-and-forget memory extraction
        if memory_store:
            _resp_text = message_clean or partial.message or ""
            asyncio.create_task(safe_extract_memories(
                request.user_id, request.user_prompt, _resp_text, memory_store,
                db_client=supabase,
            ))
        if _existing_memories:
            partial_dict["memories"] = [
                {"memory_type": m.get("memory_type"), "content": m.get("content"), "confidence": m.get("confidence", 0.5)}
                for m in _existing_memories[:20]
            ]
        yield f"event: complete\ndata: {json.dumps(partial_dict)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


async def _stream_direct_27b(request: "ChatRequest", db_client, session_id: str | None = None, supabase=None, memory_store=None) -> AsyncGenerator:
    """Stream 27B model response directly — no pipeline routing.

    Captures reasoning_content separately, tracks tok/sec, TTFT, token count.
    """
    from app.core.config import LOCAL_LLM_MODEL

    yield f"event: phase\ndata: {json.dumps({'phase': 'intent_classified', 'intent': 'GENERAL_QA', 'model': LOCAL_LLM_MODEL, 'model_mode': '27b', 'phase_summary': 'Direct 27B mode'})}\n\n"
    yield f"event: step\ndata: {json.dumps({'intent': 'GENERAL_QA', 'stage': 'pipeline_done', 'model_mode': '27b', 'synthesis_model': LOCAL_LLM_MODEL})}\n\n"

    _start = time_mod.monotonic()
    _ttft: float | None = None
    _token_count = 0
    thinking_full = ""
    message_full = ""

    try:
        token_gen = await hybrid_route_query(
            user_prompt=request.user_prompt,
            system_prompt=(
                "You are Jarvis, an intelligent AI assistant. Answer the user's question clearly and helpfully. "
                "If the question is about a concept, algorithm, or topic, explain it well with examples. "
                "Be thorough but concise."
            ),
            response_schema=None,
            model_override=LOCAL_LLM_MODEL,
            stream=True,
        )

        async for evt_type, tok in token_gen:
            if _ttft is None:
                _ttft = (time_mod.monotonic() - _start) * 1000
            _token_count += 1
            if evt_type == "reasoning":
                thinking_full += tok
                yield f"event: thinking\ndata: {json.dumps({'token': tok})}\n\n"
            else:
                message_full += tok
                yield f"event: message\ndata: {json.dumps({'token': tok})}\n\n"

    except Exception as exc:
        print(f"[Direct 27B] Streaming failed: {exc}")
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        return

    _total_s = time_mod.monotonic() - _start

    # Clean up
    message_clean = re.sub(r"</?think>", "", message_full, flags=re.IGNORECASE).strip()
    thinking_clean = re.sub(r"</?think>", "", thinking_full, flags=re.IGNORECASE).strip()

    # If no separate reasoning, try regex extraction
    if not thinking_clean:
        message_clean, thinking_extracted = _extract_thinking_process(message_clean or message_full)
        thinking_clean = thinking_extracted or ""

    # Save assistant response and attach session info
    _assistant_msg = message_clean or "Standing by, sir."
    _msg_id = None
    if session_id and supabase:
        from app.services.chat_history import save_assistant_message
        _msg_id = await save_assistant_message(
            session_id, request.user_id, _assistant_msg, "GENERAL_QA",
            {}, supabase,
        )

    # Fire-and-forget memory extraction
    if memory_store:
        from app.services.memory.extractor import safe_extract_memories
        asyncio.create_task(safe_extract_memories(
            request.user_id, request.user_prompt, _assistant_msg, memory_store,
            db_client=supabase,
        ))

    complete_data = {
        "intent": "GENERAL_QA",
        "message": _assistant_msg,
        "thinking_process": thinking_clean or None,
        "generation_metrics": {
            "total_tokens": _token_count,
            "total_time_s": round(_total_s, 2),
            "tok_per_sec": round(_token_count / _total_s, 2) if _total_s > 0 else 0,
            "ttft_ms": round(_ttft, 1) if _ttft is not None else None,
            "model": LOCAL_LLM_MODEL,
        },
        "conversation_id": session_id,
        "message_id": _msg_id,
    }
    yield f"event: complete\ndata: {json.dumps(complete_data)}\n\n"


class ConfirmScheduleRequest(BaseModel):
    """Request to schedule user-confirmed tasks."""

    user_id: str = Field(..., description="User identifier")
    tasks: list[dict] = Field(..., description="Edited TaskChunk dicts from frontend")
    goal_metadata: Optional[dict] = Field(default=None, description="Goal metadata from decomposition")
    day_start_hour: Optional[int] = Field(default=None, ge=0, le=23)
    deadline_override: Optional[str] = Field(default=None)
    max_daily_deep_work_minutes: Optional[int] = Field(default=None, ge=30, le=600)
    min_daily_deep_work_minutes: Optional[int] = Field(default=None, ge=15, le=240)
    max_task_duration_minutes: Optional[int] = Field(default=None, ge=1, le=25)
    min_task_duration_minutes: Optional[int] = Field(default=None, ge=1, le=25)


@router.post(
    "/confirm-schedule",
    summary="Schedule user-confirmed tasks (SSE)",
    description="After user reviews and edits decomposed tasks, run scheduling + Voice of Jarvis.",
)
async def confirm_schedule_stream(
    request: ConfirmScheduleRequest, http_request: Request
) -> StreamingResponse:
    """Run Phase 2: scheduling + VoJ on user-confirmed tasks."""
    from app.api.v1.endpoints.reasoning import ExecutionGraph, TaskChunk
    from app.api.v1.endpoints.schedule import run_schedule
    from app.core.config import (
        DAY_START_HOUR,
        DEFAULT_HORIZON_MINUTES,
        MAX_HORIZON_MINUTES,
        SLM_ROUTER_MODEL,
    )
    from app.services.analytical.habit_translator import translate_habits_to_slots
    from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots
    from app.services.extraction.behavioral_store import get_behavioral_context_for_calendar

    log_step("CONFIRM_SCHEDULE", "Scheduling user-confirmed tasks", {"user_id": request.user_id, "task_count": len(request.tasks)})
    db_client = getattr(http_request.app.state, "db_client", None)

    async def event_stream():
        import time as time_mod
        from datetime import datetime, timedelta, time, timezone

        supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

        # Parse tasks from frontend — clamp user-edited values to valid TaskChunk ranges
        def _clamp_task(t: dict) -> dict:
            out = dict(t)
            if "duration_minutes" in out:
                out["duration_minutes"] = max(1, min(60, int(out.get("duration_minutes", 25))))
            if "difficulty_weight" in out:
                out["difficulty_weight"] = max(0.0, min(1.0, float(out.get("difficulty_weight", 0.5))))
            return out

        try:
            task_chunks = [TaskChunk(**_clamp_task(t)) for t in request.tasks]
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'error': f'Invalid tasks: {exc}'})}\n\n"
            return

        yield f"event: phase\ndata: {json.dumps({'phase': 'confirming', 'task_count': len(task_chunks)})}\n\n"

        try:
            # Fetch habits and translate
            _sched_start = time_mod.monotonic()
            habits = ""
            try:
                habits = await get_behavioral_context_for_calendar(request.user_id, supabase)
            except Exception as exc:
                print(f"[ConfirmSchedule] habits fetch failed (non-fatal): {exc}")
            semantic_slots = []
            if habits and habits.strip():
                yield f"event: phase\ndata: {json.dumps({'phase': 'habits_translated', 'phase_summary': 'Translating habits for scheduling...'})}\n\n"
                try:
                    semantic_slots = await translate_habits_to_slots(habits)
                except Exception as exc:
                    print(f"[ConfirmSchedule] habit translation failed (non-fatal): {exc}")

            # Time context
            resolved_day_start = request.day_start_hour or DAY_START_HOUR
            plan_start = datetime.now(timezone.utc)
            plan_date = plan_start.date()
            if plan_start.hour < resolved_day_start:
                plan_date -= timedelta(days=1)
            horizon_start = datetime.combine(plan_date, time(resolved_day_start, 0), tzinfo=timezone.utc)
            # Safety: snap forward if horizon_start is stale (timezone mismatch)
            past_min = max(0, int((plan_start - horizon_start).total_seconds() / 60))
            if past_min > 960:
                plan_date = plan_start.date()
                horizon_start = datetime.combine(plan_date, time(resolved_day_start, 0), tzinfo=timezone.utc)
                if horizon_start < plan_start:
                    horizon_start = datetime.combine(plan_date + timedelta(days=1), time(resolved_day_start, 0), tzinfo=timezone.utc)

            expanded_slots = expand_semantic_slots_to_time_slots(
                semantic_slots,
                horizon_minutes=DEFAULT_HORIZON_MINUTES,
                plan_start=plan_start,
            )

            # Build execution graph
            goal_meta = request.goal_metadata or {}
            avg_load = sum(t.difficulty_weight for t in task_chunks) / max(len(task_chunks), 1)
            synthetic_graph = ExecutionGraph(
                goal_metadata=goal_meta,
                decomposition=task_chunks,
                cognitive_load_estimate={"intrinsic_load": round(avg_load, 3)},
            )

            yield f"event: phase\ndata: {json.dumps({'phase': 'scheduling', 'task_count': len(task_chunks), 'started_at_ms': int(time_mod.time() * 1000)})}\n\n"

            # Schedule
            schedule_response = None
            used_horizon = DEFAULT_HORIZON_MINUTES
            horizon_steps = [DEFAULT_HORIZON_MINUTES, 4320, 7200, 10080]
            for horizon_min in horizon_steps:
                if horizon_min > MAX_HORIZON_MINUTES:
                    break
                try:
                    schedule_response = run_schedule(
                        synthetic_graph,
                        expanded_slots,
                        horizon_minutes=horizon_min,
                        horizon_start=horizon_start,
                        max_daily_deep_work_minutes=request.max_daily_deep_work_minutes,
                        min_daily_deep_work_minutes=request.min_daily_deep_work_minutes,
                        max_task_duration_minutes=request.max_task_duration_minutes,
                        min_task_duration_minutes=request.min_task_duration_minutes,
                    )
                    used_horizon = horizon_min
                    break
                except Exception:
                    continue

            _sched_dur = int((time_mod.monotonic() - _sched_start) * 1000)

            if schedule_response is None:
                yield f"event: error\ndata: {json.dumps({'error': 'Could not find a feasible schedule. Try reducing scope or relaxing constraints.'})}\n\n"
                return

            _task_count = len(schedule_response.schedule) if hasattr(schedule_response, 'schedule') else 0
            yield f"event: phase\ndata: {json.dumps({'phase': 'schedule_done', 'status': 'OPTIMAL', 'task_count': _task_count, 'horizon_hours': round(used_horizon / 60, 1), 'duration_ms': _sched_dur})}\n\n"

            # Defer persistence — schedule returned as draft for user acceptance
            # Tasks are persisted via POST /chat/accept-schedule

            # Voice of Jarvis synthesis
            execution_summary = {"schedule_generated": True}
            if used_horizon > DEFAULT_HORIZON_MINUTES:
                execution_summary["spread_across_days"] = True

            try:
                from app.core.model_router import force_cloud_var as _fc_pd
                _pd_force_cloud = _fc_pd.get()
            except Exception:
                _pd_force_cloud = False
            import app.core.config as _cfg_pd
            _pd_synth = _cfg_pd.GEMINI_MODEL if (_cfg_pd.GEMINI_PRIMARY or _pd_force_cloud) else SLM_ROUTER_MODEL
            yield f"event: step\ndata: {json.dumps({'intent': 'PLAN_DAY', 'stage': 'pipeline_done', 'synthesis_model': _pd_synth, 'force_cloud': _pd_force_cloud})}\n\n"

            # Step 2: Stream Voice of Jarvis
            captured_summary = execution_summary
            ctx_token = _summary_capture.set(captured_summary)
            thinking_full = ""
            message_full = ""
            try:
                async for event_type, tok in synthesize_jarvis_response_stream(captured_summary):
                    if event_type == "thinking":
                        thinking_full += tok
                    else:
                        message_full += tok
                    yield f"event: {event_type}\ndata: {json.dumps({'token': tok})}\n\n"
            except Exception as exc:
                print(f"[ConfirmSchedule] VoJ streaming failed: {exc}")
                fallback_thinking = _build_thinking_fallback(captured_summary)
                if fallback_thinking:
                    yield f"event: thinking\ndata: {json.dumps({'token': fallback_thinking})}\n\n"
                    thinking_full = fallback_thinking
                message_full = "Here's your schedule."
                yield f"event: message\ndata: {json.dumps({'token': message_full})}\n\n"

            _summary_capture.reset(ctx_token)

            # Clean and emit complete
            message_clean = re.sub(r"</?think>", "", message_full, flags=re.IGNORECASE).strip()
            thinking_clean = re.sub(r"</?think>", "", thinking_full, flags=re.IGNORECASE).strip()

            complete_data = {
                "intent": "PLAN_DAY",
                "message": message_clean or "Here's your schedule.",
                "schedule": schedule_response.model_dump(mode='json') if hasattr(schedule_response, "model_dump") else schedule_response,
                "execution_graph": synthetic_graph.model_dump(mode='json') if hasattr(synthetic_graph, "model_dump") else None,
                "thinking_process": thinking_clean or _build_thinking_fallback(captured_summary),
                "awaiting_task_confirmation": False,
                "schedule_status": "draft",
            }
            yield f"event: complete\ndata: {json.dumps(complete_data)}\n\n"

        except Exception as exc:
            print(f"[ConfirmSchedule] Unhandled error in event_stream: {exc}")
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


class AcceptScheduleRequest(BaseModel):
    """Request to persist a draft schedule."""

    user_id: str = Field(..., description="User identifier")
    tasks: list[dict] = Field(..., description="TaskChunk dicts from accepted draft")
    goal_metadata: Optional[dict] = Field(default=None, description="Goal metadata")
    schedule: Optional[dict] = Field(default=None, description="Schedule map from OR-Tools {task_id: {start_min, end_min}}")
    horizon_start: Optional[str] = Field(default=None, description="ISO-8601 horizon start for computing wall-clock times")


@router.post(
    "/accept-schedule",
    summary="Accept and persist a draft schedule",
    description="Called when user accepts a draft schedule. Persists tasks to Supabase.",
)
async def accept_schedule(request: AcceptScheduleRequest, http_request: Request):
    """Persist accepted draft schedule tasks to Supabase."""
    from app.api.v1.endpoints.reasoning import TaskChunk
    from app.services.analytical.control_policy import _persist_fused_tasks

    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    try:
        task_chunks = [TaskChunk(**t) for t in request.tasks]
    except Exception as exc:
        return {"status": "error", "message": f"Invalid tasks: {exc}", "task_count": 0}

    _persist_fused_tasks(
        request.user_id, task_chunks, supabase,
        schedule=request.schedule.get("schedule") if request.schedule else None,
        horizon_start=request.horizon_start,
    )

    # Fire-and-forget: detect behavioral patterns from task history
    memory_store = getattr(http_request.app.state, "memory_store", None)
    if memory_store:
        from app.services.memory.pearl import detect_patterns
        _sb = getattr(db_client, "supabase", db_client) if db_client else None
        if _sb:
            asyncio.create_task(asyncio.to_thread(
                detect_patterns, request.user_id, _sb, memory_store
            ))

    return {"status": "accepted", "task_count": len(task_chunks)}


class ModifyScheduleRequest(BaseModel):
    """Request to modify a draft schedule via chat."""

    user_id: str = Field(..., description="User identifier")
    user_prompt: str = Field(..., description="Natural language modification request")
    current_tasks: list[dict] = Field(..., description="Current task list from draft")
    execution_graph: Optional[dict] = Field(default=None, description="Current execution graph")
    current_schedule: Optional[dict] = Field(default=None, description="Current schedule data from draft")
    horizon_start: Optional[str] = Field(default=None, description="Schedule horizon start ISO")


@router.post(
    "/modify-schedule",
    summary="Modify draft schedule via chat",
    description="Parse a natural language modification and apply it to the draft schedule.",
)
async def modify_schedule(request: ModifyScheduleRequest, http_request: Request):
    """Parse modification, apply to tasks, re-schedule."""
    from app.services.analytical.schedule_modifier import (
        parse_modification_request,
        apply_modification,
    )
    from app.services.analytical.habit_translator import translate_habits_to_slots
    from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots
    from app.services.extraction.behavioral_store import get_behavioral_context_for_calendar
    from app.core.config import DAY_START_HOUR, DEFAULT_HORIZON_MINUTES
    from datetime import datetime, timedelta, time, timezone

    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    # Parse the modification intent
    modification = await parse_modification_request(
        user_prompt=request.user_prompt,
        task_list=request.current_tasks,
    )
    if not modification:
        return {"status": "error", "message": "Could not understand the modification. Try being more specific."}

    # Build time slots for re-scheduling
    habits = ""
    try:
        habits = await get_behavioral_context_for_calendar(request.user_id, supabase)
    except Exception:
        pass
    semantic_slots = []
    if habits and habits.strip():
        try:
            semantic_slots = await translate_habits_to_slots(habits)
        except Exception:
            pass

    plan_start = datetime.now(timezone.utc)
    plan_date = plan_start.date()
    if plan_start.hour < DAY_START_HOUR:
        plan_date -= timedelta(days=1)
    horizon_start = datetime.combine(plan_date, time(DAY_START_HOUR, 0), tzinfo=timezone.utc)
    # Safety: snap forward if horizon_start is stale (timezone mismatch)
    _past = max(0, int((plan_start - horizon_start).total_seconds() / 60))
    if _past > 960:
        plan_date = plan_start.date()
        horizon_start = datetime.combine(plan_date, time(DAY_START_HOUR, 0), tzinfo=timezone.utc)
        if horizon_start < plan_start:
            horizon_start = datetime.combine(plan_date + timedelta(days=1), time(DAY_START_HOUR, 0), tzinfo=timezone.utc)
    if request.horizon_start:
        try:
            horizon_start = datetime.fromisoformat(request.horizon_start)
        except ValueError:
            pass

    expanded_slots = expand_semantic_slots_to_time_slots(
        semantic_slots,
        horizon_minutes=DEFAULT_HORIZON_MINUTES,
        plan_start=plan_start,
    )

    # Build execution graph dict
    eg = request.execution_graph or {"decomposition": request.current_tasks, "goal_metadata": {}}

    # Apply modification and re-schedule
    updated_graph, updated_schedule = apply_modification(
        modification=modification,
        execution_graph=eg,
        schedule_data=request.current_schedule or {"schedule": {}},
        time_slots=expanded_slots,
        horizon_start=horizon_start,
    )

    return {
        "status": "modified",
        "modification": modification.model_dump(),
        "execution_graph": updated_graph,
        "schedule": updated_schedule,
        "schedule_status": "draft",
    }


@router.post("/v2/stream")
async def chat_stream_v2(request: ChatRequest, http_request: Request):
    """SSE endpoint using LangGraph orchestrator. Same SSE contract as /chat/stream."""
    from app.core.user_model import UserModel
    from app.orchestrator.checkpoint import make_thread_id
    from app.orchestrator.state import ConversationPhase, NegotiationPhase
    from app.services.chat_history import (
        get_or_create_session,
        save_user_message,
        save_assistant_message,
        build_context_messages,
    )

    jarvis_graph = http_request.app.state.jarvis_graph
    db_client = getattr(http_request.app.state, "db_client", None)
    memory_store = getattr(http_request.app.state, "memory_store", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    # Model selector: when user picks Gemini, force this whole turn to cloud.
    # MUST be set BEFORE the graph is invoked so every LLM call inherits it.
    model_mode = request.model_mode or "auto"
    force_cloud_request = model_mode in ("4b", "cloud", "gemini")
    if force_cloud_request:
        try:
            from app.core.model_router import force_cloud_var
            force_cloud_var.set(True)
            print(f"[chat/v2] model_mode={model_mode} → force_cloud=True (Gemini for this turn)")
        except Exception as _e:
            print(f"[chat/v2] Failed to set force_cloud_var: {_e}")

    # --- Session management ---
    session_id = await get_or_create_session(
        request.user_id, request.conversation_id, supabase
    )
    await save_user_message(session_id, request.user_id, request.user_prompt, supabase)
    conversation_history = await build_context_messages(
        session_id, request.user_id, max_messages=10, max_chars_per_message=500, supabase=supabase
    )

    # Inject archival memory
    memory_context = ""
    if memory_store:
        from app.services.memory.retriever import build_memory_context
        memory_context = await asyncio.to_thread(build_memory_context, request.user_id, memory_store)

    # Fetch existing memories for frontend MemoryPanel
    _existing_memories: list[dict] = []
    if memory_store:
        _existing_memories = await asyncio.to_thread(
            memory_store.get_active_memories, request.user_id
        )

    user_model = UserModel(user_id=request.user_id, db=db_client)
    if memory_store:
        user_model.set_memory_store(memory_store)

    # --- Progress bridge: sub-graph callbacks → SSE phase events ---
    progress_queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(phase, **detail):
        progress_queue.put_nowait(json_mod.dumps({"phase": phase, **detail}))

    initial_state = {
        # Serializable identity — survives checkpointing; user_model does not.
        # The facade is still passed here (shared db_client / memory_store wiring);
        # _load_context rebuilds it from user_id only on checkpoint-resumed turns.
        "user_id": request.user_id,
        "user_model": user_model,
        "user_message": request.user_prompt,
        "file_base64": request.file_base64,
        "file_media_type": request.media_type,
        "file_name": request.file_name,
        "brain_dump": None,
        "intent": None,
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        # Live store in, draft id out: planning's create_draft step fills
        # draft_id, which the SSE payload below reports to the UI.
        "draft_store": getattr(http_request.app.state, "draft_store", None),
        "draft_id": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "response_message": None,
        # conversation_phase / negotiation_state are deliberately absent: on a
        # thread that already has a checkpoint they must come from it, or every
        # turn resets the negotiation to NONE and the negotiation shortcut in
        # routing.py can never fire. Seeded below only for a brand-new thread.
        "modules_invoked": [],
        "needs_followup": False,
        "needs_consent": None,
        "error": None,
        "conversation_history": conversation_history,
        "memory_context": memory_context,
        "progress_callback": progress_cb,
        "progress_queue": progress_queue,
        # Per-turn flag, cleared explicitly: it is checkpointed now, and the
        # negotiation shortcut bypasses extract_brain_dump (which otherwise
        # re-decides it), so a stale True would mute the observation loop.
        "trivial_input": None,
        # When user picks Gemini in the model selector, force every LLM call
        # in this turn to route to cloud — not local Gemma.
        "force_cloud_request": force_cloud_request,
    }

    # One checkpoint thread per (user, session). The user prefix matters:
    # _load_context rebuilds the UserModel facade from the checkpointed user_id,
    # so a session-only key would let a resumed turn rebuild another user's facade.
    graph_config = {"configurable": {"thread_id": make_thread_id(request.user_id, session_id)}}

    try:
        _existing = await jarvis_graph.aget_state(graph_config)
        _resumed = bool(_existing and getattr(_existing, "values", None))
    except Exception as _e:
        print(f"[chat/v2] Checkpoint lookup failed ({_e}) — starting a fresh thread")
        _resumed = False
    if not _resumed:
        initial_state["conversation_phase"] = ConversationPhase.GREETING
        initial_state["negotiation_state"] = NegotiationPhase.NONE

    async def event_gen():
        import app.core.config as _cfg
        LOCAL_LLM_MODEL = _cfg.LOCAL_LLM_MODEL
        SLM_ROUTER_MODEL = _cfg.SLM_ROUTER_MODEL

        def _intent_str(intent) -> str:
            """Safely convert IntentType enum or string to plain string."""
            if hasattr(intent, "value"):
                return intent.value
            return str(intent)

        try:
            final_state = {}
            async for event in jarvis_graph.astream(initial_state, config=graph_config):
                # Drain progress queue — sub-graph phase events
                while not progress_queue.empty():
                    try:
                        raw = progress_queue.get_nowait()
                        try:
                            parsed = json_mod.loads(raw)
                            evt_type = parsed.pop("_event_type", "phase")
                        except (json_mod.JSONDecodeError, TypeError):
                            evt_type = "phase"
                            parsed = None
                        if evt_type == "tool_use":
                            yield f"event: tool_use\ndata: {json_mod.dumps(parsed)}\n\n"
                        elif evt_type == "memory_extracted":
                            yield f"event: memory_extracted\ndata: {json_mod.dumps(parsed)}\n\n"
                        elif evt_type == "pattern_detected":
                            yield f"event: pattern_detected\ndata: {json_mod.dumps(parsed)}\n\n"
                        else:
                            yield f"event: phase\ndata: {raw}\n\n"
                    except asyncio.QueueEmpty:
                        break

                node_name = list(event.keys())[0]
                node_state = event[node_name]
                if not isinstance(node_state, dict):
                    node_state = {}  # normalize None / non-dict to empty dict
                final_state.update(node_state)

                from app.utils.spinner_verbs import get_spinner_verb

                phase = NODE_TO_PHASE.get(node_name)
                if phase:
                    phase_detail = {
                        "phase": phase,
                        "verb": get_spinner_verb(phase),
                    }
                    # Enrich with node-specific detail
                    # _existing_memories is captured from enclosing scope via closure
                    # (defined before event_gen — safe)
                    if node_name == "load_context":
                        conv_hist = initial_state.get("conversation_history") or []
                        phase_detail["detail"] = {
                            "memories_count": len(_existing_memories),
                            "conversation_turns": len(conv_hist),
                        }
                    elif node_name == "classify_intent" and node_state.get("intent"):
                        phase_detail["detail"] = {
                            "intent": _intent_str(node_state["intent"]),
                            "method": "rule-based",
                        }
                    elif node_name in ("planning_module", "research_agent", "coach_module", "knowledge_module", "conversation_module"):
                        phase_detail["detail"] = {
                            "module": node_name,
                        }
                    elif node_name == "observation_loop":
                        phase_detail["detail"] = {
                            "memories_extracted": node_state.get("memories_extracted_count", 0),
                            "patterns_detected": node_state.get("patterns_detected_count", 0),
                        }
                    yield f"event: phase\ndata: {json_mod.dumps(phase_detail)}\n\n"

                if node_name == "classify_intent" and node_state.get("intent"):
                    yield f"event: step\ndata: {json_mod.dumps({'intent': _intent_str(node_state['intent']), 'stage': 'intent_classified'})}\n\n"

                # Emit consent_request if a hook asked for consent
                if node_state.get("needs_consent"):
                    yield f"event: consent_request\ndata: {json_mod.dumps({'reason': node_state.get('response_message', ''), 'module': node_name})}\n\n"

                if node_name == "observation_loop":
                    if node_state.get("memories_extracted"):
                        yield f"event: memory_extracted\ndata: {json_mod.dumps(node_state['memories_extracted'])}\n\n"
                    if node_state.get("patterns_detected"):
                        yield f"event: pattern_detected\ndata: {json_mod.dumps(node_state['patterns_detected'])}\n\n"

            # Drain any remaining progress events
            while not progress_queue.empty():
                try:
                    raw = progress_queue.get_nowait()
                    try:
                        parsed = json_mod.loads(raw)
                        evt_type = parsed.pop("_event_type", "phase")
                    except (json_mod.JSONDecodeError, TypeError):
                        evt_type = "phase"
                        parsed = None
                    if evt_type == "tool_use":
                        yield f"event: tool_use\ndata: {json_mod.dumps(parsed)}\n\n"
                    elif evt_type == "memory_extracted":
                        yield f"event: memory_extracted\ndata: {json_mod.dumps(parsed)}\n\n"
                    elif evt_type == "pattern_detected":
                        yield f"event: pattern_detected\ndata: {json_mod.dumps(parsed)}\n\n"
                    else:
                        yield f"event: phase\ndata: {raw}\n\n"
                except asyncio.QueueEmpty:
                    break

            _intent = _intent_str(final_state.get("intent", "CHAT"))
            _is_chat = _intent in ("CHAT", "GREETING", "GENERAL_QA")
            _has_schedule = bool(final_state.get("schedule"))

            # Reflect actual routing: per-request force_cloud overrides static config.
            try:
                from app.core.model_router import force_cloud_var
                _force_cloud_now = force_cloud_var.get()
            except Exception:
                _force_cloud_now = False
            _active_model = _cfg.GEMINI_MODEL if (_cfg.GEMINI_PRIMARY or _force_cloud_now) else LOCAL_LLM_MODEL
            yield f"event: step\ndata: {json_mod.dumps({'intent': _intent, 'stage': 'pipeline_done', 'model_mode': model_mode, 'synthesis_model': _active_model, 'force_cloud': _force_cloud_now})}\n\n"

            # --- Token streaming (matches v1/stream SSE contract) ---
            _start = time_mod.monotonic()
            _ttft: float | None = None
            _token_count = 0
            thinking_full = ""
            message_full = ""
            _stream_model = _cfg.GEMINI_MODEL if _cfg.GEMINI_PRIMARY else LOCAL_LLM_MODEL

            if _has_schedule and _intent == "PLAN_DAY":
                # Schedule responses: emit pre-computed message, no VoJ streaming needed
                _pre_msg = final_state.get("response_message") or "Here's your schedule."
                _pre_think = final_state.get("thinking_process")
                if _pre_think:
                    yield f"event: thinking\ndata: {json.dumps({'token': _pre_think})}\n\n"
                    thinking_full = _pre_think
                yield f"event: message\ndata: {json.dumps({'token': _pre_msg})}\n\n"
                message_full = _pre_msg
                _token_count = 1
                _stream_model = "none (structured response)"

            elif _is_chat:
                # CHAT/GREETING/GENERAL_QA: stream LLM response live for typing effect
                _pre_msg = final_state.get("response_message", "")
                _pre_think = final_state.get("thinking_process")

                if _pre_msg:
                    # Graph already computed the response — stream it word-by-word for UX
                    if _pre_think:
                        yield f"event: thinking\ndata: {json.dumps({'token': _pre_think})}\n\n"
                        thinking_full = _pre_think
                    yield f"event: message\ndata: {json.dumps({'token': _pre_msg})}\n\n"
                    message_full = _pre_msg
                    _token_count = 1
                else:
                    # Empty graph output for CHAT — last-resort: invoke conversation module
                    # directly so the LLM still produces the reply (no hardcoded text).
                    from app.modules.conversation import run_general_chat as _run_gc
                    _gc_out = await _run_gc({
                        "user_message": request.user_prompt,
                        "user_model": final_state.get("user_model"),
                        "memory_context": final_state.get("memory_context", ""),
                        "conversation_history": final_state.get("conversation_history", []),
                    })
                    _llm_msg = _gc_out.get("response_message") or "Standing by, sir."
                    _llm_think = _gc_out.get("thinking_process")
                    if _llm_think:
                        yield f"event: thinking\ndata: {json.dumps({'token': _llm_think})}\n\n"
                        thinking_full = _llm_think
                    yield f"event: message\ndata: {json.dumps({'token': _llm_msg})}\n\n"
                    message_full = _llm_msg
                    _token_count = 1

            else:
                # Pipeline intents (ingestion, behavioral, etc.): stream VoJ synthesis
                _pre_msg = final_state.get("response_message", "")
                _pre_think = final_state.get("thinking_process")

                if _pre_msg and _pre_msg not in ("", "Standing by, sir."):
                    # VoJ already computed in synthesize_response node — emit it
                    if _pre_think:
                        yield f"event: thinking\ndata: {json.dumps({'token': _pre_think})}\n\n"
                        thinking_full = _pre_think
                    # Stream message word-by-word for typing effect
                    yield f"event: message\ndata: {json.dumps({'token': _pre_msg})}\n\n"
                    message_full = _pre_msg
                    _token_count = 1
                else:
                    # Fallback: generate a fresh VoJ synthesis via streaming.
                    # Use the shared bridge so this fallback can't drift from the
                    # orchestrator's voice_of_jarvis_synthesis enrichment.
                    try:
                        from app.services.analytical.voice_of_jarvis import build_summary_from_state
                        _captured_summary = build_summary_from_state(final_state, _intent)
                        _captured_summary["user_prompt"] = request.user_prompt
                        async for event_type, tok in synthesize_jarvis_response_stream(_captured_summary):
                            if _ttft is None:
                                _ttft = (time_mod.monotonic() - _start) * 1000
                            _token_count += 1
                            if event_type == "thinking":
                                thinking_full += tok
                            else:
                                message_full += tok
                            yield f"event: {event_type}\ndata: {json.dumps({'token': tok})}\n\n"
                        _stream_model = _cfg.GEMINI_MODEL if _cfg.GEMINI_PRIMARY else SLM_ROUTER_MODEL
                    except Exception as voj_exc:
                        print(f"[v2/stream] VoJ streaming failed: {voj_exc}")
                        _fallback = final_state.get("response_message") or "Standing by, sir."
                        yield f"event: message\ndata: {json.dumps({'token': _fallback})}\n\n"
                        message_full = _fallback
                        _token_count = 1

            _total_s = time_mod.monotonic() - _start

            # Clean up think tags
            message_clean = re.sub(r"</?think>", "", message_full, flags=re.IGNORECASE).strip()
            thinking_clean = re.sub(r"</?think>", "", thinking_full, flags=re.IGNORECASE).strip()

            # If no separate reasoning, try regex extraction
            if not thinking_clean and message_clean:
                message_clean, thinking_extracted = _extract_thinking_process(message_clean)
                thinking_clean = thinking_extracted or ""

            _assistant_msg = message_clean or final_state.get("response_message") or "Standing by, sir."

            # --- Save assistant message ---
            _msg_id = await save_assistant_message(
                session_id, request.user_id, _assistant_msg, _intent,
                {
                    "schedule": final_state.get("schedule"),
                    "draft_id": final_state.get("draft_id"),
                    "intent": _intent,
                },
                supabase,
            )

            # Fire-and-forget memory extraction
            if memory_store:
                from app.services.memory.extractor import safe_extract_memories
                asyncio.create_task(safe_extract_memories(
                    request.user_id, request.user_prompt, _assistant_msg, memory_store,
                    db_client=supabase,
                ))

            # --- Build full ChatResponse payload ---
            complete_payload = {
                "intent": _intent,
                "message": _assistant_msg,
                "schedule": final_state.get("schedule"),
                "execution_graph": final_state.get("execution_graph"),
                "ingestion_result": final_state.get("ingestion_result"),
                "action_proposals": None,
                "search_result": None,
                "suggested_action": None,
                "thinking_process": thinking_clean or final_state.get("thinking_process"),
                "awaiting_task_confirmation": False,
                "schedule_status": "draft" if _has_schedule else None,
                "draft_id": final_state.get("draft_id"),
                "conversation_id": session_id,
                "message_id": _msg_id,
                "clarification_options": None,
                "memories": (
                    [
                        {"memory_type": m.get("memory_type"), "content": m.get("content"), "confidence": m.get("confidence", 0.5)}
                        for m in _existing_memories[:20]
                    ]
                    if _existing_memories else None
                ),
                "pearl_insights": None,
                "generation_metrics": {
                    "total_tokens": _token_count,
                    "total_time_s": round(_total_s, 2),
                    "tok_per_sec": round(_token_count / _total_s, 2) if _total_s > 0 else 0,
                    "ttft_ms": round(_ttft, 1) if _ttft is not None else None,
                    "model": _stream_model,
                },
            }
            yield f"event: complete\ndata: {json_mod.dumps(complete_payload)}\n\n"
        except Exception as exc:
            import traceback
            traceback.print_exc()  # full traceback to server terminal
            yield f"event: error\ndata: {json_mod.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
