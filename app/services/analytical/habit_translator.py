"""Translate text habits into semantic slots via 27B. Horizon expansion happens in horizon_expander."""

import hashlib
import json
import logging
import re
import time as _time
from typing import List, Optional

from app.schemas.context import SemanticTimeSlot, SemanticTimeSlotsResponse
from app.services.planning.rule_windows import DAY_START_MIN, MINUTES_PER_DAY, blocked_windows

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


def is_translation_cached(habits_text: str) -> bool:
    """Whether ``translate_habits_to_slots(habits_text)`` would answer from the cache now.

    Planning asks before taking the PRIMARY lock: a cached translation calls no model,
    so it never waits for another heavy call.
    """
    cached = _habit_cache.get(hashlib.sha256(habits_text.encode()).hexdigest())
    return cached is not None and (_time.time() - cached[0]) < _CACHE_TTL_S


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


# A habit that names its own days or dates ("on Fridays", "weekends", "every Monday",
# "tonight") is the model's to expand: the words alone give only a daily slot.
_NAMES_ITS_DAYS = re.compile(
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b|\bweek(?:day|end)s?\b"
    r"|\b(?:every|each)\s+(?!(?:single\s+)?(?:day|night|evening|morning|afternoon)s?\b)\w+"
    r"|\b(?:today|tonight|tomorrow|semester|exams?)\b|\b(?:this|next)\s+week\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)


def _python_fallback_for_time_anchor(habit: str) -> Optional[List[SemanticTimeSlot]]:
    """The slots one habit's own words give, every day, with no model call.

    The hours come from ``rule_windows.blocked_windows``: "never work after 10 PM"
    blocks 22:00 onwards (night included), "I work only after 6 PM" blocks 08:00-18:00,
    "I'm free after 5 PM" blocks nothing. None when the words give no window, or name
    days of their own; the model reads those.
    """
    if _NAMES_ITS_DAYS.search(habit):
        return None
    slots = [
        SemanticTimeSlot(
            name=f"stated_rule_{_hhmm(start)}_{_hhmm(end)}",
            start_min=start,
            end_min=end,
            availability="blocked",
            recurrence="daily",
        )
        for start, end in blocked_windows(habit)
    ]
    return slots or None


def _hhmm(horizon_min: int) -> str:
    """A horizon minute as the local clock shows it: 840 -> "2200"."""
    minutes = (DAY_START_MIN + horizon_min) % MINUTES_PER_DAY
    return f"{minutes // 60:02d}{minutes % 60:02d}"


async def translate_habits_to_slots(habits_text: str) -> List[SemanticTimeSlot]:
    """Convert raw habit text (one habit per line) to semantic slots.

    A habit whose own words give its hours is read with no model call
    (``_python_fallback_for_time_anchor``); only the others go to the PRIMARY model.
    Returns SemanticTimeSlot list for horizon expansion. Short-circuit: [] if habits_text empty.
    Uses in-memory cache to avoid redundant model calls for unchanged habits.
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

    # --- Python-first: each habit whose words give its hours needs no model call ---
    known: list[SemanticTimeSlot] = []
    unread: list[str] = []
    for habit in (line.strip() for line in habits_text.splitlines()):
        if habit:
            read = _python_fallback_for_time_anchor(habit)
            known.extend(read or [])
            if not read:
                unread.append(habit)
    if not unread:
        logger.info("Habit translator: every habit read without the model (%d slots)", len(known))
        _habit_cache[cache_key] = (_time.time(), known)
        return known

    slots = await _translate_with_model("\n".join(unread)) + known

    # --- Cache store ---
    if slots:
        _habit_cache[cache_key] = (_time.time(), slots)
        logger.info("Habit translation cached (%d slots)", len(slots))

    return slots


async def _translate_with_model(habits_text: str) -> List[SemanticTimeSlot]:
    """The habits the words alone could not read, translated by the PRIMARY model."""
    from app.core.model_router import route_llm_call

    result = await route_llm_call(
        task="habit_translation",
        prompt=habits_text,
        system_prompt=HABIT_TRANSLATOR_PROMPT,
        response_schema=SemanticTimeSlotsResponse,
    )
    parsed = SemanticTimeSlotsResponse.model_validate(result)

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
            strict_result = await route_llm_call(
                task="habit_translation",
                prompt=strict_prompt,
                system_prompt="Output JSON with semantic_slots array. No other text.",
                response_schema=SemanticTimeSlotsResponse,
            )
            strict_parsed = SemanticTimeSlotsResponse.model_validate(strict_result)
            if strict_parsed.semantic_slots:
                slots = strict_parsed.semantic_slots
                logger.info("Habit translator: LLM strict fallback produced slots for time anchor")
        except (json.JSONDecodeError, ValueError, Exception) as e:
            logger.warning("Habit translator strict fallback failed: %s", e)
    return list(slots)
