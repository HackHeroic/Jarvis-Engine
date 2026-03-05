"""Voice of Jarvis: synthesize a single warm message from execution summary."""

import re
from typing import Any

from app.core.config import SLM_ROUTER_MODEL
from app.models.brain.litellm_conf import hybrid_route_query

VOICE_OF_JARVIS_PROMPT = (
    "You are Jarvis, a warm, capable AI assistant. The user sent a message and we executed "
    "the following actions. Write a single, natural 1-2 sentence response that acknowledges "
    "what we did. Be concise but human. Do not list bullet points. "
    "If we built a schedule, end with something like 'Here's the plan' or 'Here's your schedule.' "
    "NEEDS_END_DATE_INSTRUCTION: If needs_end_date is true (we extracted a semester/timetable schedule "
    "but no end date was mentioned), you MUST politely ask the user when their finals/semester ends "
    "so we can set an expiration date for these classes. Include this naturally in your response."
)


def _extract_thinking_process(raw_text: str) -> tuple[str, str | None]:
    """Extract think blocks and strip from main message."""
    thinking_process = None
    think_match = re.search(r"<think>(.*?)</think>", raw_text, flags=re.DOTALL | re.IGNORECASE)
    if think_match:
        thinking_process = think_match.group(1).strip()

    clean_text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE)
    if "Thinking Process" in clean_text or "Draft:" in clean_text:
        parts = re.split(r"Draft:|Final Polish:", clean_text, flags=re.IGNORECASE)
        clean_text = parts[-1].strip() if len(parts) > 1 else clean_text.split("\n\n")[-1]
    clean_text = clean_text.strip().replace('"', "").replace("*", "")
    if not clean_text:
        clean_text = "Done."
    return clean_text, thinking_process


async def synthesize_jarvis_response(execution_summary: dict[str, Any]) -> tuple[str, str | None]:
    """Generate message and extract thinking process. Returns (message, thinking_process)."""
    if execution_summary.get("spread_across_days"):
        return (
            "I've spread this across multiple days to fit your constraints. Here's your schedule.",
            None,
        )
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

    if not parts:
        return "Done.", None

    summary_text = "\n".join(parts)
    try:
        result = await hybrid_route_query(
            user_prompt=summary_text,
            system_prompt=VOICE_OF_JARVIS_PROMPT,
            response_schema=None,
            model_override=SLM_ROUTER_MODEL,
        )
        msg = result if isinstance(result, str) else str(result)
        msg = msg.strip() if msg else "Done."
        return _extract_thinking_process(msg)
    except Exception as e:
        print(f"[Voice of Jarvis] Synthesis failed: {e}")
        if execution_summary.get("spread_across_days"):
            return (
                "I've spread this across multiple days to fit your constraints. Here's your schedule.",
                None,
            )
        if execution_summary.get("schedule_generated"):
            return "Here's your schedule.", None
        if execution_summary.get("habits_saved"):
            return "I've noted your preferences.", None
        return "Done.", None
