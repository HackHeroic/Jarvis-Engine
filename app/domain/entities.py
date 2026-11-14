"""Domain entities: the things Jarvis reasons about, free of any framework or storage detail."""

from datetime import datetime, time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

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


class LLMLocation(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


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


class Pattern(BaseModel):
    """A recurring behaviour a detector found in the user's history."""

    detector: str = Field(description="Name of the detector that found the pattern.")
    description: str = Field(description="Human-readable statement of the pattern.")
    occurrences: int = Field(description="How many times the pattern was observed.")
    confidence: float = Field(description="Detector confidence in the pattern.")
    evidence: list[str] = Field(
        default_factory=list, description="References to the observations behind it."
    )


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
    created_at: datetime = Field(description="When the suggestion was made.")
    expires_at: datetime | None = Field(
        default=None, description="After this instant the suggestion is stale."
    )


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
    input_tokens: int = Field(description="Prompt tokens consumed.")
    output_tokens: int = Field(description="Completion tokens produced.")
    cache_read_tokens: int = Field(
        default=0, description="Prompt tokens served from the provider's cache."
    )
    latency_ms: int = Field(description="Wall-clock duration of the call in milliseconds.")
    pii_redactions: int = Field(
        default=0, description="How many PII spans were redacted before a cloud send."
    )
    created_at: datetime = Field(description="When the call completed.")
