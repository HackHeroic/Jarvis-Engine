"""Rule check-ins: now and then, Jarvis asks whether a rule it learned still holds.

Every rule the user has (a behavioural constraint) gets one SM-2 tracker
(``HabitTracker.constraint_id``). A check-in is that tracker's review: "Still how you
work?", rated 0 to 5. A rule that keeps holding is asked about less and less often
(one day, six days, then the interval times its easiness); a rating under 3 brings it
back the next day. It is infer → confirm → adapt, applied to the rules themselves.

A tracker counts from the moment its rule was learned (held as its ``last_done_at``,
with no rating yet), so the first check-in is due the day after: never the moment a
rule is said, and already due for a rule learned a week before it had a tracker.

Trackers start when a rule is stored (``start``, best effort: a rule is never lost for
its tracker) and, for any rule still without one, when the list is read
(``list_for_user``). They go just before their rule is deleted (``stop``): the
database's key would only clear the tracker's ``constraint_id``. A tracker whose rule
is gone, or no longer applies, is never listed.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from app.domain import errors
from app.domain.clock import utc_now
from app.domain.entities import BehavioralConstraint, HabitTracker
from app.domain.ports import ConstraintRepository, HabitTrackerRepository, ProfileRepository
from app.schemas.habits import CheckInReviewed, RuleCheckIn, RuleCheckInList
from app.services.analytical.sm2_engine import next_due_on, record_completion
from app.services.planning.local_time import local_date, user_zone

logger = logging.getLogger(__name__)

_NEVER = datetime.min.replace(tzinfo=timezone.utc)


class _UserLock:
    """A user's lock and how many calls hold or await it."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class RuleCheckIns:
    """Starts, lists, reviews and ends the check-ins on the user's rules."""

    def __init__(
        self,
        trackers: HabitTrackerRepository,
        rules: ConstraintRepository,
        profiles: ProfileRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._trackers = trackers
        self._rules = rules
        self._profiles = profiles
        self._clock = clock
        self._locks: dict[str, _UserLock] = {}

    async def start(self, user_id: str, rule: BehavioralConstraint) -> None:
        """Start the check-ins on a rule just stored: the first is due tomorrow.

        Best effort: a store fault is logged, never raised, so storing the rule never
        fails for it (the next read of the list starts the tracker instead).
        """
        try:
            async with self._held(user_id):
                await self._trackers.add_for_constraint(
                    user_id, rule.id, started_at=self._clock()
                )
        except errors.JarvisError as exc:
            logger.warning("A rule's check-ins were not started (%s); the next read will", exc.code)

    async def stop(self, user_id: str, rule_id: str) -> None:
        """End the check-ins on a rule about to be deleted (call it before the delete).

        A store fault is raised: the rule is then not deleted either, and deleting it
        again finishes both.
        """
        async with self._held(user_id):
            await self._trackers.delete_for_constraint(user_id, rule_id)

    async def list_for_user(self, user_id: str) -> RuleCheckInList:
        """Every rule the user has now, with when its check-in is due, on their clock.

        A rule with no tracker yet gets one here, counting from when it was learned.
        One read of the rules and one of the trackers otherwise; an outage raises
        PersistenceError (503), never an empty list.
        """
        now = self._clock()
        zone = user_zone(await self._profiles.get_or_create(user_id))
        today = local_date(now, zone)
        rules = await self._rules.list_active(user_id)
        async with self._held(user_id):
            tracked = _one_per_rule(await self._trackers.list_for_user(user_id))
            for rule in rules:
                if rule.id not in tracked:
                    tracked[rule.id] = await self._trackers.add_for_constraint(
                        user_id, rule.id, started_at=rule.created_at or now
                    )
        check_ins = sorted(
            (
                _check_in(rule, tracked[rule.id], next_due_on(tracked[rule.id], zone, today=today))
                for rule in rules
            ),
            key=lambda check_in: check_in.next_due_on,  # stable: rules stay oldest first
        )
        return RuleCheckInList(
            timezone=zone.key,
            today=today,
            due_trackers=[c for c in check_ins if c.next_due_on <= today],
            upcoming_trackers=[c for c in check_ins if c.next_due_on > today],
        )

    async def review(self, user_id: str, tracker_id: str, quality: int) -> CheckInReviewed:
        """Record the user's answer to a check-in (0-5) and say when the rule comes back.

        Another user's tracker, or an unknown id, is NotFoundError.
        """
        zone = user_zone(await self._profiles.get_or_create(user_id))
        now = self._clock()
        stored = await record_completion(self._trackers, user_id, tracker_id, quality, now=now)
        interval = stored.next_interval_days or 1
        return CheckInReviewed(
            next_review_date=(now + timedelta(days=interval)).astimezone(timezone.utc),
            next_interval_days=interval,
            next_due_on=next_due_on(stored, zone, today=local_date(now, zone)),
        )

    @asynccontextmanager
    async def _held(self, user_id: str) -> AsyncIterator[None]:
        """One start, stop or list of a user's trackers at a time, in this process: two
        page loads at once start one tracker per rule, not two. The lock is dropped as
        soon as no call holds or awaits it."""
        entry = self._locks.get(user_id)
        if entry is None:
            entry = self._locks[user_id] = _UserLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._locks[user_id]


def _one_per_rule(trackers: Iterable[HabitTracker]) -> dict[str, HabitTracker]:
    """Each rule's tracker, by rule id. Two processes racing can leave two for one rule:
    the one answered last is the rule's (of equal stamps, the newer, listed first)."""
    chosen: dict[str, HabitTracker] = {}
    for tracker in trackers:
        if tracker.constraint_id is None:  # its rule was deleted under it
            continue
        held = chosen.get(tracker.constraint_id)
        if held is None or (tracker.last_done_at or _NEVER) > (held.last_done_at or _NEVER):
            chosen[tracker.constraint_id] = tracker
    return chosen


def _check_in(rule: BehavioralConstraint, tracker: HabitTracker, due_on: date) -> RuleCheckIn:
    reviewed = tracker.quality_last is not None  # before that, last_done_at is the start
    return RuleCheckIn(
        id=tracker.id,
        constraint_id=rule.id,
        rule_text=rule.raw_text,
        source=rule.source,
        learned_at=rule.created_at,
        last_reviewed_at=tracker.last_done_at if reviewed else None,
        next_due_on=due_on,
        interval_days=tracker.next_interval_days or 1,
    )
