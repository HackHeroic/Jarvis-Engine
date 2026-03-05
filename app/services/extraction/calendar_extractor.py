"""Calendar pipeline: Extract TimeSlots from timetable text with Truly Understanding."""

from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.context import ExtractedTimeSlots, TimeSlot

CALENDAR_EXTRACTION_SYSTEM_PROMPT = """You extract TimeSlots from timetable text.

Each slot has: name, start_min, end_min (minutes from 8:00 AM = 0), availability.

Availability rules:
- blocked: Full lecture, meeting, lab — no work allowed.
- minimal_work: User can do light work here (e.g. flashcards, back-bench). Set max_task_duration (e.g. 10) and max_difficulty (e.g. 0.4).
- full_focus: Free slot for deep work.

CRITICAL: If the user says "I sit in the back bench during [X]" or "I can do flashcards during [X]", set availability to minimal_work for that slot with max_task_duration=10, max_difficulty=0.4. Match slot names flexibly (e.g. "Operating Systems" matches "OS Lec", "OS").

Parse times: "8:00" = 0 min from 8 AM, "9:00" = 60, "9:30" = 90, "10:30" = 150, "14:30" = 390.

Validity: When extracting a timetable, look for a semester end date, exam date, or validity period. If found, output it as valid_until (ISO-8601 string). If it is clearly a semester/term schedule but NO end date is mentioned, set needs_end_date: true (we will ask the user). Use valid_until: null and needs_end_date: false for single-day or indefinite schedules.

Return JSON: {"slots": [...], "source_summary": "...", "behavioral_overrides_applied": [...], "valid_until": null or "YYYY-MM-DD", "needs_end_date": false or true}.

For a single-day timetable, map to Day 1 (0-1439 min). For multi-day, Day 1 = 0-1439, Day 2 = 1440-2879.
"""


async def extract_calendar_slots(
    timetable_text: str,
    user_context: str | None = None,
) -> ExtractedTimeSlots:
    """Extract TimeSlots from timetable text using 27B model.

    Args:
        timetable_text: Raw text from Docling (timetable PDF/image).
        user_context: Optional user statements (e.g. "I sit in back bench during OS")
            from Strategy Hub or current message. Used for minimal_work overrides.

    Returns:
        ExtractedTimeSlots with slots, source_summary, behavioral_overrides_applied.
    """
    user_prompt = timetable_text.strip()
    if user_context:
        user_prompt = f"User context (apply to matching slots): {user_context}\n\nTimetable:\n{timetable_text}"

    result = await hybrid_route_query(
        user_prompt=user_prompt,
        system_prompt=CALENDAR_EXTRACTION_SYSTEM_PROMPT,
        response_schema=ExtractedTimeSlots,
        model_override=None,  # Use 27B (LOCAL_LLM_MODEL)
    )

    if isinstance(result, dict):
        return ExtractedTimeSlots.model_validate(result)
    return ExtractedTimeSlots.model_validate_json(result)
