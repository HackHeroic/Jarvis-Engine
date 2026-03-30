"""Translate text habits into semantic slots via 27B. Horizon expansion happens in horizon_expander."""

import hashlib
import json
import logging
import re
import time as _time
from typing import List, Optional

from app.core.config import SLM_ROUTER_MODEL
from app.models.brain.litellm_conf import gemini_primary_route, hybrid_route_query
from app.schemas.context import SemanticTimeSlot, SemanticTimeSlotsResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Habit translation cache — avoids redundant 27B calls for unchanged habits
# Key: SHA-256 of habits text. Value: (timestamp, List[SemanticTimeSlot]).
# ---------------------------------------------------------------------------
_habit_cache: dict[str, tuple[float, list[SemanticTimeSlot]]] = {}
_CACHE_TTL_S = 86400  # 24 hours (was 3600)


def invalidate_habit_cache() -> None:
    """Clear the habit translation cache. Call after habits are stored/deleted."""
    _habit_cache.clear()
    logger.info("Habit translation cache invalidated")


def invalidate_translation_cache() -> None:
    """Clear habit translation cache when habits are modified."""
    _habit_cache.clear()
    logger.info("Habit translation cache invalidated (translation cache)")

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


def _python_fallback_for_time_anchor(habits_text: str) -> Optional[List[SemanticTimeSlot]]:
    """Deterministic fallback for common time-anchor patterns. No LLM needed.

    Returns slots if a known pattern matches, None otherwise (caller should use LLM retry).
    """
    lower = habits_text.lower()
    slots: list[SemanticTimeSlot] = []

    # "before X AM/PM" or "until X AM/PM" or "no work before X"
    before_match = re.search(
        r"(?:before|until|no\s+(?:work|tasks?|scheduling?)\s+(?:before|until))\s+(\d{1,2})\s*(am|pm)",
        lower,
    )
    if before_match:
        hour = int(before_match.group(1))
        period = before_match.group(2)
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        end_min = max(0, (hour - 8) * 60)  # day starts at 8 AM = minute 0
        if end_min > 0:
            slots.append(SemanticTimeSlot(
                name="morning_restriction",
                start_min=0,
                end_min=end_min,
                availability="blocked",
                recurrence="daily",
            ))

    # "before noon" / "no work before noon" / "until noon"
    if not slots and re.search(r"(?:before|until|no\s+work\s+(?:before|until))\s+noon", lower):
        slots.append(SemanticTimeSlot(
            name="morning_restriction",
            start_min=0,
            end_min=240,  # noon = 12 PM = 4h from 8 AM
            availability="blocked",
            recurrence="daily",
        ))

    # "after X AM/PM" or "only after X"
    after_match = re.search(r"(?:only\s+)?after\s+(\d{1,2})\s*(am|pm)", lower)
    if after_match:
        hour = int(after_match.group(1))
        period = after_match.group(2)
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        start_min = max(0, (hour - 8) * 60)
        # Block everything before the "after" time
        if start_min > 0:
            slots.append(SemanticTimeSlot(
                name="morning_restriction",
                start_min=0,
                end_min=start_min,
                availability="blocked",
                recurrence="daily",
            ))

    # "after noon" / "after lunch"
    if not slots and re.search(r"(?:only\s+)?after\s+(?:noon|lunch)", lower):
        slots.append(SemanticTimeSlot(
            name="morning_restriction",
            start_min=0,
            end_min=240,  # block before noon
            availability="blocked",
            recurrence="daily",
        ))

    # "evening only" / "only in the evening"
    if not slots and re.search(r"(?:evening\s+only|only\s+(?:in\s+(?:the\s+)?)?evening)", lower):
        slots.append(SemanticTimeSlot(
            name="morning_restriction",
            start_min=0,
            end_min=600,  # block before 6 PM (8 AM + 600 min)
            availability="blocked",
            recurrence="daily",
        ))

    # "gym at X-Y" or "gym from X to Y" — block that time range
    gym_match = re.search(
        r"(?:gym|workout|exercise|training)\s+(?:at|from)\s+(\d{1,2})\s*(am|pm)?\s*[-–to]+\s*(\d{1,2})\s*(am|pm)",
        lower,
    )
    if gym_match:
        start_h = int(gym_match.group(1))
        start_p = gym_match.group(2) or gym_match.group(4)  # infer AM/PM from end if missing
        end_h = int(gym_match.group(3))
        end_p = gym_match.group(4)
        if start_p and start_p == "pm" and start_h != 12:
            start_h += 12
        if end_p == "pm" and end_h != 12:
            end_h += 12
        gym_start = max(0, (start_h - 8) * 60)
        gym_end = max(0, (end_h - 8) * 60)
        if gym_end > gym_start:
            slots.append(SemanticTimeSlot(
                name="gym_block",
                start_min=gym_start,
                end_min=gym_end,
                availability="blocked",
                recurrence="daily",
            ))

    # "class/meeting at X AM/PM" or "class from X to Y"
    class_match = re.search(
        r"(?:class|meeting|lecture)\s+(?:at|from)\s+(\d{1,2})\s*(am|pm)?\s*[-–to]*\s*(\d{1,2})?\s*(am|pm)?",
        lower,
    )
    if class_match:
        start_h = int(class_match.group(1))
        start_p = class_match.group(2) or class_match.group(4)
        end_h = int(class_match.group(3)) if class_match.group(3) else start_h + 1
        end_p = class_match.group(4) or start_p
        if start_p and start_p == "pm" and start_h != 12:
            start_h += 12
        if end_p and end_p == "pm" and end_h != 12:
            end_h += 12
        cls_start = max(0, (start_h - 8) * 60)
        cls_end = max(0, (end_h - 8) * 60)
        if cls_end > cls_start:
            slots.append(SemanticTimeSlot(
                name="class_block",
                start_min=cls_start,
                end_min=cls_end,
                availability="blocked",
                recurrence="daily",
            ))

    # "mornings" / "hate mornings" (conservative: block 0-180 = 8-11 AM)
    if not slots and re.search(r"\bmorning", lower):
        slots.append(SemanticTimeSlot(
            name="morning_restriction",
            start_min=0,
            end_min=180,
            availability="blocked",
            recurrence="daily",
        ))

    return slots if slots else None


async def translate_habits_to_slots(habits_text: str) -> List[SemanticTimeSlot]:
    """Convert raw habit text to semantic slots via 27B.

    Returns SemanticTimeSlot list for horizon expansion. Short-circuit: [] if habits_text empty.
    Uses in-memory cache to avoid redundant 27B calls for unchanged habits.
    """
    if not habits_text or not habits_text.strip():
        return []

    # --- Cache check ---
    cache_key = hashlib.sha256(habits_text.encode()).hexdigest()
    cached = _habit_cache.get(cache_key)
    if cached:
        ts, cached_slots = cached
        if (_time.time() - ts) < _CACHE_TTL_S:
            logger.info("Habit translation cache HIT (%d slots)", len(cached_slots))
            return cached_slots
        else:
            del _habit_cache[cache_key]

    # --- Python-first: try deterministic pattern matching before any LLM call ---
    python_slots = _python_fallback_for_time_anchor(habits_text)
    if python_slots:
        slots = python_slots
        logger.info("Habit translator: Python fallback matched %d slots, skipping 27B", len(slots))
        _habit_cache[cache_key] = (_time.time(), slots)
        return slots

    # --- Primary Gemini Flash call (only if Python fallback didn't match) ---
    result = await gemini_primary_route(
        user_prompt=habits_text,
        system_prompt=HABIT_TRANSLATOR_PROMPT,
        response_schema=SemanticTimeSlotsResponse,
        fallback_model=SLM_ROUTER_MODEL,
    )
    if isinstance(result, dict):
        parsed = SemanticTimeSlotsResponse.model_validate(result)
    else:
        parsed = SemanticTimeSlotsResponse.model_validate_json(result)

    slots = parsed.semantic_slots or []

    # Post-translation validation: time anchors present but no matching slot
    # -> LLM strict retry as last resort (skip the old fallback retry entirely)
    if not slots and _TIME_ANCHOR_PATTERN.search(habits_text):
        strict_prompt = (
            f"User said: {habits_text}\n\n"
            "Output a blocked or minimal_work slot. "
            "If they said 'before 11 AM' or 'morning' or 'no work before X': "
            "start_min 0, end_min 180, availability blocked, recurrence daily. "
            'Return JSON: {{"semantic_slots": [{{"name": "morning_restriction", "start_min": 0, '
            '"end_min": 180, "availability": "blocked", "recurrence": "daily"}}]}}'
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
                logger.info("Habit translator: LLM strict fallback produced slots for time anchor")
        except (json.JSONDecodeError, ValueError, Exception) as e:
            logger.warning("Habit translator strict fallback failed: %s", e)

    # --- Cache store ---
    if slots:
        _habit_cache[cache_key] = (_time.time(), slots)
        logger.info("Habit translation cached (%d slots)", len(slots))

    return slots
