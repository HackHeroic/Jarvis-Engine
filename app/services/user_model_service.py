"""What Jarvis knows about the user, in one read, and the settings the user controls.

``snapshot`` gathers the You page: the profile, the patterns PEARL sees in the outcome
history (each with its evidence and the memory that holds it), the rules the user's
plans respect, the memories in use, the focus profile, the privacy receipt of the
last RECEIPT_WINDOW_DAYS and the completion streak (the coach's count). It runs PEARL's idempotent ``observe`` first, so every
pattern shown has a memory the user can confirm or dismiss. A dismissed or forgotten
pattern is never shown again. A remembered pattern PEARL does not see now (its
evidence aged out) is listed among the memories: it may still shape plans, so it
stays visible and can be confirmed or dismissed.

Evidence reads in the user's terms: "set aside" (skipped and missed both count as set
aside for PEARL) and "done". PEARL words it so; a line that still starts with a raw
event type ("missed", "skipped") is reworded here, so none ever reaches the page.
"""

import asyncio
import functools
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import available_timezones

from pydantic import BaseModel, Field

from app.domain import errors
from app.domain.clock import utc_now
from app.domain.entities import (
    AutonomyMode,
    BehavioralConstraint,
    FocusProfile,
    Memory,
    MemorySource,
    TaskEventType,
    UserProfile,
)
import app.core.config as config
from app.domain.ports import (
    ConstraintRepository,
    EnergyEstimator,
    ProfileRepository,
    TaskEventRepository,
)
from app.services.analytical.mastery_tracker import STREAK_WINDOW_DAYS, streak_as_of
from app.services.memory.memory_service import MemoryService
from app.services.memory.patterns import PatternService, PatternView
from app.services.planning.local_time import user_zone
from app.services.task_service import SLOT_OUTCOMES, as_aware
from app.services.usage_ledger import UsageLedger

# How each outcome reads on the page: never "missed", "skipped" or "failed".
_EVIDENCE_WORDS = {
    TaskEventType.COMPLETED.value: "done",
    TaskEventType.SKIPPED.value: "set aside",
    TaskEventType.MISSED.value: "set aside",
    TaskEventType.DEADLINE_MISSED.value: "set aside",
}
# A pattern shown under "patterns" is not listed again among the memories.
_PATTERN_MEMORY_TYPE = "behavioral_pattern"
# The privacy receipt covers this many days back from the read; the response says so.
RECEIPT_WINDOW_DAYS = 30


@functools.cache
def _known_zones() -> frozenset[str]:
    """The IANA zone names this machine knows, exactly as spelled (read once)."""
    return frozenset(available_timezones())


def _readable(line: str) -> str:
    """An evidence line with its outcome word in the user's terms ("set aside", "done")."""
    word, _, rest = line.partition(" ")
    return f"{_EVIDENCE_WORDS[word]} {rest}" if word in _EVIDENCE_WORDS and rest else line


class PatternShown(BaseModel):
    """A pattern PEARL sees now, as the You page shows it."""

    memory_id: str | None = Field(
        description="Id of the memory that holds it, to confirm or dismiss; null if not remembered."
    )
    pattern_key: str = Field(description="Stable identity of the pattern, e.g. skip_time_window:9.")
    detector: str = Field(description="Which detector found it.")
    description: str = Field(description="What Jarvis noticed, in words.")
    confidence: float = Field(description="How consistent the behaviour is, 0 to 1.")
    occurrences: int = Field(description="How many outcomes it rests on.")
    evidence: list[str] = Field(
        description="Up to five observations behind it, newest first, on the user's clock."
    )
    confirmed: bool = Field(description="Whether the user has confirmed it.")

    @classmethod
    def of(cls, view: PatternView) -> "PatternShown":
        pattern = view.pattern
        return cls(
            memory_id=view.memory_id,
            pattern_key=pattern.pattern_key or pattern.detector,
            detector=pattern.detector,
            description=pattern.description,
            confidence=pattern.confidence,
            occurrences=pattern.occurrences,
            evidence=[_readable(line) for line in pattern.evidence],
            confirmed=view.confirmed,
        )


class RuleShown(BaseModel):
    """A rule the user's plans respect."""

    id: str = Field(
        description="The rule's id (`id`: the item is the rule itself); delete it at "
        "/habits/constraints/{id}."
    )
    text: str = Field(description="The rule as said, or as inferred.")
    provenance: MemorySource = Field(
        description="'said': the user stated it. 'inferred': Jarvis noticed it."
    )
    created_at: datetime | None = Field(
        default=None, description="When it was stored; null for a rule older than the stamp."
    )

    @classmethod
    def of(cls, rule: BehavioralConstraint) -> "RuleShown":
        return cls(
            id=rule.id, text=rule.raw_text, provenance=rule.source, created_at=rule.created_at
        )


class PrivacyReceipt(BaseModel):
    """Where the user's language-model calls ran, since ``since``."""

    since: datetime = Field(description="Start of the window the counts cover; it ends now.")
    truncated: bool = Field(
        description="True if the store stopped at its read bound: the counts may be short."
    )
    on_device_calls: int = Field(description="Calls answered on the device.")
    cloud_calls: int = Field(description="Calls sent to a cloud model.")
    pii_redactions: int = Field(description="Personal details redacted before a cloud send.")
    input_tokens: int = Field(description="Prompt tokens, all calls in the window.")
    output_tokens: int = Field(description="Reply tokens, all calls in the window.")


class YouView(BaseModel):
    """Everything the You page shows, read at one instant."""

    as_of: datetime = Field(description="The instant of the read; freshness is measured at it.")
    profile: UserProfile = Field(description="The user's settings.")
    patterns: list[PatternShown] = Field(description="Patterns seen now; dismissed ones never.")
    rules: list[RuleShown] = Field(description="Rules that apply now.")
    memories: list[Memory] = Field(
        description=(
            "Memories in use at as_of, oldest first; a pattern's memory only when the pattern"
            " is not among `patterns` (no recent evidence), so all that shapes plans is visible."
        )
    )
    focus: FocusProfile = Field(description="Focus by local hour, learned or the prior.")
    receipt: PrivacyReceipt = Field(description="Where the user's model calls ran.")
    streak_days: int = Field(
        ge=0,
        description=(
            "Consecutive local days with a completed task, ending today, or yesterday while"
            " nothing is done yet today."
        ),
    )


class UserModelService:
    """Reads what Jarvis knows about the user, and changes the settings they control."""

    def __init__(
        self,
        profiles: ProfileRepository,
        memory: MemoryService,
        patterns: PatternService,
        constraints: ConstraintRepository,
        energy: EnergyEstimator,
        usage: UsageLedger,
        task_events: TaskEventRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._task_events = task_events
        self._profiles = profiles
        self._memory = memory
        self._patterns = patterns
        self._constraints = constraints
        self._energy = energy
        self._usage = usage
        self._clock = clock

    async def snapshot(self, user_id: str) -> YouView:
        """The You page for ``user_id``: PEARL looks first, then every section is read.

        The profile and the outcome history are read once. PEARL's look
        (``PatternService.look``: idempotent, nothing new is nothing written) and the
        focus profile share its last FOCUS_WINDOW_DAYS; the streak counts its last
        STREAK_WINDOW_DAYS (``mastery_tracker.streak_as_of``, the coach's count). The
        read is newest first, so under a row cap the focus window's events are exactly
        those a read of that window alone would return. The other sections are
        independent reads, made concurrently with the look. Any store failure
        propagates (PersistenceError): an outage is never an empty page.
        """
        now = self._clock()
        history_days = max(config.FOCUS_WINDOW_DAYS, STREAK_WINDOW_DAYS)
        profile, history = await asyncio.gather(
            self._profiles.get_or_create(user_id),
            self._task_events.list_for_user(
                user_id, since=now - timedelta(days=history_days), types=set(SLOT_OUTCOMES)
            ),
        )
        focus_since = now - timedelta(days=config.FOCUS_WINDOW_DAYS)
        events = [e for e in history if as_aware(e.occurred_at) >= focus_since]
        zone = user_zone(profile)
        since = now - timedelta(days=RECEIPT_WINDOW_DAYS)
        look, rules, active, focus, receipt = await asyncio.gather(
            self._patterns.look(user_id, events=events, zone=zone),
            self._constraints.list_active(user_id),
            self._memory.list_active(user_id, now=now),
            self._energy.estimate(user_id, zone, now=now, events=events),
            self._usage.receipt(user_id, since=since),
        )
        patterns = [PatternShown.of(view) for view in look.views]
        shown = {pattern.pattern_key for pattern in patterns}
        return YouView(
            as_of=now,
            profile=profile,
            patterns=patterns,
            rules=[RuleShown.of(rule) for rule in rules],
            memories=[
                memory
                for memory in active
                if not (memory.memory_type == _PATTERN_MEMORY_TYPE and memory.pattern_key in shown)
            ],
            focus=focus,
            receipt=PrivacyReceipt(since=since, **receipt),
            streak_days=streak_as_of(history, zone, now),
        )

    async def update_settings(
        self,
        user_id: str,
        *,
        autonomy_mode: AutonomyMode | None = None,
        wake_time: time | None = None,
        timezone: str | None = None,
    ) -> UserProfile:
        """Change the given settings and return the profile; the others stay as they are.

        An unknown IANA zone is a ValidationError and nothing is changed.
        """
        fields: dict = {}
        if autonomy_mode is not None:
            fields["autonomy_mode"] = autonomy_mode
        if wake_time is not None:
            fields["wake_time"] = wake_time
        if timezone is not None:
            if timezone not in _known_zones():
                raise errors.ValidationError(
                    "That timezone isn't one Jarvis knows. Use a name like Asia/Kolkata.",
                    details={"fields": ["timezone"]},
                )
            fields["timezone"] = timezone
        if not fields:
            return await self._profiles.get_or_create(user_id)
        return await self._profiles.update(user_id, **fields)
