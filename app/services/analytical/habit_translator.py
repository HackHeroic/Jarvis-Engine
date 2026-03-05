"""Translate text habits into semantic slots via 27B. Horizon expansion happens in horizon_expander."""

import json
import logging
import re
from typing import List

from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.context import SemanticTimeSlot, SemanticTimeSlotsResponse

logger = logging.getLogger(__name__)

# Time anchors that require a slot: if present but translator returns empty/wrong, retry
_TIME_ANCHOR_PATTERN = re.compile(
    r"before\s+\d{1,2}\s*(?:AM|PM|am|pm)|"
    r"until\s+\d{1,2}\s*(?:AM|PM|am|pm)|"
    r"until\s+noon|before\s+noon|"
    r"after\s+\d{1,2}\s*(?:AM|PM|am|pm)|"
    r"after\s+noon|"
    r"morning|mornings",
    re.IGNORECASE,
)

HABIT_TRANSLATOR_PROMPT = (
    "You are a STRICT ENFORCER. Translate verbatim habit text into schedule constraints. "
    "You receive exact user wording. Every time anchor (11 AM, noon, 2 PM) maps to specific start_min/end_min. "
    "Do not skip or approximate. For any habit mentioning a time or 'morning', you MUST output at least one slot.\n\n"
    "MANDATORY TIME ANCHOR TABLE (day starts 0 = 8:00 AM; 1440 = 8:00 AM next day):\n"
    "- 'before 11 AM' / 'until 11 AM' / 'before noon' -> end_min 180 (11 AM = 180 min from 8 AM)\n"
    "- 'before 10 AM' -> end_min 120\n"
    "- 'after 12 PM' / 'after noon' -> start_min 240\n"
    "- 'after 2 PM' -> start_min 360\n"
    "- 'mornings' (without time) -> 0-180 (conservative default)\n"
    "- 'evening' -> start_min 600\n"
    "- 'after lunch' -> start_min 480\n\n"
    "RECURRENCE: Infer from phrasing. Default: daily. 'weekdays' = Mon-Fri; 'weekends' = Sat-Sun; "
    "'every Monday' = weekly + weekday 0; 'every Friday' = weekly + weekday 4; monthly/yearly/once when explicit.\n"
    "WEEKDAY: 0=Mon, 1=Tue, ..., 6=Sun. Set only for weekly/monthly (e.g. 'every Monday' -> 0). Use null otherwise.\n"
    "VALIDITY: If user says 'until exams', 'this semester', set valid_until to placeholder (e.g. 'semester_end'). Use null for indefinite.\n\n"
    "STRICT RULE: If the user says 'never schedule work' or 'no work before X' or 'never schedule before X', "
    "use 'blocked' (not minimal_work) for that time range — NO tasks may overlap.\n\n"
    "NEVER use full_focus for time ranges the user wants to avoid or limit. "
    "Use 'blocked' for complete avoidance (sleep, meetings, 'no work before X'). "
    "Use 'minimal_work' only for 'only easy/short tasks' (e.g. 'no heavy work before 11 AM' -> max_difficulty 0.3, max_task_duration 10).\n\n"
    "Examples: 'never schedule work before 11 AM' -> blocked 0-180, recurrence daily; "
    "'I hate mornings' -> minimal_work 0-180; "
    "'I have a meeting 2-3 PM' -> blocked 840-900, recurrence once; "
    "'every Monday no meetings' -> blocked, recurrence weekly, weekday 0.\n\n"
    "Output strictly valid JSON with a 'semantic_slots' array. "
    "Each slot: name, start_min, end_min, availability, recurrence (default daily), "
    "weekday (optional), valid_from (optional), valid_until (optional), max_task_duration, max_difficulty."
)

HABIT_TRANSLATOR_FALLBACK_PROMPT = (
    "The user's constraints were: {habits_text}\n\n"
    "The previous translation returned no slots. Use this MANDATORY mapping: "
    "'before 11 AM'/'until 11 AM'/'before noon'/'mornings' -> slot with start_min 0, end_min 180, availability blocked, recurrence daily. "
    "'never schedule work'/'no work before X' -> blocked. "
    "'no heavy work before X' -> minimal_work with max_difficulty 0.3, max_task_duration 10. "
    "You MUST output at least one slot. Return strictly valid JSON with a 'semantic_slots' array."
)


async def translate_habits_to_slots(habits_text: str) -> List[SemanticTimeSlot]:
    """Convert raw habit text to semantic slots via 27B.

    Returns SemanticTimeSlot list for horizon expansion. Short-circuit: [] if habits_text empty.
    """
    if not habits_text or not habits_text.strip():
        return []

    result = await hybrid_route_query(
        user_prompt=habits_text,
        system_prompt=HABIT_TRANSLATOR_PROMPT,
        response_schema=SemanticTimeSlotsResponse,
        model_override=None,
    )
    if isinstance(result, dict):
        parsed = SemanticTimeSlotsResponse.model_validate(result)
    else:
        parsed = SemanticTimeSlotsResponse.model_validate_json(result)

    slots = parsed.semantic_slots or []

    # LLM retry fallback: if empty for non-empty habits, retry with fallback prompt
    if not slots and habits_text.strip():
        fallback_prompt = HABIT_TRANSLATOR_FALLBACK_PROMPT.format(habits_text=habits_text)
        try:
            retry_result = await hybrid_route_query(
                user_prompt=fallback_prompt,
                system_prompt="You are a habit-to-schedule translator. Output JSON with semantic_slots array.",
                response_schema=SemanticTimeSlotsResponse,
                model_override=None,
            )
            if isinstance(retry_result, dict):
                retry_parsed = SemanticTimeSlotsResponse.model_validate(retry_result)
            else:
                retry_parsed = SemanticTimeSlotsResponse.model_validate_json(retry_result)
            slots = retry_parsed.semantic_slots or []
        except (json.JSONDecodeError, ValueError, Exception) as e:
            logger.warning("Habit translator retry failed: %s", e)
            return []

    # Post-translation validation: time anchors present but no morning slot -> retry with explicit mapping
    if _TIME_ANCHOR_PATTERN.search(habits_text) and habits_text.strip():
        has_morning_slot = any(
            s.start_min < 180 and s.end_min > 0 for s in slots
        )
        if not has_morning_slot:
            strict_prompt = (
                f"User said: {habits_text}\n\n"
                "Output a blocked or minimal_work slot. "
                "If they said 'before 11 AM' or 'morning' or 'no work before X': "
                "start_min 0, end_min 180, availability blocked, recurrence daily. "
                'Return JSON: {"semantic_slots": [{"name": "morning_restriction", "start_min": 0, '
                '"end_min": 180, "availability": "blocked", "recurrence": "daily"}]}'
            )
            try:
                strict_result = await hybrid_route_query(
                    user_prompt=strict_prompt,
                    system_prompt="Output JSON with semantic_slots array. No other text.",
                    response_schema=SemanticTimeSlotsResponse,
                    model_override=None,
                )
                if isinstance(strict_result, dict):
                    strict_parsed = SemanticTimeSlotsResponse.model_validate(strict_result)
                else:
                    strict_parsed = SemanticTimeSlotsResponse.model_validate_json(strict_result)
                if strict_parsed.semantic_slots:
                    slots = strict_parsed.semantic_slots
                    logger.info("Habit translator: strict fallback produced slots for time anchor")
            except (json.JSONDecodeError, ValueError, Exception) as e:
                logger.warning("Habit translator strict fallback failed: %s", e)

    return slots
