"""Sleep windows, precedence, and buffer logic."""

from __future__ import annotations

from typing import Iterable


def merge_time_windows(windows: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Coalesce ``(start, end)`` minute windows into disjoint, ascending windows.

    Blocked time is a *union*: a 09:00-11:00 lecture nested inside a "no work
    before 11am" rule is one blocked stretch, not two things competing for the
    user's single body. Both callers need that union — one puts the windows in a
    CP-SAT ``add_no_overlap`` set (where two overlapping *constant* intervals are
    unsatisfiable on their own), the other sums blocked minutes per day (where
    overlap is double counted).

    Degenerate windows (``end <= start``) block nothing and are dropped. Touching
    windows (``a.end == b.start``) are merged, since the result is contiguous.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(w for w in windows if w[1] > w[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
