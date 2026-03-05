"""Parse deadline_hint from TaskChunks to compute scheduling horizon."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.core.config import MAX_HORIZON_MINUTES

if TYPE_CHECKING:
    from app.api.v1.endpoints.reasoning import ExecutionGraph


def parse_deadline_to_date(hint: str | None, ref: datetime) -> datetime | None:
    """Parse ISO-8601 date string; return datetime at start-of-day or None if unparseable.

    Phase 1: ISO-8601 only (e.g. '2026-03-20', '2026-03-07'). No natural language.
    Phase 2 (optional): Add dateparser for NL strings like "before Friday exam".

    Args:
        hint: Raw deadline_hint from TaskChunk.
        ref: Reference datetime (used for relative semantics in future NL support).

    Returns:
        datetime at midnight for parsed date, or None if unparseable. Past dates are returned
        as-is; caller may filter them.
    """
    if not hint or not isinstance(hint, str) or len(hint) < 10:
        return None
    s = hint.strip()[:10]
    try:
        year, month, day = int(s[:4]), int(s[5:7]), int(s[8:10])
        return datetime(year, month, day)
    except (ValueError, TypeError, IndexError):
        return None


def compute_horizon_from_deadlines(
    graph: "ExecutionGraph",
    plan_start: datetime,
) -> int | None:
    """Compute horizon_minutes from furthest parseable deadline across all TaskChunks.

    Takes the maximum (furthest) parsed deadline date. Ignores past deadlines.
    Returns None if no deadline_hint is parseable.

    Args:
        graph: ExecutionGraph from Socratic decomposition.
        plan_start: Reference datetime (e.g. planning "now").

    Returns:
        Horizon in minutes (capped at MAX_HORIZON_MINUTES), or None if no parseable deadlines.
    """
    plan_date = plan_start.date()
    max_deadline_date = None

    for chunk in graph.decomposition:
        if not chunk.deadline_hint:
            continue
        parsed = parse_deadline_to_date(chunk.deadline_hint, plan_start)
        if parsed is None:
            continue
        # Ignore past deadlines
        if parsed.date() <= plan_date:
            continue
        if max_deadline_date is None or parsed.date() > max_deadline_date:
            max_deadline_date = parsed.date()

    if max_deadline_date is None:
        return None

    delta = max_deadline_date - plan_date
    horizon_minutes = int(delta.days * 1440)
    return min(horizon_minutes, MAX_HORIZON_MINUTES)
