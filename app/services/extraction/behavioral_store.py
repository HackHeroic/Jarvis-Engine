"""Behavioral pipeline: store the user's rules (behavioural constraints) in Strategy Hub (L7).

Rules are written and read through the ``ConstraintRepository`` port. A stored rule's
check-ins start at once (``RuleCheckIns.start``). Deleting a rule is
``StatedRules.delete_rule`` (``DELETE /habits/constraints/{id}``), which also lets go of
the memory of stating it and ends its check-ins.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.domain import errors
from app.domain.entities import BehavioralConstraint, MemorySource
from app.domain.ports import ConstraintRepository
from app.services.analytical.habit_translator import invalidate_habit_cache
from app.services.analytical.rule_check_ins import RuleCheckIns

logger = logging.getLogger(__name__)

# The most rules handed to calendar extraction as the user's context: the newest.
_CALENDAR_CONTEXT_RULES = 10


async def store_behavioral_constraint(
    constraints: ConstraintRepository,
    habit: str,
    *,
    user_id: str,
    constraint_type: str = "habit",
    check_ins: RuleCheckIns | None = None,
    learned_at: datetime | None = None,
) -> dict[str, Any]:
    """Store a rule the user stated (e.g. "no study before 11am") for every later plan.

    ``check_ins`` starts Jarvis's check-ins on it ("still how you work?"), the first due
    tomorrow; best effort, so the rule is stored even when that fails. None (tests,
    scripts): the next read of the check-ins starts them. ``learned_at`` is when it was
    said; None: when it is stored (the demo seed passes its own clock).

    Returns ``{"status": "stored", "id": ...}`` (the id at once, so the rule can be
    linked to the memory of stating it), or ``{"status": "error", "error": ...}`` with a
    fixed text when the store fails: the result can reach an HTTP response
    (/ingestion/process), so the cause is logged, never returned.
    """
    rule = BehavioralConstraint(
        id=str(uuid4()),
        user_id=user_id,
        raw_text=habit,
        constraint_type=constraint_type,
        recurrence="daily",
        structured_semantics={},
        source=MemorySource.SAID,
        created_at=learned_at,
    )
    try:
        stored = await constraints.add(user_id, rule)
    except errors.JarvisError as exc:
        logger.warning("Could not store behavioral constraint (%s)", exc.code)
        return {"status": "error", "error": "Could not save this preference."}
    invalidate_habit_cache()
    if check_ins is not None:
        await check_ins.start(user_id, stored)
    return {"status": "stored", "id": stored.id}


async def get_behavioral_context_for_calendar(
    constraints: ConstraintRepository, user_id: str
) -> str:
    """The user's newest rules that apply now, joined by "; ", as context for calendar
    extraction. "" when they have none, or they cannot be read (the extraction then runs
    without context)."""
    try:
        rules = await constraints.list_active(user_id)
    except errors.JarvisError as exc:
        logger.warning("Could not read rules for calendar context (%s)", exc.code)
        return ""
    newest = list(reversed(rules))[:_CALENDAR_CONTEXT_RULES]
    return "; ".join(rule.raw_text for rule in newest if rule.raw_text)


_SYSTEM_FALLBACK_PHRASES = {
    "standing by, sir.",
    "at your service, sir.",
    "all sorted, sir.",
    "your schedule is set, sir.",
    "a minor hiccup on my end, sir. shall we try that again?",
    "done.",
    "hello!",
    "hey! what can i help with?",
}


def is_system_fallback(text: str) -> bool:
    """True if text matches a known system fallback phrase. Used to filter
    fallback strings out of habit staging — never save 'Standing by, sir.'
    as a behavioral constraint."""
    if not text:
        return True
    return text.strip().lower() in _SYSTEM_FALLBACK_PHRASES


def stage_habit(raw_text: str, user_id: str) -> dict | None:
    """Create a habit proposal without persisting to DB.

    Returns None if raw_text is empty, a system fallback string, or trivially
    short (< 5 chars). The orchestrator's habit-staging loop must drop None
    entries — never offer 'Standing by, sir.' as 'New habit detected'.
    """
    cleaned = (raw_text or "").strip()
    if not cleaned or is_system_fallback(cleaned) or len(cleaned) < 5:
        return None
    return {
        "raw_text": cleaned,
        "user_id": user_id,
        "constraint_type": "behavioral",
        "status": "staged",
    }
