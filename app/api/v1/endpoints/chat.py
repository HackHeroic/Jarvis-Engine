"""The chat endpoint: ``POST /api/v1/chat/stream``, the one way to talk to Jarvis.

A turn runs the LangGraph orchestrator and streams its progress and reply as
server-sent events. ``/api/v1/chat/v2/stream`` is the same handler under its old
path, hidden from the schema and deprecated, kept so a frontend and a backend
restarted in either order keep talking.
"""

import asyncio
import json as json_mod
import re
import time as time_mod
from typing import Optional

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.schemas.chat_stream import (
    ChatCompletePayload,
    ConsentRequestFrame,
    PhaseFrame,
    SseEncoder,
    SseEvent,
    StepFrame,
    TokenFrame,
)
from app.schemas.errors import ErrorBody, ErrorResponse
from app.services.analytical.voice_of_jarvis import (
    extract_thinking_process,
    synthesize_jarvis_response_stream,
)

router = APIRouter()


def _text_frame(event: SseEvent, text: str) -> str:
    """A ``thinking`` or ``message`` frame carrying ``text``."""
    return SseEncoder.frame(event, TokenFrame(token=text))


def _error_frame(request_id: str, code: str, message: str, details: dict | None = None) -> str:
    """The error envelope as the stream's ``error`` frame: the 200 has already gone out."""
    body = ErrorBody(code=code, message=message, details=details, request_id=request_id)
    return SseEncoder.frame(SseEvent.ERROR, ErrorResponse(error=body))


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
    """Request body for POST /api/v1/chat/stream."""

    user_prompt: str = Field(..., description="The user's message or goal")
    user_id: str = Field(..., description="User identifier for habits and storage")
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
        description=(
            "Which models answer this turn. 'auto' (the default): the local model first, "
            "the cloud providers when it cannot answer (cloud only while no local model is "
            "loaded). 'cloud' (also 'gemini', and the older '4b'): the cloud providers only, "
            "for this turn. Any other value reads as 'auto'."
        ),
    )
    file_name: Optional[str] = Field(
        default=None,
        description="Original filename of the uploaded file (for document management).",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="Session ID for conversation continuity. If None, auto-creates a new session.",
    )


@router.post(
    "/stream",
    summary="Talk to Jarvis (server-sent events)",
    description=(
        "THE entry point. Streams phase / step / thinking / message / tool_use / "
        "memory_extracted / pattern_detected frames, then one `complete` frame whose data "
        "is a `ChatCompletePayload` (app/schemas/chat_stream.py), or one `error` frame in "
        "the standard envelope."
    ),
)
# The old path of this same handler, hidden and deprecated: a frontend and a backend
# restarted in either order keep talking. It goes once the new path has passed a live run.
@router.post("/v2/stream", include_in_schema=False, deprecated=True)
async def chat_stream(request: ChatRequest, http_request: Request):
    """One turn: the LangGraph orchestrator, streamed as server-sent events. The
    ``complete`` frame is validated against ``ChatCompletePayload`` before it is sent."""
    from app.core.model_router import ServedModels, attribute_llm_calls, recording_served_models
    from app.core.observation import pearl_insights
    from app.core.user_model import UserModel
    from app.orchestrator.checkpoint import make_thread_id
    from app.orchestrator.state import ConversationPhase, NegotiationPhase
    from app.services.chat_history import (
        get_or_create_session,
        save_user_message,
        save_assistant_message,
        build_context_messages,
    )
    from contextlib import AsyncExitStack

    from app.api.deps import container_of
    from app.api.error_handlers import request_id_of
    from app.domain.errors import JarvisError
    from app.orchestrator.graph import is_trivial_input
    from app.services.memory.context import Recall, note_line, render_context_note
    from app.services.planning.local_time import user_zone
    from app.domain.clock import utc_now
    from app.services.turn_locks import thread_lock

    jarvis_graph = http_request.app.state.jarvis_graph
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    # Model selector: when user picks Gemini, force this whole turn to cloud.
    # MUST be set BEFORE the graph is invoked so every LLM call inherits it.
    # Reset with the usage attribution once the stream is over (see the end of
    # this handler): set here, it would otherwise outlive the turn.
    model_mode = request.model_mode or "auto"
    force_cloud_request = model_mode in ("4b", "cloud", "gemini")
    force_cloud_token = None
    if force_cloud_request:
        from app.core.model_router import force_cloud_var
        force_cloud_token = force_cloud_var.set(True)
        print(f"[chat] model_mode={model_mode} → force_cloud=True (cloud providers only this turn)")

    # --- Session management ---
    session_id = await get_or_create_session(
        request.user_id, request.conversation_id, supabase
    )

    # The recalled memories, for the frontend MemoryPanel; filled by _read_conversation.
    _existing_memories: list[dict] = []
    # "Since we last spoke" exactly as the note gave it to the model, when it did.
    _since_last_spoke: list[str] = []

    user_model = UserModel(user_id=request.user_id, db=db_client)

    # --- Progress bridge: sub-graph callbacks → SSE phase events ---
    progress_queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(phase, **detail):
        progress_queue.put_nowait(json_mod.dumps({"phase": phase, **detail}))

    initial_state = {
        # Serializable identity — survives checkpointing; user_model does not.
        # The facade is still passed here (the shared db_client);
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
        # draft_id, which the SSE payload below reports to the UI. draft_id is
        # deliberately absent, like the phase fields below: a resumed thread's
        # "looks good" must resolve the draft that thread proposed, and a None
        # here would overwrite it (accepting the user's newest draft instead).
        "draft_store": getattr(http_request.app.state, "draft_store", None),
        "saved_constraints": None,
        "saved_rule_ids": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        # Per turn: a checkpointed framing would re-announce last turn's missed tasks.
        "anti_guilt_message": None,
        "missed_deadlines": None,
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
        # Read under the turn lock by _read_conversation, so a turn that waited
        # for the one before it sees that turn's reply.
        "conversation_history": [],
        "memory_context": "",
        "progress_callback": progress_cb,
        "progress_queue": progress_queue,
        # Per-turn flag, cleared explicitly: it is checkpointed now, and the
        # negotiation shortcut bypasses extract_brain_dump (which otherwise
        # re-decides it), so a stale True would mute the observation loop.
        "trivial_input": None,
        # What this turn's memories cite as their source: the conversation, and the
        # user's message (its id filled in by _read_conversation once it is saved).
        # The handles are per turn too: extract_brain_dump sets them fresh.
        "session_id": session_id,
        "user_message_id": None,
        "memory_handles": {},
        # The user's memories, read once by _read_conversation for the whole turn; and
        # the insights of PEARL's look in the observation loop. Both per turn.
        "memory_snapshot": None,
        "pearl_insights": None,
        # When user picks Gemini in the model selector, force every LLM call
        # in this turn to route to cloud — not local Gemma.
        "force_cloud_request": force_cloud_request,
    }

    # One checkpoint thread per (user, session). The user prefix matters:
    # _load_context rebuilds the UserModel facade from the checkpointed user_id,
    # so a session-only key would let a resumed turn rebuild another user's facade.
    thread_id = make_thread_id(request.user_id, session_id)
    graph_config = {"configurable": {"thread_id": thread_id}}

    async def _seed_if_fresh_thread() -> None:
        try:
            _existing = await jarvis_graph.aget_state(graph_config)
            _resumed = bool(_existing and getattr(_existing, "values", None))
        except Exception as _e:
            print(f"[chat] Checkpoint lookup failed ({_e}) — starting a fresh thread")
            _resumed = False
        if not _resumed:
            initial_state["conversation_phase"] = ConversationPhase.GREETING
            initial_state["negotiation_state"] = NegotiationPhase.NONE
            initial_state["draft_id"] = None

    async def _read_conversation() -> None:
        """Read the conversation as it stands now. Runs under the turn lock.

        History first, then save: the window is the PRIOR context. Saved first, the
        message being answered also sat in its own history — the model read it
        twice and it took one of the window's slots. The window is
        ``ConversationMemoryService.build_history``: the messages after the
        conversation's summary, the summary itself going in the note (``<earlier>``).
        A conversation with no messages yet, opened after an earlier one, gets "since
        we last spoke" (``<since>``, no model call).

        Two stages of reads: the window with the profile; then, at once, "since we last
        spoke" (only for an empty window), recall, the plate and the save of this
        message, none of which reads this conversation's messages.

        Then the turn's context note (``memory_context``): what ``MemoryService.recall``
        finds for this message within its token budgets (a trivial message gets the
        profile only), the plate and the time on the user's clock. The memory panel
        shows exactly the recalled memories. A read that fails is left out of the note
        and logged; the turn goes on. Without a composition root there is no note, and
        the history is the plain newest-ten window, as it is when the window cannot be
        read (for one, before the summary columns' migration is applied).
        """
        container = container_of(http_request)
        if container is None:
            initial_state["conversation_history"] = await build_context_messages(
                session_id, request.user_id, max_messages=10, max_chars_per_message=500,
                supabase=supabase,
            )
            initial_state["user_message_id"] = await save_user_message(
                session_id, request.user_id, request.user_prompt, supabase
            )
            return
        conversations = container.conversation_memory

        async def _snapshot():
            try:
                return await container.memory_service.snapshot(request.user_id)
            except Exception as exc:  # each reader reads for itself, never a failed turn
                logger.warning(f"Memories unread for the turn ({type(exc).__name__})")
                return None

        # The user's memories, read once for the whole turn: recall, the extraction's
        # known memories and every forget check share this one read.
        snapshot_read = asyncio.ensure_future(_snapshot())

        async def _let_go_ids():
            snapshot = await snapshot_read
            return snapshot.let_go_ids if snapshot is not None else None

        async def _window():
            try:
                return await conversations.build_history(
                    request.user_id, session_id, let_go_ids=_let_go_ids
                )
            except Exception as exc:  # the plain newest-ten window, never a failed turn
                logger.warning(
                    f"Conversation history unavailable this turn ({type(exc).__name__})"
                )
                return None

        # Stage 1, before this message is saved: the window, the profile and the memories.
        window, profile, snapshot = await asyncio.gather(
            _window(), user_model.get_profile(), snapshot_read
        )
        initial_state["memory_snapshot"] = snapshot
        if window is not None:
            initial_state["conversation_history"] = window.history
        else:
            initial_state["conversation_history"] = await build_context_messages(
                session_id, request.user_id, max_messages=10, max_chars_per_message=500,
                supabase=supabase,
            )
        zone = user_zone(profile)
        now = utc_now()

        async def _since() -> str | None:
            if window is None or window.message_count or window.summarized:
                return None
            try:  # the window found no messages: "since" need not read them again
                return await conversations.since_last_spoke(
                    request.user_id, session_id, zone=zone, known_empty=True,
                    snapshot=snapshot,
                )
            except Exception as exc:  # nothing to pick up this turn, never a failed turn
                logger.warning(f"'Since we last spoke' unavailable ({type(exc).__name__})")
                return None

        async def _recall() -> Recall:
            try:
                return await container.memory_service.recall(
                    request.user_id,
                    request.user_prompt,
                    trivial=is_trivial_input(request.user_prompt),
                    now=now,
                    snapshot=snapshot,
                )
            except Exception as exc:  # no memories this turn, never a failed turn
                logger.warning(f"Recall unavailable this turn ({type(exc).__name__})")
                return Recall(profile=[], relevant=[], tokens=0)

        async def _plate() -> list[str]:
            try:
                pending = await container.repos.tasks.list_pending(request.user_id)
            except Exception as exc:  # no plate this turn, never a failed turn
                logger.warning(
                    f"Pending tasks unavailable for the context note ({type(exc).__name__})"
                )
                return []
            pending.sort(  # soonest first; task_id breaks ties, so the note is stable
                key=lambda t: (t.scheduled_start is None, t.scheduled_start or now, t.task_id)
            )
            return [
                f"{t.title} (due {t.deadline_hint})" if t.deadline_hint else t.title
                for t in pending
            ]

        # Stage 2, all at once: none of these reads this conversation's own messages, so
        # saving the message alongside them is safe.
        since, recall, plate, user_message_id = await asyncio.gather(
            _since(),
            _recall(),
            _plate(),
            save_user_message(session_id, request.user_id, request.user_prompt, supabase),
        )
        initial_state["user_message_id"] = user_message_id
        initial_state["memory_context"] = (
            render_context_note(
                recall,
                now=now,
                zone=zone,
                since=since,
                earlier=window.earlier if window is not None else None,
                plate=plate,
            )
            or ""
        )
        _since_last_spoke[:] = [note_line(since)] if since else []
        _existing_memories[:] = [
            {
                "memory_id": m.id,
                "memory_type": m.memory_type,
                "content": m.content,
                "confidence": m.confidence,
                "provenance": m.provenance.value,
            }
            for m in recall.all()
        ]

    # Which provider and model answered each LLM call of this turn, recorded by the
    # metering decorator; the reply is credited to the model that wrote it.
    served = ServedModels()
    _REPLY_TASKS = ("voice_of_jarvis", "voice_of_jarvis_synthesis")

    async def event_gen():
        import app.core.config as _cfg

        def _answering_model() -> str:
            """The model that wrote this turn's reply; "cloud"/"local" when no model call did."""
            model = served.last_model_for(_REPLY_TASKS)
            if model:
                return model
            from app.core.model_router import force_cloud_var

            cloud_first = _cfg.GEMINI_PRIMARY or _cfg.CLAUDE_PRIMARY or force_cloud_var.get()
            return "cloud" if cloud_first else "local"

        def _intent_str(intent) -> str:
            """Safely convert IntentType enum or string to plain string."""
            if hasattr(intent, "value"):
                return intent.value
            return str(intent)

        # Graph node events ride the SAME queue as progress/token events so the
        # SSE stream keeps true emission order. Draining only between astream()
        # iterations would leave this coroutine blocked for the whole synthesis
        # call and deliver every "streamed" token in one burst at the end.
        _NODE, _ERROR, _DONE = "__node__", "__error__", "__done__"

        async def _pump_graph():
            try:
                await _seed_if_fresh_thread()
                async for _ev in jarvis_graph.astream(initial_state, config=graph_config):
                    progress_queue.put_nowait((_NODE, _ev))
            except Exception as _exc:
                progress_queue.put_nowait((_ERROR, _exc))
            finally:
                progress_queue.put_nowait((_DONE, None))

        # Streaming metrics — filled by the token branch of the loop below.
        _turn_start = time_mod.monotonic()
        _gen_start: float | None = None
        _ttft: float | None = None
        _token_count = 0
        thinking_full = ""
        message_full = ""
        tokens_streamed = False
        thinking_streamed = False

        pump_task: asyncio.Task | None = None
        turn_lock = AsyncExitStack()
        try:
            # One turn at a time per conversation: a second tab's turn on this thread
            # queues here until this turn is over — its reply saved, or failed, or
            # cancelled by a disconnect. History, memory and the checkpoint are all
            # read under the lock, so a queued turn sees what the turn before it left.
            # A wait past TURN_LOCK_WAIT_SECONDS ends in a conflict frame instead.
            await turn_lock.enter_async_context(thread_lock(thread_id))
            await _read_conversation()
            final_state = {}
            graph_exc: Exception | None = None
            pump_task = asyncio.create_task(_pump_graph())

            while True:
                raw = await progress_queue.get()

                if isinstance(raw, tuple):
                    kind, payload = raw
                    if kind == _DONE:
                        break
                    if kind == _ERROR:
                        graph_exc = payload
                        continue

                    event = payload
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
                        yield SseEncoder.frame(
                            SseEvent.PHASE, PhaseFrame.model_validate(phase_detail)
                        )

                    if node_name == "classify_intent" and node_state.get("intent"):
                        yield SseEncoder.frame(SseEvent.STEP, StepFrame(
                            intent=_intent_str(node_state["intent"]), stage="intent_classified"
                        ))

                    # Emit consent_request if a hook asked for consent
                    if node_state.get("needs_consent"):
                        yield SseEncoder.frame(SseEvent.CONSENT_REQUEST, ConsentRequestFrame(
                            reason=node_state.get("response_message") or "", module=node_name
                        ))

                    continue

                # Sub-graph / synthesis bridge event (JSON string)
                frame, evt_type, _tok = SseEncoder.render_progress(raw)
                if evt_type in ("token", "thinking_token"):
                    if _gen_start is None:
                        _gen_start = time_mod.monotonic()
                        _ttft = (_gen_start - _turn_start) * 1000
                    _token_count += 1
                    if evt_type == "token":
                        tokens_streamed = True
                        message_full += _tok
                    else:
                        thinking_streamed = True
                        thinking_full += _tok
                yield frame

            await pump_task
            if graph_exc is not None:
                raise graph_exc

            _intent = _intent_str(final_state.get("intent", "CHAT"))
            _is_chat = _intent in ("CHAT", "GREETING", "GENERAL_QA")
            _has_schedule = bool(final_state.get("schedule"))

            from app.core.model_router import force_cloud_var

            _force_cloud_now = force_cloud_var.get()
            yield SseEncoder.frame(SseEvent.STEP, StepFrame(
                intent=_intent, stage="pipeline_done", model_mode=model_mode,
                synthesis_model=_answering_model(), force_cloud=_force_cloud_now,
            ))

            # --- Token streaming ---
            # Credited once the reply exists (below); a template reply keeps its label.
            _stream_model: str | None = None
            if _gen_start is None:
                # Nothing came through the bridge — this turn falls back to
                # emitting the assembled message as a single frame. Time the
                # emission window only, as the pre-streaming code did.
                _gen_start = time_mod.monotonic()

            if tokens_streamed:
                # Synthesis already streamed live through the progress bridge.
                # Re-emitting the assembled message here would duplicate it.
                pass

            elif _has_schedule and _intent == "PLAN_DAY":
                # Schedule responses: emit pre-computed message, no VoJ streaming needed
                _pre_msg = final_state.get("response_message") or "Here's your schedule."
                _pre_think = final_state.get("thinking_process")
                if _pre_think and not thinking_streamed:
                    yield _text_frame(SseEvent.THINKING, _pre_think)
                    thinking_full = _pre_think
                yield _text_frame(SseEvent.MESSAGE, _pre_msg)
                message_full = _pre_msg
                _token_count = 1
                _stream_model = "none (structured response)"

            elif _is_chat:
                # CHAT/GREETING/GENERAL_QA: stream LLM response live for typing effect
                _pre_msg = final_state.get("response_message", "")
                _pre_think = final_state.get("thinking_process")

                if _pre_msg:
                    # Graph already computed the response — stream it word-by-word for UX
                    if _pre_think and not thinking_streamed:
                        yield _text_frame(SseEvent.THINKING, _pre_think)
                        thinking_full = _pre_think
                    yield _text_frame(SseEvent.MESSAGE, _pre_msg)
                    message_full = _pre_msg
                    _token_count = 1
                else:
                    # Empty graph output for CHAT — last-resort: invoke conversation module
                    # directly so the LLM still produces the reply (no hardcoded text).
                    from app.modules.conversation import run_general_chat as _run_gc
                    # The note and history come from the turn's input: final_state holds
                    # node outputs only, and neither is one.
                    _gc_out = await _run_gc({
                        "user_message": request.user_prompt,
                        "user_model": final_state.get("user_model"),
                        "memory_context": initial_state.get("memory_context", ""),
                        "conversation_history": initial_state.get("conversation_history", []),
                    })
                    _llm_msg = _gc_out.get("response_message") or "Standing by, sir."
                    _llm_think = _gc_out.get("thinking_process")
                    if _llm_think:
                        yield _text_frame(SseEvent.THINKING, _llm_think)
                        thinking_full = _llm_think
                    yield _text_frame(SseEvent.MESSAGE, _llm_msg)
                    message_full = _llm_msg
                    _token_count = 1

            else:
                # Pipeline intents (ingestion, behavioral, etc.): stream VoJ synthesis
                _pre_msg = final_state.get("response_message", "")
                _pre_think = final_state.get("thinking_process")

                if _pre_msg and _pre_msg not in ("", "Standing by, sir."):
                    # VoJ already computed in synthesize_response node — emit it
                    if _pre_think and not thinking_streamed:
                        yield _text_frame(SseEvent.THINKING, _pre_think)
                        thinking_full = _pre_think
                    # Stream message word-by-word for typing effect
                    yield _text_frame(SseEvent.MESSAGE, _pre_msg)
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
                        # From the turn's input, like the chat fallback above.
                        _captured_summary["memory_context"] = initial_state.get(
                            "memory_context", ""
                        )
                        async for event_type, tok in synthesize_jarvis_response_stream(
                            _captured_summary,
                            conversation_history=initial_state.get("conversation_history"),
                        ):
                            if _ttft is None:
                                _ttft = (time_mod.monotonic() - _turn_start) * 1000
                            _token_count += 1
                            if event_type == "thinking":
                                thinking_full += tok
                            else:
                                message_full += tok
                            yield _text_frame(SseEvent(event_type), tok)
                    except Exception as voj_exc:
                        print(f"[chat/stream] VoJ streaming failed: {voj_exc}")
                        _fallback = final_state.get("response_message") or "Standing by, sir."
                        yield _text_frame(SseEvent.MESSAGE, _fallback)
                        message_full = _fallback
                        _token_count = 1

            if _stream_model is None:
                _stream_model = _answering_model()

            # Generation window, not turn duration: tok/sec must describe the
            # model's output rate, not how long the whole pipeline ran.
            _total_s = time_mod.monotonic() - (_gen_start or _turn_start)

            # Clean up think tags
            message_clean = re.sub(r"</?think>", "", message_full, flags=re.IGNORECASE).strip()
            thinking_clean = re.sub(r"</?think>", "", thinking_full, flags=re.IGNORECASE).strip()

            # If no separate reasoning, try regex extraction
            if not thinking_clean and message_clean:
                message_clean, thinking_extracted = extract_thinking_process(message_clean)
                thinking_clean = thinking_extracted or ""

            _assistant_msg = message_clean or final_state.get("response_message") or "Standing by, sir."
            # A draft id belongs to the turn that proposed it: only a turn that
            # produced a schedule did. Any other turn (an accept, an edit request)
            # would otherwise re-announce the thread's draft as if it were new.
            _turn_draft_id = final_state.get("draft_id") if _has_schedule else None

            # --- Save assistant message ---
            _msg_id = await save_assistant_message(
                session_id, request.user_id, _assistant_msg, _intent,
                {
                    "schedule": final_state.get("schedule"),
                    "draft_id": _turn_draft_id,
                    "intent": _intent,
                },
                supabase,
            )

            # Memories and PEARL were learned in the graph's observation loop, which
            # skips greetings, and their frames are already sent: every learning
            # moment arrives before complete, and nothing is left running after it.
            # The turn's learning, for the complete frame: PEARL's insights (a bounded
            # read), a re-plan hint, and the honest status of a plan that did not fit.
            # The observation loop's own look holds them; asked of PEARL only when that
            # look did not run or finish (a greeting, a slow store).
            _insights = final_state.get("pearl_insights")
            if _insights is None:
                _insights = await pearl_insights(request.user_id)
            _insights = _insights or None
            _infeasible = final_state.get("error") == "INFEASIBLE_EXHAUSTED"
            _schedule_status = "draft" if _has_schedule else ("infeasible" if _infeasible else None)
            _suggested_action = (
                "replan"
                if final_state.get("saved_constraints") or final_state.get("anti_guilt_message")
                else None
            )

            # --- The complete frame (ChatCompletePayload) ---
            complete_payload = {
                "intent": _intent,
                "message": _assistant_msg,
                "schedule": final_state.get("schedule"),
                "execution_graph": final_state.get("execution_graph"),
                "ingestion_result": final_state.get("ingestion_result"),
                "action_proposals": None,
                "search_result": None,
                "suggested_action": _suggested_action,
                "thinking_process": thinking_clean or final_state.get("thinking_process"),
                "schedule_status": _schedule_status,
                "conflict_summary": (
                    final_state.get("clarification_request") if _infeasible else None
                ),
                "draft_id": _turn_draft_id,
                "saved_constraints": final_state.get("saved_constraints"),
                "conversation_id": session_id,
                "message_id": _msg_id,
                "clarification_options": None,
                # {memory_id, memory_type, content, confidence, provenance}: what the
                # model was given about the user this turn.
                "memories": list(_existing_memories) or None,
                # What the model was told about the last conversation, or null.
                "since_last_spoke": _since_last_spoke[0] if _since_last_spoke else None,
                "pearl_insights": _insights,
                "generation_metrics": {
                    "total_tokens": _token_count,
                    "total_time_s": round(_total_s, 2),
                    "tok_per_sec": round(_token_count / _total_s, 2) if _total_s > 0 else 0,
                    "ttft_ms": round(_ttft, 1) if _ttft is not None else None,
                    "model": _stream_model,
                },
            }
            # Compaction: a tracked background task the turn never waits for. It starts
            # once this frame is on its way; created before the yield, so a client that
            # leaves at the frame cannot skip it.
            _container = container_of(http_request)
            if _container is not None:
                _container.conversation_memory.compact_in_background(request.user_id, session_id)
            # The frame's contract, checked before it is sent (fields exactly as the
            # frontend reads them): a frame off the contract is a failed turn, not a
            # silently different payload.
            yield SseEncoder.frame(
                SseEvent.COMPLETE, ChatCompletePayload.model_validate(complete_payload)
            )
        except JarvisError as exc:
            # Raised on purpose, with a user-facing message (a turn that waited too
            # long for the one before it is a conflict): the standard envelope.
            yield _error_frame(request_id_of(http_request), exc.code, exc.message, exc.details)
        except Exception as exc:
            # The cause is logged with the request id; its text never goes to the client.
            request_id = request_id_of(http_request)
            logger.error(
                f"chat stream failed [request_id={request_id}]: {type(exc).__name__}",
                exc_info=exc,
            )
            yield _error_frame(request_id, "internal_error", "Something went wrong on our side.")
        finally:
            # Shielded: under uvicorn, Starlette streams inside an anyio task group
            # and a client disconnect cancels that scope — anyio then re-cancels
            # every await made inside it, so an unshielded wait below would itself
            # be cancelled, the lock would be left to the garbage collector, and a
            # queued turn could start while this run was still unwinding.
            with anyio.CancelScope(shield=True):
                # The graph no longer runs inline, so a client disconnect would
                # otherwise leave it churning through LLM calls nobody reads.
                if pump_task is not None and not pump_task.done():
                    pump_task.cancel()
                    # Release the conversation only once this run has stopped writing it.
                    await asyncio.wait([pump_task])
                await turn_lock.aclose()

    # Usage attribution, like force_cloud_var: every LLM call of this turn — the
    # graph pump and every task it starts copy this context — is metered to this
    # user and session. The stream is consumed after this handler returns, so the
    # attribution ends in the response's background task, which Starlette runs in
    # this same context once the last frame is sent or the client has gone.
    end_attribution = attribute_llm_calls(request.user_id, session_id)
    end_recording = recording_served_models(served)

    async def _end_attribution() -> None:
        # async on purpose: a sync background task runs on a worker thread, in a
        # copy of this context, where the ContextVar reset would raise.
        end_recording()
        end_attribution()
        if force_cloud_token is not None:
            from app.core.model_router import force_cloud_var
            force_cloud_var.reset(force_cloud_token)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        background=BackgroundTask(_end_attribution),
    )
