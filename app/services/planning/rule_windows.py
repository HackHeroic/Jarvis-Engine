"""The hours a stated rule keeps free, read off its words with no model call.

A rule names a window ("after 10 PM", "before 11 AM", "between 1 and 2 PM") and says
what to do with it:

* A negation ("never", "no", "don't", "avoid", "not", "nothing"), "keep ... free" or
  "busy" blocks the window it names. "Never work after 10 PM" blocks 22:00 to the end
  of the day, night included; it does not block the day before 22:00.
* A restriction ("only", "except", or "just" with no negation) allows the window and
  blocks the rest of the day. "I work only after 6 PM" blocks 08:00-18:00.
* Availability ("I'm free after 5 PM"), how well work goes ("my best work is after
  9 PM") and a window with no rule word block nothing. The exception is a range for a
  fixed activity ("Gym from 6 to 7 pm"), which is occupied.

Words without a clock time ("after lunch", "mornings") are not read here; the habit
translator leaves them to the model. A bare hour ("after 6") is read the way that
blocks less: "no work after 6" is 18:00, "no work before 6" is 06:00 (nothing to block).

Windows are horizon minutes, the frame ``TimeSlot`` uses: 0 is DAY_START_HOUR (08:00
local), 960 is midnight, 1440 is 08:00 the next morning.
"""

import re
from dataclasses import dataclass

from app.core.config import DAY_START_HOUR

MINUTES_PER_DAY = 1440
DAY_START_MIN = DAY_START_HOUR * 60  # horizon minute 0 == 08:00 on the user's clock

Window = tuple[int, int]

_PERIOD = r"[ap]\.?\s?m\b\.?"
_DURATION = r"(?!\s*(?:weeks?|days?|hours?|hrs?|minutes?|mins?|months?|years?)\b)"
_CLOCK = (
    rf"(?:noon|midday|midnight|(?<![\d:/.-])\d{{1,2}}(?::\d{{2}})?(?:\s*{_PERIOD})?"
    rf"(?![\d:]){_DURATION})"
)
# "between 1 and 2 PM", "from 1 PM to 2 PM", "13:00-14:00", "2 PM to 3 PM".
_RANGE = re.compile(
    rf"(?P<keyword>\b(?:between|from)\s+)?(?P<a>{_CLOCK})\s*(?:and|to|until|till|-|–|—)\s*"
    rf"(?P<b>{_CLOCK})"
)
_AFTER = re.compile(
    rf"\b(?:after|past)\s+(?P<t>{_CLOCK})|\bfrom\s+(?P<f>{_CLOCK})\s+on(?:wards)?\b"
)
_BEFORE = re.compile(rf"\b(?:before|until|till|til)\s+(?P<t>{_CLOCK})")
_ACTIVITY_AT = re.compile(
    rf"\b(?:gym|workout|exercise|training|class|meeting|lecture)\s+at\s+(?P<t>{_CLOCK})"
)

_NEGATION = re.compile(
    r"\b(?:never|no|not|nothing|don't|dont|do not|doesn't|avoid\w*|can't|cannot|won't|without)\b"
)
_KEEP_FREE = re.compile(
    r"\bkeep\b[^.;]*\b(?:free|clear|open|empty)\b|\bblock(?:ed|s)?\b"
    r"|\b(?:busy|unavailable|occupied)\b"
)
_RESTRICTION = re.compile(r"\b(?:only|solely|except)\b")
_JUST = re.compile(r"\bjust\b")
_AVAILABILITY = re.compile(r"\b(?:free|available|around|reachable)\b")
_WORKS_WELL = re.compile(r"\b(?:best|better|productive|peak|sharpest)\b|\bwork(?:s|ing)?\s+well\b")
_ACTIVITY = re.compile(r"\b(?:gym|workout|exercise|training|class(?:es)?|meeting|lecture)\b")


@dataclass(frozen=True)
class _Clock:
    """A time of day as written: ``period`` "am"/"pm" when given; ``exact`` for a
    24-hour time, noon or midnight (no reading to choose)."""

    hour: int
    minute: int
    period: str | None
    exact: bool


def to_horizon(clock_min: int) -> int:
    """Wall-clock minutes-from-midnight -> horizon minutes (0 = DAY_START_HOUR).

    Every consumer of ``TimeSlot`` (the horizon expander, the biological sleep fallback
    in ``run_schedule``, OR-Tools itself) anchors minute 0 at 08:00.
    """
    return (clock_min - DAY_START_MIN) % MINUTES_PER_DAY


def horizon_window(start_clock: int, end_clock: int) -> Window | None:
    """A wall-clock window in horizon minutes; None when it is empty or wraps across the
    08:00 anchor (it cannot be one interval then, and lies mostly in the night anyway)."""
    start, end = to_horizon(start_clock), to_horizon(end_clock)
    return (start, end) if start < end else None


def _clock(token: str) -> _Clock | None:
    token = token.strip()
    if token in ("noon", "midday"):
        return _Clock(12, 0, "pm", True)
    if token == "midnight":
        return _Clock(0, 0, "am", True)
    match = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap])?", token)
    hour, minute = int(match[1]), int(match[2] or 0)
    if minute > 59:
        return None
    if match[3]:
        return _Clock(hour, minute, match[3] + "m", False) if 1 <= hour <= 12 else None
    if match[2] is not None or hour == 0 or hour > 12:  # a 24-hour clock
        return _Clock(hour, minute, None, True) if hour < 24 else None
    return _Clock(hour, minute, None, False)  # a bare 1-12


def _wall(clock: _Clock, *, later: bool) -> int:
    """Minutes from midnight. A bare hour below 12 is read in the evening when ``later``."""
    hour = clock.hour % 12 if clock.period else clock.hour
    if clock.period == "pm" or (clock.period is None and not clock.exact and hour < 12 and later):
        hour += 12
    return hour * 60 + clock.minute


def _from(clock: _Clock | None) -> Window | None:
    """Blocked from ``clock`` to the end of the day (night included); a bare hour is read
    in the evening, which blocks less."""
    if clock is None:
        return None
    return (to_horizon(_wall(clock, later=True)), MINUTES_PER_DAY)


def _until(clock: _Clock | None) -> Window | None:
    """Blocked from the start of the day to ``clock``; a bare hour is read in the
    morning, which blocks less. None when ``clock`` is at or before the day starts."""
    if clock is None:
        return None
    wall = _wall(clock, later=False)
    if 0 < wall <= DAY_START_MIN:
        return None  # nothing schedulable before it
    return (0, to_horizon(wall)) if to_horizon(wall) > 0 else None


def _range(a: _Clock | None, b: _Clock | None, *, keyword: bool) -> Window | None:
    """The window a range names. A bare end takes the other end's AM/PM ("1 and 2 PM");
    two bare ends read 1-7 as the evening. Without "between"/"from", one end must be
    written as a time (AM/PM, HH:MM, noon), so "chapters 2-4" is no window."""
    if a is None or b is None:
        return None
    if not keyword and not (a.period or a.exact or b.period or b.exact):
        return None
    if a.period is None and not a.exact and b.period:
        a = _Clock(a.hour, a.minute, b.period, False)
        if _wall(a, later=False) >= _wall(b, later=False):
            a = _Clock(a.hour, a.minute, "am", False)  # "between 11 and 1 PM"
    if b.period is None and not b.exact and a.period:
        b = _Clock(b.hour, b.minute, a.period, False)
    start = _wall(a, later=a.hour < DAY_START_HOUR)
    end = _wall(b, later=b.hour < DAY_START_HOUR)
    return horizon_window(start, end)


@dataclass(frozen=True)
class _Named:
    """One window the words name: ``kind`` is "from" (after X), "until" (before X) or
    "range"; ``clock``/``end`` are its bounds as written."""

    kind: str
    clock: _Clock | None
    end: _Clock | None = None
    keyword: bool = False


def _named(text: str) -> list[_Named]:
    """Every window the text names, in the order written. An "until" inside a range
    ("from 9 until 5") is part of the range, not a window of its own."""
    named: list[tuple[int, _Named]] = []
    taken: list[tuple[int, int]] = []
    for m in _RANGE.finditer(text):
        found = _Named("range", _clock(m["a"]), _clock(m["b"]), keyword=bool(m["keyword"]))
        if _range(found.clock, found.end, keyword=found.keyword) is not None:
            named.append((m.start(), found))
            taken.append(m.span())

    def free(m: re.Match) -> bool:
        return not any(s < m.end() and m.start() < e for s, e in taken)

    for m in _AFTER.finditer(text):
        if free(m):
            named.append((m.start(), _Named("from", _clock(m["t"] or m["f"]))))
    for m in _BEFORE.finditer(text):
        if free(m):
            named.append((m.start(), _Named("until", _clock(m["t"]))))
    return [n for _, n in sorted(named, key=lambda item: item[0])]


def _blocked(named: _Named) -> Window | None:
    if named.kind == "range":
        return _range(named.clock, named.end, keyword=named.keyword)
    return _from(named.clock) if named.kind == "from" else _until(named.clock)


def _allowed_only(named: _Named) -> list[Window]:
    """The rest of the day around a window the user keeps for work."""
    if named.kind == "from":
        rest = [_until(named.clock)]
    elif named.kind == "until":
        rest = [_from(named.clock)]
    else:
        window = _range(named.clock, named.end, keyword=named.keyword)
        rest = [] if window is None else [(0, window[0]), (window[1], MINUTES_PER_DAY)]
    return [w for w in rest if w is not None and w[0] < w[1]]


def parse_time_range(text: str) -> Window | None:
    """The first window ``text`` names, read as a block, in horizon minutes; None when
    it names none.

    "between 14:00 and 15:00" -> (360, 420); "after 6 PM" -> (600, 1440); "before
    10 AM" -> (0, 120). It does not say whether the words block the window: that is
    ``blocked_windows``. Planning uses this only for the window a rule claims for
    focused work ("Deep work from 9 AM to 10 AM").
    """
    for named in _named(_normalised(text)):
        window = _blocked(named)
        if window is not None:
            return window
    return None


def blocked_windows(text: str) -> list[Window]:
    """The windows a stated rule blocks, in horizon minutes, sorted; [] when its words
    block nothing or name no clock time (see the module docstring for the semantics)."""
    words = _normalised(text)
    if _WORKS_WELL.search(words):
        return []
    named = _named(words)
    negated = bool(_NEGATION.search(words))
    if named and (_RESTRICTION.search(words) or (_JUST.search(words) and not negated)):
        windows = [w for n in named for w in _allowed_only(n)]
    elif named and (negated or _KEEP_FREE.search(words)):
        windows = [w for n in named if (w := _blocked(n)) is not None]
    elif negated or _AVAILABILITY.search(words) or not _ACTIVITY.search(words):
        return []
    elif named:  # a fixed activity occupies its range
        windows = [w for n in named if n.kind == "range" and (w := _blocked(n)) is not None]
    else:
        windows = _activity_hour(words)
    return sorted(set(windows))


def _activity_hour(words: str) -> list[Window]:
    """"Class at 10 AM": the hour from then; [] when no time follows the activity."""
    at = _ACTIVITY_AT.search(words)
    clock = _clock(at["t"]) if at else None
    if clock is None:
        return []
    start = _wall(clock, later=clock.hour < DAY_START_HOUR)
    window = horizon_window(start, start + 60)
    return [window] if window else []


def _normalised(text: str) -> str:
    return (text or "").lower().replace("’", "'")
