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
    "You are Jarvis, a warm, capable AI assistant. The user sent a message and we executed "
    "the following actions.\n\n"
    "You MUST use this EXACT format — no exceptions:\n"
    "<think>\n"
    "2-4 sentences explaining what we did internally.\n"
    "</think>\n"
    "Your warm 1-2 sentence response to the user.\n\n"
    "RULES:\n"
    "- You MUST start your response with <think> (the literal XML tag)\n"
    "- You MUST close the thinking block with </think> (the literal XML tag)\n"
    "- After </think>, write ONLY your 1-2 sentence response — nothing else\n"
    "- Do NOT number steps, do NOT use bullet points, do NOT explain your process outside the tags\n"
    "- Be concise and human\n"
    "- If we built a schedule, end with something like 'Here's your schedule.'\n"
    "- NEEDS_END_DATE_INSTRUCTION: If needs_end_date is true, politely ask when their semester ends\n\n"
    "EXAMPLE:\n"
    "<think>\n"
    "I classified the user's input as a planning request and decomposed their goal into 6 micro-tasks. "
    "I applied their gym habit as a hard block and ran the scheduler.\n"
    "</think>\n"
    "I've broken down your study plan into focused 25-minute tasks and scheduled them around your gym sessions. "
    "Here's your schedule."
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
            "User sent a greeting. Routed via 4B SLM (no planning goal detected). "
            "Responding warmly and surfacing what Jarvis can do."
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
        clean_text = "Done."
    return clean_text, thinking_process


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
        return "Done.", None

    summary_text = "\n".join(parts)
    try:
        # Include memory context in system prompt for personalization
        voj_prompt = VOICE_OF_JARVIS_PROMPT
        mem_ctx = execution_summary.get("memory_context", "")
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
        msg = msg.strip() if msg else "Done."
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
        return "Done.", thinking


async def synthesize_jarvis_response_stream(
    execution_summary: dict[str, Any],
) -> AsyncGenerator[tuple[str, str], None]:
    """Stream (event_type, token) pairs for live thinking display.

    event_type is 'thinking' while inside <think>...</think> and 'message' outside.
    Yields tokens as they arrive for real-time UI updates.
    Falls back to yielding the deterministic thinking then the fallback message if LLM fails.
    """
    if execution_summary.get("spread_across_days"):
        thinking = _build_thinking_fallback(execution_summary) or ""
        if thinking:
            yield ("thinking", thinking)
        yield ("message", "I've spread this across multiple days to fit your constraints. Here's your schedule.")
        return

    parts = _build_voj_parts(execution_summary)
    if not parts:
        yield ("message", "Done.")
        return

    summary_text = "\n".join(parts)
    try:
        token_gen = await hybrid_route_query(
            user_prompt=summary_text,
            system_prompt=VOICE_OF_JARVIS_PROMPT,
            response_schema=None,
            model_override=_cfg.SLM_ROUTER_MODEL,
            stream=True,
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
            yield ("message", "Done.")

    except Exception as e:
        print(f"[Voice of Jarvis Stream] Failed: {e}")
        thinking = _build_thinking_fallback(execution_summary)
        if thinking:
            yield ("thinking", thinking)
        msg = "Here's your schedule." if execution_summary.get("schedule_generated") else "Done."
        yield ("message", msg)
