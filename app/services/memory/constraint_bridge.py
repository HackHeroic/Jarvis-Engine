# app/services/memory/constraint_bridge.py
"""Bridge between archival memory and the deterministic scheduler.

This is what makes Jarvis different from ChatGPT's memory:
- ChatGPT memory: affects what the LLM says
- Jarvis memory: affects the MATHEMATICAL CONSTRAINTS in OR-Tools

A behavioral pattern like "user skips morning tasks" doesn't just make
Jarvis say "I notice you prefer afternoons" — it makes the scheduler
STOP SCHEDULING deep work before 10 AM.
"""

import re
from collections.abc import Iterable

from app.core.config import DAY_START_HOUR
from app.domain.entities import Memory
from app.schemas.context import TimeSlot
from app.services.memory.patterns import SkipTimeWindowDetector
from app.services.planning.rule_windows import (
    blocked_windows,
    horizon_window,
    parse_time_range,
    to_horizon,
)

# A learned skip window at least this sure becomes a minimal-work hour.
SKIP_WINDOW_MIN_CONFIDENCE = 0.6
# A stated rule claims its window for focused work when it names such work and no
# negation: "Deep work 9-10 every morning" claims 09:00-10:00, "No study before 11 AM"
# claims nothing.
_FOCUSED_WORK = re.compile(
    r"\b(deep work|focus\w*|stud(?:y|ying)|revis\w*|cod(?:e|ing)|writ(?:e|ing))\b", re.IGNORECASE
)
_NEGATION = re.compile(r"\b(no|not|never|don'?t|avoid\w*|without)\b", re.IGNORECASE)
# "9-10" or "2 to 4" with no AM/PM; never a piece of a date such as 2026-11-14.
_BARE_HOURS = re.compile(r"(?<![\d/-])(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})(?![\d/:-])")


def _skip_window_hour(pattern_key: str | None) -> int | None:
    """The local hour of a ``skip_time_window:{h}`` pattern key; None for any other key."""
    kind, _, hour = (pattern_key or "").partition(":")
    if kind != SkipTimeWindowDetector.name or not hour.isdigit() or int(hour) > 23:
        return None
    return int(hour)


def _bare_hour(hour: int) -> int:
    """A bare hour on the clock it most likely means: before the day starts reads as PM."""
    return hour + 12 if 1 <= hour < DAY_START_HOUR else hour


def _claimed_window(text: str) -> tuple[int, int] | None:
    """The window (horizon minutes) a stated rule claims for focused work, or None."""
    if not _FOCUSED_WORK.search(text) or _NEGATION.search(text):
        return None
    window = parse_time_range(text)
    if window is None and (bare := _BARE_HOURS.search(text)):
        start_h, end_h = (_bare_hour(int(h)) for h in bare.groups())
        if start_h <= 23 and end_h <= 24:
            window = horizon_window(start_h * 60, end_h * 60)
    return window


def memory_slots(
    memories: list[Memory],
    *,
    habits: Iterable[str] = (),
    covered: frozenset[str] = frozenset(),
) -> list[TimeSlot]:
    """Turn the user's memories in use into solver constraints.

    A ``constraint`` memory becomes a hard block over the hours its words keep free
    (``rule_windows.blocked_windows``: "never after 10 PM" blocks 22:00 onwards, "only
    after 6 PM" the day before 18:00, "free after 5 PM" nothing), unless a
    stored rule already covers it (its id is in ``covered``: the rule the user stated
    carries the id of the memory of stating it). Planning has that rule from the
    rules, so the memory never blocks the same hours a second time, and letting go of
    either one frees them. A learned
    skip window (pattern key ``skip_time_window:{h}``) at least 0.6 sure becomes a
    minimal-work hour. Nothing else blocks time: not a goal or a fact, and not any
    other pattern. A peak hour is where demanding work belongs, and reading "hour N"
    off a pattern's words used to turn it into a minimal-work window.

    The user's own word beats what Jarvis inferred. A stated rule (a ``constraint``
    memory, or one of ``habits``, the user's habit texts) that claims a window for
    focused work ("Deep work 9-10 every morning") is not a block, and no learned skip
    window is laid over the hours it claims.

    Slots carry horizon minutes (0 = DAY_START_HOUR) and are ``recurring``: these are
    standing rules, not one-off calendar entries.
    """
    rules = [m.content for m in memories if m.memory_type == "constraint"]
    claims = [w for w in map(_claimed_window, [*habits, *rules]) if w is not None]
    slots: list[TimeSlot] = []
    for memory in memories:
        if memory.memory_type == "constraint":
            if memory.id in covered:
                continue
            if _claimed_window(memory.content) is not None:
                continue
            for i, (start, end) in enumerate(blocked_windows(memory.content)):
                slots.append(TimeSlot(
                    name=f"memory_constraint_{memory.id}" + (f"_{i + 1}" if i else ""),
                    start_min=start,
                    end_min=end,
                    availability="blocked",
                    recurring=True,
                    source="user",
                ))
        elif (
            memory.memory_type == "behavioral_pattern"
            and memory.confidence >= SKIP_WINDOW_MIN_CONFIDENCE
        ):
            hour = _skip_window_hour(memory.pattern_key)
            if hour is None:
                continue
            # The end follows the start, so hour 7 (1380-1440) stays a
            # forward-going interval.
            start_min = to_horizon(hour * 60)
            if any(start < start_min + 60 and start_min < end for start, end in claims):
                continue  # the user keeps this hour for focused work
            slots.append(TimeSlot(
                name=f"pearl_pattern_{memory.id}",
                start_min=start_min,
                end_min=start_min + 60,
                availability="minimal_work",
                recurring=True,
                source="pearl_inferred",
            ))
    return slots
