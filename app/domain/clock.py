"""The one clock: the current instant, in UTC.

Services take it as their default ``clock``; a test freezes this module's ``datetime``
to fix "now" everywhere that reads it.
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """The current instant, in UTC."""
    return datetime.now(timezone.utc)
