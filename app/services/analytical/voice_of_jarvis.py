"""Voice of Jarvis: synthesize a single warm message from execution summary."""

import contextvars
import re
from collections.abc import AsyncGenerator
from typing import Any

import app.core.config as _cfg
from app.models.brain.litellm_conf import hybrid_route_query

# ContextVar used by /stream endpoint to capture execution_summary without calling VoJ.
# When set to a dict, synthesize_jarvis_response writes the summary into it and returns
# empty placeholders instead of calling the LLM.
_summary_capture: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_summary_capture", default=None
)

VOICE_OF_JARVIS_PROMPT = (
    "You are J.A.R.V.I.S. (Just A Rather Very Intelligent System), the user's personal AI. "
    "Loyal, brilliant, composed. British cadence, formal but warm. Address the user as 'sir'. "
    "The user sent a message and we executed the following actions.\n\n"
    "You MUST use this EXACT format — no exceptions:\n"
    "<think>\n"
    "2-4 sentences explaining what we did internally.\n"
    "</think>\n"
    "Your composed 1-2 sentence response to the user as JARVIS.\n\n"
    "VOICE RULES:\n"
    "- You MUST start your response with <think> (the literal XML tag)\n"
    "- You MUST close the thinking block with </think> (the literal XML tag)\n"
    "- After </think>, write ONLY your 1-2 sentence response — nothing else\n"
    "- Use British cadence: 'Shall I...', 'If I may, sir...', 'I've taken the liberty of...'\n"
    "- Lead with information or status, not pleasantries\n"
    "- Show care through competence, not flattery. No 'I'd be happy to!' or 'Great!'\n"
    "- Dry wit through understatement, never sarcasm or condescension\n"
    "- No emoji, no exclamation marks, no filler phrases\n"
    "- Never begin the response with 'I' — rephrase to lead with the fact or action\n"
    "- NEEDS_END_DATE_INSTRUCTION: If needs_end_date is true, ask when the semester ends\n\n"
    "EXAMPLES:\n"
    "<think>\n"
    "Classified as a planning request. Decomposed into 6 micro-tasks, applied gym habit as "
    "a hard block, ran the CP-SAT solver with TMT priority weighting.\n"
    "</think>\n"
    "Your schedule is set, sir — six tasks, routed around your gym blocks. "
    "Deadline-adjacent items have been given priority.\n\n"
    "<think>\n"
    "User uploaded a PDF. Extracted 12 pages via Docling, chunked into 34 segments, "
    "embedded in ChromaDB, linked to 3 active tasks.\n"
    "</think>\n"
    "Document processed and linked to your active tasks, sir. "
    "Shall I pull the relevant sections into your next study session?"
)


def _build_thinking_fallback(execution_summary: dict[str, Any]) -> str | None:
    """Build human-readable thinking_process from execution summary when LLM does not emit think blocks."""
    bullets: list[str] = []
    if execution_summary.get("schedule_generated"):
        bullets.append(
            "I broke down your goal into micro-tasks with a 25-minute ceiling. "
            "I applied your habit constraints. The CP-SAT solver assigned tasks to slots with zero overlaps."
        )
    if execution_summary.get("habits_saved"):
        bullets.append("I noted your preferences. On the next plan, I'll respect them.")
    if execution_summary.get("knowledge_ingested"):
        bullets.append(
            "I extracted and chunked your materials, stored them in ChromaDB, "
            "and linked them to your tasks."
        )
    if execution_summary.get("calendar_extracted"):
        bullets.append("I extracted your timetable and prepared it for review.")
    if execution_summary.get("spread_across_days"):
        bullets.append("I spread the schedule across multiple days to fit your constraints.")
    if execution_summary.get("greeting"):
        bullets.append(
            "User sent a greeting. Routed via 4B SLM — no planning goal detected. "
            "Acknowledging and standing by."
        )
    if not bullets:
        return None
    return " ".join(bullets)


def _extract_thinking_process(raw_text: str) -> tuple[str, str | None]:
    """Extract think blocks and strip from main message.

    Uses greedy .* (not non-greedy .*?) so that </think> appearing as text inside
    the thinking block doesn't cause a premature match.
    """
    thinking_process = None
    think_match = re.search(r"<think>(.*)</think>", raw_text, flags=re.DOTALL | re.IGNORECASE)
    if think_match:
        thinking_process = think_match.group(1).strip()

    clean_text = re.sub(r"<think>.*</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE)
    if "Thinking Process" in clean_text or "Draft:" in clean_text:
        parts = re.split(r"Draft:|Final Polish:", clean_text, flags=re.IGNORECASE)
        clean_text = parts[-1].strip() if len(parts) > 1 else clean_text.split("\n\n")[-1]
    clean_text = clean_text.strip().replace('"', "").replace("*", "")
    if not clean_text:
        clean_text = "Standing by, sir."
    return clean_text, thinking_process


def build_summary_from_state(state: dict, intent: str = "") -> dict[str, Any]:
    """Build the dict that _build_voj_parts can consume from raw orchestrator state.

    Bridges v2 graph state (which carries `schedule`, `execution_graph`, etc.)
    to the boolean-flag contract `_build_voj_parts` expects (`schedule_generated`,
    `knowledge_ingested`, etc.). Used by both the orchestrator's
    voice_of_jarvis_synthesis node AND the chat-stream fallback path so they
    can never drift out of sync.
    """
    schedule = state.get("schedule")
    execution_graph = state.get("execution_graph")
    ingestion_result = state.get("ingestion_result")
    research_results = state.get("research_results")
    cal_extracted = bool(
        ingestion_result
        and isinstance(ingestion_result, dict)
        and ingestion_result.get("calendar_extracted")
    )
    return {
        "intent": intent or str(state.get("intent", "")),
        "schedule": schedule,
        "execution_graph": execution_graph,
        "research_results": research_results,
        "ingestion_result": ingestion_result,
        "knowledge_ingested": bool(ingestion_result),
        "calendar_extracted": cal_extracted,
        "search_done": bool(research_results),
        "schedule_generated": bool(schedule) or bool(execution_graph),
        "task_count": (
            len(execution_graph.get("decomposition") or [])
            if isinstance(execution_graph, dict) else 0
        ),
        "anti_guilt_message": state.get("anti_guilt_message"),
        "missed_deadlines": state.get("missed_deadlines"),
        "clarification_request": state.get("clarification_request"),
        "error": state.get("error"),
        "user_prompt": state.get("user_message", "") or state.get("user_prompt", ""),
        "memory_context": state.get("memory_context", ""),
        "coaching_data": state.get("coaching_data"),
    }


def _will_route_cloud() -> bool:
    """The same predicate route_llm_call uses to decide a cloud send.

    Both synthesis twins call hybrid_route_query directly with a model_override,
    which silently redirects to Gemini under GEMINI_PRIMARY (litellm_conf.py:103)
    — so they bypass route_llm_call's PreCloudLLM gate entirely. This lets them
    run it themselves on exactly the turns that leave the machine.
    """
    try:
        from app.core.model_router import force_cloud_var
        if force_cloud_var.get():
            return True
    except Exception:
        pass
    return bool(_cfg.GEMINI_PRIMARY)


async def _gate_cloud_text(text: str) -> str:
    """Run the PreCloudLLM (L8 PII) hook over one outbound string."""
    if not text:
        return text
    try:
        from app.orchestrator.hooks import HookDecision, get_hooks

        hooks = get_hooks()
        if hooks is None:
            return text
        result = await hooks.execute("PreCloudLLM", prompt=text)
        if result.decision == HookDecision.MODIFY and result.modified_input:
            return result.modified_input.get("prompt", text)
    except Exception as e:
        # A gate that crashes must not become a gate that leaks: the caller
        # keeps the original text only because failing the turn outright would
        # be worse, and the failure is loud.
        print(f"[Voice of Jarvis] PreCloudLLM gate failed: {e}")
    return text


def _build_voj_parts(execution_summary: dict[str, Any]) -> list[str]:
    """Build the summary text parts passed to Voice of Jarvis LLM."""
    parts = []
    if execution_summary.get("habits_saved"):
        parts.append(f"habits_saved: {execution_summary['habits_saved']}")
    if execution_summary.get("state_applied"):
        parts.append(f"state_applied: {execution_summary['state_applied']}")
    if execution_summary.get("action_proposals"):
        titles = [
            p.get("title", p) for p in execution_summary["action_proposals"] if isinstance(p, dict)
        ]
        parts.append(f"action_proposals: {titles}")
    if execution_summary.get("calendar_extracted"):
        parts.append("calendar_extracted: true")
    if execution_summary.get("knowledge_ingested"):
        parts.append("knowledge_ingested: true")
        if execution_summary.get("materials_linked"):
            parts.append(f"materials_linked_to_tasks: {execution_summary['materials_linked']}")
    if execution_summary.get("needs_end_date"):
        parts.append(
            "needs_end_date: true - MUST politely ask when the semester/finals end so we can expire the schedule"
        )
    if execution_summary.get("action_proposal"):
        ap = execution_summary["action_proposal"]
        title = ap.get("title", ap) if isinstance(ap, dict) else getattr(ap, "title", "")
        parts.append(f"action_proposal: {title}")
    if execution_summary.get("search_done"):
        parts.append(f"search_done: {execution_summary['search_done']}")
    if execution_summary.get("schedule_generated"):
        parts.append("schedule_generated: true")
    if execution_summary.get("greeting"):
        parts.append(
            "greeting: true — the user said hi or sent a non-actionable message. "
            "Respond warmly in 1-2 sentences. Introduce yourself briefly. "
            "Tell them they can plan their day, drop a syllabus, or set habits."
        )
    if execution_summary.get("anti_guilt_message"):
        parts.append(
            f"anti_guilt: {execution_summary['anti_guilt_message']} "
            "Lead with this framing — composed, never apologetic, never shaming."
        )
    return parts


async def synthesize_jarvis_response(
    execution_summary: dict[str, Any],
    conversation_history: list[dict] | None = None,
) -> tuple[str, str | None]:
    """Generate message and extract thinking process. Returns (message, thinking_process).

    When _summary_capture ContextVar is set (streaming mode), captures the summary
    and returns empty placeholders instead of calling the LLM.
    """
    capture = _summary_capture.get()
    if capture is not None:
        capture.update(execution_summary)
        return ("", None)

    if execution_summary.get("spread_across_days"):
        thinking = _build_thinking_fallback(execution_summary)
        return (
            "I've spread this across multiple days to fit your constraints. Here's your schedule.",
            thinking,
        )

    parts = _build_voj_parts(execution_summary)
    if not parts:
        # Before falling back, surface a clarification or error if upstream
        # produced one — never let signal die silently.
        clar = execution_summary.get("clarification_request")
        if clar:
            return clar, None
        err = execution_summary.get("error")
        if err:
            return f"Sir, something hiccupped on my end: {err}. Shall we try again?", None
        return "Standing by, sir.", None

    summary_text = "\n".join(parts)
    try:
        # Include memory context in system prompt for personalization
        voj_prompt = VOICE_OF_JARVIS_PROMPT
        mem_ctx = execution_summary.get("memory_context", "")
        if _will_route_cloud():
            summary_text = await _gate_cloud_text(summary_text)
            mem_ctx = await _gate_cloud_text(mem_ctx)
        if mem_ctx:
            voj_prompt += f"\n\nUser context:\n{mem_ctx}"

        result = await hybrid_route_query(
            user_prompt=summary_text,
            system_prompt=voj_prompt,
            response_schema=None,
            model_override=_cfg.SLM_ROUTER_MODEL,
            conversation_history=conversation_history,
        )
        msg = result if isinstance(result, str) else str(result)
        msg = msg.strip() if msg else "Standing by, sir."
        message, thinking_process = _extract_thinking_process(msg)

        # Use fallback for missing or trivially short thinking (e.g. model outputs "...")
        if not thinking_process or len(thinking_process.strip(".").strip()) < 10:
            thinking_process = _build_thinking_fallback(execution_summary)

        return (message, thinking_process)
    except Exception as e:
        print(f"[Voice of Jarvis] Synthesis failed: {e}")
        thinking = _build_thinking_fallback(execution_summary)
        if execution_summary.get("spread_across_days"):
            return "I've spread this across multiple days to fit your constraints. Here's your schedule.", thinking
        if execution_summary.get("schedule_generated"):
            return "Here's your schedule.", thinking
        if execution_summary.get("habits_saved"):
            return "I've noted your preferences. I'll apply them to your next plan.", thinking
        if execution_summary.get("knowledge_ingested"):
            return "I've processed and stored your materials.", thinking
        if execution_summary.get("calendar_extracted"):
            return "I've extracted your timetable for review.", thinking
        return "Standing by, sir.", thinking


async def synthesize_jarvis_response_stream(
    execution_summary: dict[str, Any],
    conversation_history: list[dict] | None = None,
) -> AsyncGenerator[tuple[str, str], None]:
    """Stream (event_type, token) pairs for live thinking display.

    event_type is 'thinking' while inside <think>...</think> and 'message' outside.
    Yields tokens as they arrive for real-time UI updates.
    Falls back to yielding the deterministic thinking then the fallback message if LLM fails.

    Takes conversation_history like its non-streaming twin: a live SSE turn
    always has a progress_queue and therefore always lands here, so omitting it
    made synthesis context-blind exactly where users actually hit it.
    """
    if execution_summary.get("spread_across_days"):
        thinking = _build_thinking_fallback(execution_summary) or ""
        if thinking:
            yield ("thinking", thinking)
        yield ("message", "I've spread this across multiple days to fit your constraints. Here's your schedule.")
        return

    parts = _build_voj_parts(execution_summary)
    if not parts:
        clar = execution_summary.get("clarification_request")
        if clar:
            yield ("message", clar)
            return
        err = execution_summary.get("error")
        if err:
            yield ("message", f"Sir, something hiccupped on my end: {err}. Shall we try again?")
            return
        yield ("message", "Standing by, sir.")
        return

    summary_text = "\n".join(parts)
    try:
        # Same memory personalization the non-streaming twin applies — without
        # this the streamed voice quietly loses what it knows about the user.
        voj_prompt = VOICE_OF_JARVIS_PROMPT
        mem_ctx = execution_summary.get("memory_context", "")
        if _will_route_cloud():
            summary_text = await _gate_cloud_text(summary_text)
            mem_ctx = await _gate_cloud_text(mem_ctx)
        if mem_ctx:
            voj_prompt += f"\n\nUser context:\n{mem_ctx}"

        token_gen = await hybrid_route_query(
            user_prompt=summary_text,
            system_prompt=voj_prompt,
            response_schema=None,
            model_override=_cfg.SLM_ROUTER_MODEL,
            stream=True,
            conversation_history=conversation_history,
        )

        # Stream tokens in real-time with <think> tag parsing state machine.
        # If LM Studio provides native reasoning_content, those arrive as "reasoning" evt_type.
        # Otherwise, we parse <think>...</think> tags from the content stream.
        in_think_block = False
        tag_buffer = ""
        had_reasoning = False
        had_content = False

        async for evt_type, tok in token_gen:  # type: ignore[union-attr]
            # Native reasoning_content from LM Studio — yield directly
            if evt_type == "reasoning":
                had_reasoning = True
                yield ("thinking", tok)
                continue

            # Parse <think> tags from content stream in real-time
            tag_buffer += tok

            while tag_buffer:
                if not in_think_block:
                    think_idx = tag_buffer.lower().find("<think>")
                    if think_idx == -1:
                        # No tag found; keep last 7 chars as potential partial tag
                        if len(tag_buffer) > 7:
                            safe = tag_buffer[:-7]
                            tag_buffer = tag_buffer[-7:]
                            if safe:
                                had_content = True
                                yield ("message", safe)
                        break
                    else:
                        before = tag_buffer[:think_idx]
                        if before:
                            had_content = True
                            yield ("message", before)
                        tag_buffer = tag_buffer[think_idx + 7:]
                        in_think_block = True
                else:
                    close_idx = tag_buffer.lower().find("</think>")
                    if close_idx == -1:
                        # Keep last 8 chars as potential partial closing tag
                        if len(tag_buffer) > 8:
                            safe = tag_buffer[:-8]
                            tag_buffer = tag_buffer[-8:]
                            if safe:
                                had_reasoning = True
                                yield ("thinking", safe)
                        break
                    else:
                        before = tag_buffer[:close_idx]
                        if before:
                            had_reasoning = True
                            yield ("thinking", before)
                        tag_buffer = tag_buffer[close_idx + 8:]
                        in_think_block = False

        # Flush remaining buffer
        if tag_buffer.strip():
            if in_think_block:
                had_reasoning = True
                yield ("thinking", tag_buffer)
            else:
                had_content = True
                yield ("message", tag_buffer)

        # Fallback: if no reasoning was emitted at all, yield deterministic thinking
        if not had_reasoning:
            fallback_thinking = _build_thinking_fallback(execution_summary)
            if fallback_thinking:
                yield ("thinking", fallback_thinking)

        if not had_content:
            yield ("message", "Standing by, sir.")

    except Exception as e:
        print(f"[Voice of Jarvis Stream] Failed: {e}")
        thinking = _build_thinking_fallback(execution_summary)
        if thinking:
            yield ("thinking", thinking)
        msg = "Your schedule is set, sir." if execution_summary.get("schedule_generated") else "All sorted, sir."
        yield ("message", msg)
