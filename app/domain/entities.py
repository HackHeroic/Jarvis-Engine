"""Domain entities: the things Jarvis reasons about, free of any framework or storage detail."""

import hashlib
import json
import math
from datetime import date, datetime, time
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


class SuggestionExpiry(str, Enum):
    """Why a suggestion expired, kept in its ``action`` JSON under EXPIRY_KEY.

    OUT_OF_DATE: its time passed, or a newer plan replaced what it would put in place;
    it was still made that day. NO_FIT: what it would put in place no longer fit (a
    clash with the user's tasks); a fresh one may be made the same day.
    """

    OUT_OF_DATE = "out_of_date"
    NO_FIT = "no_fit"


# Where a suggestion's action JSON records why it expired (``SuggestionExpiry``).
EXPIRY_KEY = "expired_because"


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


# The one definition of each vocabulary below: schemas and API models import them.
MemoryType = Literal[
    "fact", "preference", "behavioral_pattern", "temporal_event", "goal", "feedback", "constraint"
]

# A consent decision: run without asking, ask the user first, or drop it.
ConsentBehavior = Literal["allow", "ask", "deny"]

# A memory in one of these states never comes back on its own: the same fact said or
# observed again is recognised by its pattern key and not stored again.
SUPPRESSING_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN}
)

# Legal memory lifecycle moves (the State pattern, as a table). A mention, a confirmation
# or an edit keeps a memory in use: ACTIVE, and a FADED one comes back. The decay sweep
# fades an ACTIVE one. The user may dismiss a memory in use and forget anything not yet
# forgotten; DISMISSED leads only to FORGOTTEN, and FORGOTTEN nowhere: nothing automatic
# ever brings back what the user let go of. Every move is checked against this table
# (``check_memory_move``), and a store's compare-and-set guard for a move is the set of
# statuses it may start from (``memory_moves_to``), so a forget landing between a read
# and its write still wins.
MEMORY_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset(
        {MemoryStatus.ACTIVE, MemoryStatus.FADED, MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN}
    ),
    MemoryStatus.FADED: frozenset(
        {MemoryStatus.ACTIVE, MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN}
    ),
    MemoryStatus.DISMISSED: frozenset({MemoryStatus.FORGOTTEN}),
    MemoryStatus.FORGOTTEN: frozenset(),
}


def memory_moves_to(to: MemoryStatus | str) -> frozenset[MemoryStatus]:
    """The statuses a memory may move to ``to`` from: the guard of a write that sets it."""
    to = MemoryStatus(to)
    return frozenset(frm for frm, targets in MEMORY_TRANSITIONS.items() if to in targets)


def check_memory_move(
    memory_id: str | None, frm: MemoryStatus | str, to: MemoryStatus | str
) -> MemoryStatus:
    """``to``, if MEMORY_TRANSITIONS allows moving there from ``frm``.

    Raises InvalidStateTransition otherwise.
    """
    frm, to = MemoryStatus(frm), MemoryStatus(to)
    if to not in MEMORY_TRANSITIONS[frm]:
        raise InvalidStateTransition(
            f"This memory was {frm.value}, so it can't become {to.value}.",
            details={"memory_id": memory_id, "from": frm.value, "to": to.value},
        )
    return to

# At stability 1 a memory's strength falls by a factor of e every week.
BASE_HALFLIFE_HOURS = 168


# The states a draft is still under review in. The only definition: the persistence
# mapping imports it.
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


# Legal suggestion lifecycle moves. PENDING waits for the user; ACCEPTED is claimed (its
# action is running, or ran); the rest are terminal. Every status change is checked
# against this table first (``check_suggestion_move``); the store's compare-and-set
# on the status it expects is what stops two racing moves.
SUGGESTION_TRANSITIONS: dict[SuggestionStatus, frozenset[SuggestionStatus]] = {
    SuggestionStatus.PENDING: frozenset(
        {SuggestionStatus.ACCEPTED, SuggestionStatus.DISMISSED, SuggestionStatus.EXPIRED}
    ),
    # Back to PENDING when the action did not finish; EXPIRED when it was out of date.
    SuggestionStatus.ACCEPTED: frozenset(
        {SuggestionStatus.PENDING, SuggestionStatus.EXPIRED, SuggestionStatus.AUTO_APPLIED}
    ),
    SuggestionStatus.DISMISSED: frozenset(),
    SuggestionStatus.EXPIRED: frozenset(),
    SuggestionStatus.AUTO_APPLIED: frozenset(),
}


def made_that_day(status: SuggestionStatus | str, action: dict | None) -> bool:
    """Whether a suggestion counts as its trigger's suggestion for the day it was made.

    Every status does but an expiry that left room for a fresh one: NO_FIT, or one
    whose reason was never recorded (expired before reasons were kept).
    """
    if SuggestionStatus(status) is not SuggestionStatus.EXPIRED:
        return True
    return (action or {}).get(EXPIRY_KEY) == SuggestionExpiry.OUT_OF_DATE.value


def check_suggestion_move(frm: SuggestionStatus, to: SuggestionStatus) -> SuggestionStatus:
    """``to``, if SUGGESTION_TRANSITIONS allows moving there from ``frm``.

    Raises InvalidStateTransition otherwise.
    """
    frm, to = SuggestionStatus(frm), SuggestionStatus(to)
    if to not in SUGGESTION_TRANSITIONS[frm]:
        raise InvalidStateTransition(
            f"A {frm.value} suggestion cannot become {to.value}.",
            details={"from": frm.value, "to": to.value},
        )
    return to


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


class GoalDeadlineUpdate(BaseModel):
    """A deadline learned for a goal (from a document, or the user), with its context."""

    user_id: str = Field(description="Owner of the goal.")
    goal_id: str | None = Field(default=None, description="Goal the deadline is for, if known.")
    source: str = Field(description="Where the deadline came from (e.g. ingestion).")
    deadline_date: date | None = Field(default=None, description="The deadline's date, if parsed.")
    deadline_raw: str | None = Field(
        default=None, description="The deadline as it was written, before parsing."
    )
    context_snippet: str | None = Field(
        default=None, description="Text around the deadline: what it is about."
    )
    created_at: datetime | None = Field(
        default=None, description="When it was stored; set by the store, None until then."
    )


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
        default=None,
        description=(
            "Id shared by the rows one draft acceptance wrote: a fresh UUID minted per"
            " accept, not the draft's id."
        ),
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
    created_at: datetime | None = Field(
        default=None, description="When it was stored; set by the store, None until then."
    )

    @property
    def version(self) -> str:
        """What the user is looking at, as one opaque value (an ETag): it changes whenever
        the draft's tasks or slots change. An accept that names the version it read is
        refused once the draft has changed since (``StaleVersionError``)."""
        seen = json.dumps(
            {"tasks": self.tasks, "schedule": self.schedule},
            sort_keys=True, separators=(",", ":"), default=str,
        )
        return hashlib.sha256(seen.encode()).hexdigest()[:16]

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
    source: MemorySource = Field(
        default=MemorySource.SAID,
        description="Whether the user stated it or Jarvis inferred it from behaviour.",
    )
    created_at: datetime | None = Field(
        default=None, description="When it was stored; set by the store, None until then."
    )


# SuperMemo-2's easiness-factor floor: a habit harder than this is broken down instead.
SM2_EF_FLOOR = 1.3


class HabitTracker(BaseModel):
    """The SM-2 state of one rule (or habit) the user reviews: "still how you work?" 0-5."""

    id: str = Field(description="Tracker identifier (a UUID).")
    user_id: str = Field(description="Owner of the tracker.")
    habit_id: str | None = Field(default=None, description="The habit it tracks, if named.")
    constraint_id: str | None = Field(
        default=None, description="The behavioural constraint it tracks, if any."
    )
    repetitions: int = Field(
        default=0, ge=0, description="Reviews in a row rated 3 or better (0 after a lapse)."
    )
    quality_last: int | None = Field(
        default=None, ge=0, le=5, description="The last review's rating, 0 to 5; None before one."
    )
    ef: float = Field(
        default=2.5, ge=SM2_EF_FLOOR, description="SM-2 easiness factor, never below 1.3."
    )
    next_interval_days: int = Field(
        default=1, ge=0, description="Days after the last review that the next one is due."
    )
    last_done_at: datetime | None = Field(
        default=None, description="Last reviewed; before any review, when started. None: due now."
    )


class IngestedDocument(BaseModel):
    """A document the user uploaded: its record. Its text is in the knowledge store (as
    chunks carrying ``source_id``) and its file, when one was kept, in the file store."""

    id: str | None = Field(default=None, description="Storage row id; None until persisted.")
    user_id: str = Field(description="Owner of the document.")
    source_id: str = Field(
        description="The document's id, unique per user (ingestion_<12 hex>); its chunks carry it."
    )
    file_name: str | None = Field(default=None, description="The uploaded file's own name.")
    media_type: str | None = Field(
        default=None, description="The kind it was uploaded as: 'pdf', 'image', 'png', 'jpeg'."
    )
    document_topics: list[str] = Field(
        default_factory=list, description="The topics read from it (1 to 5 short phrases)."
    )
    chunk_count: int = Field(default=0, ge=0, description="How many chunks its text became.")
    created_at: datetime | None = Field(
        default=None, description="When it was ingested; stamped by the store when unset."
    )


class TaskMaterialLink(BaseModel):
    """One of the user's documents, linked to one of their tasks as material for it."""

    user_id: str = Field(description="Owner of the task and the document.")
    task_id: str = Field(description="The task the document is material for.")
    source_id: str = Field(description="The document (IngestedDocument.source_id).")
    source_type: str = Field(default="chunk", description="What is linked: the document's chunks.")
    document_topics: list[str] = Field(
        default_factory=list, description="The document's topics when the link was made."
    )
    linked_at: datetime | None = Field(
        default=None, description="When the link was made; stamped by the store when unset."
    )


class StoredFile(BaseModel):
    """An uploaded file as it was kept: its bytes and the content type it was stored with."""

    content: bytes = Field(description="The file's bytes, exactly as uploaded.")
    content_type: str = Field(description="Its MIME type (application/pdf, image/png...).")


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

    behavior: ConsentBehavior = Field(
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
    consent_behavior: ConsentBehavior = Field(
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
        description="Running summary of the conversation: every message up to "
        "summary_through, and, once it went idle, the newest ones after it too. None until "
        "the first one is written.",
    )
    summary_through: datetime | None = Field(
        default=None,
        description="Where the verbatim window starts: the history shows only the messages "
        "created after it. It never moves back.",
    )
    summary_updated_at: datetime | None = Field(
        default=None, description="When the summary was last written."
    )
    summary_covered_through: datetime | None = Field(
        default=None,
        description="created_at of the newest message the summary covers: never before "
        "summary_through, and past it once an idle summary covered the turns the window "
        "still shows word for word. None for a summary written before it was tracked.",
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
