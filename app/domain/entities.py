"""Domain entities: the things Jarvis reasons about, free of any framework or storage detail."""

import math
from datetime import datetime, time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.errors import InvalidStateTransition


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    MISSED = "missed"


class DraftStatus(str, Enum):
    PENDING = "pending"
    MODIFIED = "modified"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class TaskEventType(str, Enum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    MISSED = "missed"
    RESCHEDULED = "rescheduled"
    DEADLINE_MISSED = "deadline_missed"


class AutonomyMode(str, Enum):
    SUGGEST_ONLY = "suggest_only"
    ASK_FIRST = "ask_first"
    AUTO_LOW_RISK = "auto_low_risk"


class SuggestionStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    EXPIRED = "expired"
    AUTO_APPLIED = "auto_applied"


class ConsentReasonKind(str, Enum):
    """Which link of the consent chain decided, so a reason can be shown by type.

    Defined here rather than in ``app.domain.anticipation``, which imports this module.
    """

    RULE_MATCH = "rule_match"
    MODE_DECISION = "mode_decision"
    RISK_CHECK = "risk_check"
    LEARNED_PREFERENCE = "learned_preference"


class LLMLocation(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    DISMISSED = "dismissed"
    FORGOTTEN = "forgotten"
    FADED = "faded"


class MemorySource(str, Enum):
    """Where a memory came from: the user's own words, or Jarvis's reading of behaviour."""

    SAID = "said"
    INFERRED = "inferred"


MemoryType = Literal[
    "fact", "preference", "behavioral_pattern", "temporal_event", "goal", "feedback", "constraint"
]

# A memory in one of these states never comes back on its own: the same fact said or
# observed again is recognised by its pattern key and not stored again.
SUPPRESSING_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN}
)

# At stability 1 a memory's strength falls by a factor of e every week.
BASE_HALFLIFE_HOURS = 168


# The states a draft is still under review in. The only definition: the persistence
# mapping and the legacy DraftStore both import it.
OPEN_DRAFT_STATUSES: tuple[DraftStatus, ...] = (DraftStatus.PENDING, DraftStatus.MODIFIED)

_OPEN_DRAFT_TARGETS = frozenset(
    {
        DraftStatus.MODIFIED,
        DraftStatus.ACCEPTED,
        DraftStatus.REJECTED,
        DraftStatus.EXPIRED,
        DraftStatus.SUPERSEDED,
    }
)

# Legal draft lifecycle moves. PENDING and MODIFIED are the open states; every other
# state is terminal, so a second accept (or anything after one) is an illegal move.
DRAFT_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
    DraftStatus.PENDING: _OPEN_DRAFT_TARGETS,
    DraftStatus.MODIFIED: _OPEN_DRAFT_TARGETS,
    DraftStatus.ACCEPTED: frozenset(),
    DraftStatus.REJECTED: frozenset(),
    DraftStatus.EXPIRED: frozenset(),
    DraftStatus.SUPERSEDED: frozenset(),
}


class UserProfile(BaseModel):
    """Per-user settings that shape how Jarvis plans and how much it may act on its own."""

    user_id: str = Field(description="Owner of this profile.")
    display_name: str | None = Field(default=None, description="Name Jarvis addresses the user by.")
    timezone: str = Field(default="Asia/Kolkata", description="IANA timezone the user lives in.")
    wake_time: time = Field(default=time(7, 0), description="Usual local wake-up time.")
    autonomy_mode: AutonomyMode = Field(
        default=AutonomyMode.ASK_FIRST,
        description=(
            "How far Jarvis may act without asking: suggest only, ask first,"
            " or auto-apply low-risk changes."
        ),
    )


class Goal(BaseModel):
    """Something the user is working towards; tasks are grouped under it."""

    id: str = Field(description="Goal identifier, unique per user.")
    user_id: str = Field(description="Owner of the goal.")
    title: str = Field(description="What the user wants to achieve.")
    deadline: datetime | None = Field(
        default=None, description="When the goal is due, if it has a deadline."
    )
    status: str = Field(default="active", description="Lifecycle state of the goal.")


class Task(BaseModel):
    """One schedulable unit of work the user has committed to."""

    id: str | None = Field(default=None, description="Storage row id; None until persisted.")
    user_id: str = Field(description="Owner of the task.")
    task_id: str = Field(
        description=(
            "Task identifier, unique per user; new plans write {goal_id}_{run}_{task_id}, "
            "one run token per planning run. Read the goal from goal_id, never from this."
        )
    )
    goal_id: str | None = Field(default=None, description="Goal this task belongs to.")
    plan_id: str | None = Field(
        default=None, description="Plan (accepted draft) that wrote this task."
    )
    title: str = Field(description="What to do.")
    duration_minutes: int = Field(gt=0, description="Planned length in minutes; always positive.")
    difficulty_weight: float = Field(
        ge=0.0, le=1.0, description="Cognitive difficulty from 0 (trivial) to 1 (hardest)."
    )
    status: TaskStatus = Field(description="Where the task is in its lifecycle.")
    dependencies: list[str] = Field(
        default_factory=list, description="task_ids that must finish before this one starts."
    )
    scheduled_start: datetime | None = Field(
        default=None, description="Scheduled wall-clock start, if scheduled."
    )
    scheduled_end: datetime | None = Field(
        default=None, description="Scheduled wall-clock end, if scheduled."
    )
    deadline_hint: str | None = Field(
        default=None, description="Free-text deadline the task was planned against."
    )


class TaskEvent(BaseModel):
    """An append-only record of something that happened to a task."""

    id: str | None = Field(default=None, description="Storage row id; None until persisted.")
    user_id: str = Field(description="Owner of the task.")
    task_id: str = Field(description="Task the event is about.")
    event_type: TaskEventType = Field(description="What happened.")
    occurred_at: datetime = Field(description="When it happened.")
    scheduled_start: datetime | None = Field(
        default=None, description="Task's scheduled start at the time of the event."
    )
    scheduled_end: datetime | None = Field(
        default=None, description="Task's scheduled end at the time of the event."
    )
    metadata: dict = Field(
        default_factory=dict, description="Event-specific detail (e.g. skip reason, new slot)."
    )


class DraftSchedule(BaseModel):
    """A proposed schedule under review; nothing reaches the user's tasks until it is accepted."""

    id: str = Field(description="Draft identifier.")
    user_id: str = Field(description="Owner of the draft.")
    goal_id: str | None = Field(default=None, description="Goal the draft plans for, if any.")
    tasks: list[dict] = Field(
        default_factory=list, description="Proposed task chunks, as serialised by the planner."
    )
    schedule: dict | None = Field(
        default=None, description="Solver output mapping task_id to its slot, if solved."
    )
    horizon_start: str | None = Field(
        default=None, description="ISO start of the planning horizon slot offsets count from."
    )
    status: DraftStatus = Field(description="Where the draft is in its review lifecycle.")
    expires_at: datetime | None = Field(
        default=None, description="After this instant the draft can no longer be accepted."
    )
    rejection_reason: str | None = Field(
        default=None, description="Why the user rejected the draft, kept for learning."
    )

    def transition(self, to: DraftStatus) -> "DraftSchedule":
        """Return a deep copy moved to ``to``; raise InvalidStateTransition if the move is illegal.

        ``to`` is coerced first: ``model_copy`` does not validate, so a raw string
        would be stored as is and ``status`` would stop being a ``DraftStatus``. A
        string that is no status raises ``ValueError``.
        """
        to = DraftStatus(to)
        if to not in DRAFT_TRANSITIONS[self.status]:
            raise InvalidStateTransition(
                f"This draft is {self.status.value} and cannot become {to.value}.",
                details={"draft_id": self.id, "from": self.status.value, "to": to.value},
            )
        return self.model_copy(update={"status": to}, deep=True)


class BehavioralConstraint(BaseModel):
    """A habit or rule about the user's time that planning must respect."""

    id: str = Field(description="Constraint identifier.")
    user_id: str = Field(description="Owner of the constraint.")
    raw_text: str = Field(description="The constraint as the user said it, or as it was inferred.")
    constraint_type: str = Field(
        description="Kind of constraint (e.g. sleep, meeting, focus window)."
    )
    recurrence: str | None = Field(default=None, description="How often it repeats, if it repeats.")
    structured_semantics: dict | None = Field(
        default=None, description="Machine-readable form the scheduler consumes."
    )
    source: Literal["said", "inferred"] = Field(
        default="said",
        description="Whether the user stated it or Jarvis inferred it from behaviour.",
    )
    created_at: datetime | None = Field(
        default=None, description="When it was stored; set by the store, None until then."
    )


class Memory(BaseModel):
    """Something Jarvis knows about the user, with a lifecycle and a strength that decays."""

    id: str | None = Field(default=None, description="Storage row id; None until persisted.")
    user_id: str = Field(description="Owner of the memory.")
    memory_type: MemoryType = Field(description="What kind of thing is remembered.")
    content: str = Field(
        max_length=2000,
        description="The memory in words; empty only once the user has asked Jarvis to forget it.",
    )
    status: MemoryStatus = Field(
        default=MemoryStatus.ACTIVE,
        description=(
            "Lifecycle state: active, dismissed (hidden), forgotten (text erased) or faded."
        ),
    )
    provenance: MemorySource = Field(
        description="Whether the user said it or Jarvis inferred it from what happened."
    )
    origin: str = Field(
        default="conversation",
        description="The channel it arrived by (e.g. conversation, behavior, ingestion).",
    )
    pattern_key: str | None = Field(
        default=None,
        description="Stable identity of the fact, unique per user, so a repeat is recognised.",
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="How sure Jarvis is that it holds, 0 to 1."
    )
    strength: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Strength as of strength_as_of, 0 to 1."
    )
    stability: float = Field(
        default=1.0,
        ge=0.01,
        le=20.0,
        description="How slowly strength decays: 1 loses a factor of e per week, 20 per 20 weeks.",
    )
    strength_as_of: datetime = Field(description="The instant `strength` was measured at.")
    observation_count: int = Field(
        default=1, ge=1, description="How many times it has been said or observed."
    )
    source_session_id: str | None = Field(
        default=None, description="Chat session it was learned in, if any."
    )
    source_message_id: str | None = Field(
        default=None, description="Chat message it was learned from, if any."
    )
    created_at: datetime = Field(description="When it was first stored.")
    last_confirmed_at: datetime | None = Field(
        default=None, description="When it was last said, observed or confirmed again."
    )
    superseded_by: str | None = Field(
        default=None, description="Id of the memory that replaced this one, if any."
    )
    expires_at: datetime | None = Field(
        default=None, description="After this instant it no longer applies (temporal events)."
    )
    applied_as: str | None = Field(
        default=None,
        description="How planning applies it, if it does (e.g. hard_constraint, soft_preference).",
    )

    @model_validator(mode="after")
    def _blank_only_when_forgotten(self) -> "Memory":
        if not self.content.strip() and self.status is not MemoryStatus.FORGOTTEN:
            raise ValueError("only a forgotten memory may have empty content")
        return self

    def strength_at(self, now: datetime) -> float:
        """Strength at ``now``: it falls by a factor of e every ``stability`` weeks.

        An instant before ``strength_as_of`` reads as ``strength_as_of``: strength
        never grows on its own.
        """
        hours = max(0.0, (now - self.strength_as_of).total_seconds() / 3600)
        return self.strength * math.exp(-hours / (self.stability * BASE_HALFLIFE_HOURS))


class Pattern(BaseModel):
    """A recurring behaviour a detector found in the user's history."""

    detector: str = Field(description="Name of the detector that found the pattern.")
    description: str = Field(description="Human-readable statement of the pattern.")
    occurrences: int = Field(ge=1, description="How many times the pattern was observed.")
    confidence: float = Field(ge=0.0, le=1.0, description="Detector confidence in the pattern, 0 to 1.")
    evidence: list[str] = Field(
        default_factory=list, description="References to the observations behind it."
    )
    pattern_key: str | None = Field(
        default=None,
        description=(
            "Stable identity of the pattern (e.g. skip_time_window:9), so the same habit"
            " found again is recognised; the remembered pattern carries the same key."
        ),
    )
    hour: int | None = Field(
        default=None, ge=0, le=23, description="Local hour of the day it is about, if any."
    )
    last_observed_at: datetime | None = Field(
        default=None,
        description=(
            "When the newest observation behind it was recorded; a remembered pattern is"
            " strengthened only by an observation newer than the memory."
        ),
    )


HOURS_PER_DAY = 24


class HourFocus(BaseModel):
    """How well the user gets things done in one local hour of their day."""

    hour: int = Field(ge=0, le=23, description="Local hour of the day, 0 to 23.")
    score: float = Field(
        ge=0.0, le=1.0, description="Focus at this hour, from 0 (none) to 1 (peak)."
    )
    done: int = Field(ge=0, description="Slots at this hour that were completed.")
    not_done: int = Field(
        ge=0, description="Slots at this hour that were set aside or didn't happen."
    )


class FocusProfile(BaseModel):
    """The user's focus across the 24 hours of their day, learned or assumed."""

    source: Literal["prior", "learned"] = Field(
        description=(
            "'learned' once there are enough outcomes to trust; 'prior' is the default"
            " table, which never steers the solver."
        )
    )
    observations: int = Field(ge=0, description="Outcomes with a slot the profile was built from.")
    hours: list[HourFocus] = Field(
        min_length=HOURS_PER_DAY,
        max_length=HOURS_PER_DAY,
        description="One entry per local hour; index == hour.",
    )

    @field_validator("hours")
    @classmethod
    def _index_is_hour(cls, hours: list[HourFocus]) -> list[HourFocus]:
        if any(h.hour != i for i, h in enumerate(hours)):
            raise ValueError("hours must run 0 to 23 in order, one entry per hour")
        return hours

    def scores(self) -> list[float]:
        """The 24 focus scores; index == local hour."""
        return [h.score for h in self.hours]


class ConsentDecision(BaseModel):
    """What the consent chain decided for one action, and why, in words the user can read.

    Defined here, beside ``Suggestion`` which stores it, so the repository port can
    take one; ``app.domain.anticipation`` re-exports it.
    """

    behavior: Literal["allow", "ask", "deny"] = Field(
        description="Run it without asking, ask the user first, or drop it."
    )
    kind: ConsentReasonKind = Field(description="Which consent rule decided.")
    message: str = Field(description="The reason, as shown to the user.")


class Suggestion(BaseModel):
    """A proactive proposal Jarvis makes to the user, subject to consent."""

    id: str | None = Field(default=None, description="Storage row id; None until persisted.")
    user_id: str = Field(description="User the suggestion is for.")
    trigger: str = Field(description="Name of the trigger that produced the suggestion.")
    kind: str = Field(description="Kind of suggestion (e.g. reschedule, break).")
    title: str = Field(description="Short headline shown to the user.")
    body: str = Field(description="Full suggestion text shown to the user.")
    action: dict = Field(
        default_factory=dict,
        description="Machine-readable action applied if the suggestion is taken.",
    )
    status: SuggestionStatus = Field(description="Where the suggestion is in its lifecycle.")
    consent_behavior: Literal["allow", "ask", "deny"] = Field(
        description="Consent decision: apply without asking, ask first, or never apply."
    )
    consent_reason: str = Field(description="Why that consent decision was made.")
    consent_kind: ConsentReasonKind | None = Field(
        default=None,
        description="Which consent rule made the decision; None on suggestions stored before it.",
    )
    created_at: datetime = Field(description="When the suggestion was made.")
    expires_at: datetime | None = Field(
        default=None, description="After this instant the suggestion is stale."
    )


class ConversationSession(BaseModel):
    """One conversation with Jarvis, and the summary of its turns that are no longer shown."""

    id: str = Field(description="Session id (a UUID string).")
    user_id: str = Field(description="Owner of the conversation.")
    title: str | None = Field(
        default=None, description="Short title taken from the first message; None until then."
    )
    created_at: datetime = Field(description="When the conversation started.")
    updated_at: datetime = Field(
        description="Its last activity: when a message was last added. Writing the summary "
        "is not activity and leaves it as it was."
    )
    summary: str | None = Field(
        default=None,
        max_length=4000,
        description="Running summary of every message up to summary_through; None until the "
        "first one is written.",
    )
    summary_through: datetime | None = Field(
        default=None,
        description="created_at of the last message the summary covers; the history window "
        "starts after it.",
    )
    summary_updated_at: datetime | None = Field(
        default=None, description="When the summary was last written."
    )


class ChatMessage(BaseModel):
    """One message in a conversation, as stored."""

    id: str = Field(description="Message id (a UUID string).")
    session_id: str = Field(description="Conversation the message belongs to.")
    user_id: str = Field(description="Owner of the conversation.")
    role: Literal["user", "assistant", "system"] = Field(description="Who wrote it.")
    content: str = Field(description="The message text.")
    created_at: datetime = Field(description="When it was written.")


class LLMUsageEvent(BaseModel):
    """One completed language-model call, recorded for cost and privacy accounting."""

    user_id: str | None = Field(default=None, description="User the call was made for, if any.")
    session_id: str | None = Field(
        default=None, description="Chat session the call belonged to, if any."
    )
    task: str = Field(description="Routing task name (e.g. socratic_chunker).")
    role: str = Field(description="Model role the task maps to (primary, fast, cloud).")
    provider: str = Field(description="Provider that answered (e.g. local, gemini).")
    model: str = Field(description="Model identifier that answered.")
    location: LLMLocation = Field(description="Whether the call ran on-device or in the cloud.")
    input_tokens: int = Field(ge=0, description="Prompt tokens consumed.")
    output_tokens: int = Field(ge=0, description="Completion tokens produced.")
    cache_read_tokens: int = Field(
        default=0, ge=0, description="Prompt tokens served from the provider's cache."
    )
    latency_ms: int = Field(ge=0, description="Wall-clock duration of the call in milliseconds.")
    pii_redactions: int = Field(
        default=0, description="How many PII spans were redacted before a cloud send."
    )
    created_at: datetime = Field(description="When the call completed.")
