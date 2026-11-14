"""Parse a deadline hint (a TaskChunk's, or one a document names) into a date."""

from __future__ import annotations

from datetime import datetime


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
