"""The user's clock: the zone their day runs in, and where a plan's day starts in it.

The scheduler counts minutes from ``horizon_start``; this module decides which
instant that is. Minute 0 is ``DAY_START_HOUR`` in the user's zone on their
logical day — never in UTC — so every wall-clock rule expressed as minutes from
08:00 (habit slots, the constraint bridge, the midnight-08:00 sleep block) is in
the user's local time by construction.

Zones with daylight saving have 23- and 25-hour days, which "minute 1440 is
08:00 tomorrow" ignores: across a transition the later days of a plan sit an
hour off. India has no DST.

The caller passes "now": the scheduler takes it from its own clock.
"""

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import app.core.config as config
from app.domain.entities import UserProfile

logger = logging.getLogger(__name__)


def _zone_named(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r", name)
        return None


def user_zone(profile: UserProfile | None = None) -> ZoneInfo:
    """The zone the user's day runs in.

    ``profile.timezone`` when there is a profile naming a real zone, else
    ``DEFAULT_TIMEZONE`` (env ``JARVIS_DEFAULT_TIMEZONE``, default Asia/Kolkata);
    UTC only if that too is misconfigured.
    """
    return (
        _zone_named(profile.timezone if profile is not None else None)
        or _zone_named(config.DEFAULT_TIMEZONE)
        or ZoneInfo("UTC")
    )


def _local(moment: datetime, zone: ZoneInfo) -> datetime:
    """``moment`` on the user's clock; a naive datetime is taken as their wall time."""
    return moment.astimezone(zone) if moment.tzinfo is not None else moment.replace(tzinfo=zone)


def day_start_on(moment: datetime, zone: ZoneInfo, day_start_hour: int | None = None) -> datetime:
    """``day_start_hour`` (default ``DAY_START_HOUR``) local on ``moment``'s local date."""
    hour = config.DAY_START_HOUR if day_start_hour is None else day_start_hour
    return datetime.combine(_local(moment, zone).date(), time(hour, 0), tzinfo=zone)


def logical_day_start(
    now: datetime, zone: ZoneInfo, day_start_hour: int | None = None
) -> datetime:
    """Where today's plan starts: ``day_start_hour`` local on the user's logical day.

    Before that hour the logical day is still yesterday's (the late-night rule), so
    a plan made at 01:30 runs on yesterday's day, whose midnight-08:00 night block
    covers the rest of the night, instead of starting a fresh day at 3 AM.
    """
    hour = config.DAY_START_HOUR if day_start_hour is None else day_start_hour
    local_now = _local(now, zone)
    day = local_now.date()
    if local_now.hour < hour:
        day -= timedelta(days=1)
    return datetime.combine(day, time(hour, 0), tzinfo=zone)


def local_today(zone: ZoneInfo | None = None) -> date:
    """Today's date on the user's clock."""
    return datetime.now(zone or user_zone()).date()


def end_of_local_day(day: date, zone: ZoneInfo) -> datetime:
    """The last second of ``day`` on the user's clock: when a date-only deadline passes."""
    return datetime.combine(day, time(23, 59, 59), tzinfo=zone)
