"""Parse deadline_hint from TaskChunks to compute scheduling horizon."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Union

from app.core.config import MAX_HORIZON_MINUTES

if TYPE_CHECKING:
    from app.api.v1.endpoints.reasoning import ExecutionGraph, TaskChunk


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
    graph: Union["ExecutionGraph", None] = None,
    plan_start: datetime | None = None,
    *,
    chunks: list["TaskChunk"] | None = None,
    external_deadline: str | None = None,
    plan_deadlines: list[str] | None = None,
) -> int | None:
    """Compute horizon_minutes from furthest parseable deadline across all sources.

    Collects: (a) chunk.deadline_hint from graph.decomposition or chunks, (b)
    external_deadline (e.g. manual override), (c) plan_deadlines (from
    user_plan_updates). Takes max of all parseable future dates.

    Args:
        graph: Optional ExecutionGraph (used when chunks not provided).
        plan_start: Reference datetime (e.g. planning "now"). Required if chunks provided.
        chunks: Optional list of TaskChunks (takes precedence over graph.decomposition).
        external_deadline: Manual override (ISO-8601).
        plan_deadlines: List of deadline strings from user_plan_updates.

    Returns:
        Horizon in minutes (capped at MAX_HORIZON_MINUTES), or None if no parseable deadlines.
    """
    plan_date = (plan_start or datetime.now()).date()
    max_deadline_date = None

    def _consider(hint: str | None) -> None:
        nonlocal max_deadline_date
        if not hint:
            return
        parsed = parse_deadline_to_date(hint, plan_start or datetime.now())
        if parsed is None:
            return
        if parsed.date() <= plan_date:
            return
        if max_deadline_date is None or parsed.date() > max_deadline_date:
            max_deadline_date = parsed.date()

    if chunks is not None:
        for chunk in chunks:
            _consider(chunk.deadline_hint)
    elif graph is not None:
        for chunk in graph.decomposition:
            _consider(chunk.deadline_hint)

    _consider(external_deadline)

    for d in plan_deadlines or []:
        _consider(d)

    if max_deadline_date is None:
        return None

    delta = max_deadline_date - plan_date
    horizon_minutes = int(delta.days * 1440)
    return min(horizon_minutes, MAX_HORIZON_MINUTES)
