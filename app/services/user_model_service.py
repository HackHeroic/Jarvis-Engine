"""What Jarvis knows about the user, in one read, and the settings the user controls.

``snapshot`` gathers the You page: the profile, the patterns PEARL sees in the outcome
history (each with its evidence and the memory that holds it), the rules the user's
plans respect, the memories in use, the focus profile and the privacy receipt. It runs
PEARL's idempotent ``observe`` first, so every pattern shown has a memory the user can
confirm or dismiss. A dismissed or forgotten pattern is never shown again.

Evidence is reworded for the page: the history records "skipped" and "missed", the
user reads "set aside" (both count as set aside for PEARL) and "done".
"""

import functools
from collections.abc import Callable
from datetime import datetime, time
from typing import Literal
from zoneinfo import available_timezones

from pydantic import BaseModel, Field

from app.domain import errors
from app.domain.entities import (
    AutonomyMode,
    BehavioralConstraint,
    FocusProfile,
    Memory,
    TaskEventType,
    UserProfile,
)
from app.domain.ports import ConstraintRepository, EnergyEstimator, ProfileRepository
from app.services.memory.memory_service import MemoryService
from app.services.memory.patterns import PatternService, PatternView
from app.services.planning.local_time import user_zone
from app.services.task_service import utc_now
from app.services.usage_ledger import UsageLedger

# How each outcome reads on the page: never "missed", "skipped" or "failed".
_EVIDENCE_WORDS = {
    TaskEventType.COMPLETED.value: "done",
    TaskEventType.SKIPPED.value: "set aside",
    TaskEventType.MISSED.value: "set aside",
    TaskEventType.DEADLINE_MISSED.value: "set aside",
}
# Patterns have their own section; their memories are not listed twice.
_PATTERN_MEMORY_TYPE = "behavioral_pattern"


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

    id: str = Field(description="The rule's id.")
    text: str = Field(description="The rule as said, or as inferred.")
    provenance: Literal["said", "inferred"] = Field(
        description="'said': the user stated it. 'inferred': Jarvis noticed it."
    )
    created_at: datetime | None = Field(
        default=None, description="When it was stored; null when the store does not say."
    )

    @classmethod
    def of(cls, rule: BehavioralConstraint) -> "RuleShown":
        return cls(id=rule.id, text=rule.raw_text, provenance=rule.source)


class PrivacyReceipt(BaseModel):
    """Where the user's language-model calls ran, all time."""

    on_device_calls: int = Field(description="Calls answered on the device.")
    cloud_calls: int = Field(description="Calls sent to a cloud model.")
    pii_redactions: int = Field(description="Personal details redacted before a cloud send.")
    input_tokens: int = Field(description="Prompt tokens, all calls.")
    output_tokens: int = Field(description="Reply tokens, all calls.")


class YouView(BaseModel):
    """Everything the You page shows, read at one instant."""

    as_of: datetime = Field(description="The instant of the read; freshness is measured at it.")
    profile: UserProfile = Field(description="The user's settings.")
    patterns: list[PatternShown] = Field(description="Patterns seen now; dismissed ones never.")
    rules: list[RuleShown] = Field(description="Rules that apply now.")
    memories: list[Memory] = Field(
        description="Memories in use at as_of, oldest first; pattern memories excluded."
    )
    focus: FocusProfile = Field(description="Focus by local hour, learned or the prior.")
    receipt: PrivacyReceipt = Field(description="Where the user's model calls ran.")


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
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._profiles = profiles
        self._memory = memory
        self._patterns = patterns
        self._constraints = constraints
        self._energy = energy
        self._usage = usage
        self._clock = clock

    async def snapshot(self, user_id: str) -> YouView:
        """The You page for ``user_id``: PEARL looks first, then every section is read.

        Any store failure propagates (PersistenceError): an outage is never an empty page.
        """
        await self._patterns.observe(user_id)  # idempotent: nothing new, nothing written
        now = self._clock()
        profile = await self._profiles.get_or_create(user_id)
        patterns = [PatternShown.of(view) for view in await self._patterns.current(user_id)]
        rules = [RuleShown.of(rule) for rule in await self._constraints.list_active(user_id)]
        memories = [
            memory
            for memory in await self._memory.list_active(user_id, now=now)
            if memory.memory_type != _PATTERN_MEMORY_TYPE
        ]
        focus = await self._energy.estimate(user_id, user_zone(profile), now=now)
        receipt = PrivacyReceipt(**await self._usage.receipt(user_id))
        return YouView(
            as_of=now,
            profile=profile,
            patterns=patterns,
            rules=rules,
            memories=memories,
            focus=focus,
            receipt=receipt,
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
