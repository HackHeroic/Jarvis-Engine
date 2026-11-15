"""The Habits page's check-ins on the user's rules: what ``GET /habits/tracker/due``
lists and what ``POST /habits/tracker/{id}/complete`` answers (served by ``RuleCheckIns``).
"""

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.domain.entities import MemorySource


class RuleCheckIn(BaseModel):
    """One rule Jarvis checks in on ("Still how you work?"), and when it next asks."""

    id: str = Field(
        description="The rule's tracker; rate a check-in at /habits/tracker/{id}/complete."
    )
    constraint_id: str = Field(
        description="The rule itself; delete it at /habits/constraints/{constraint_id}."
    )
    rule_text: str = Field(description="The rule in the user's words, or as Jarvis inferred it.")
    source: MemorySource = Field(
        description="'said': the user stated it; 'inferred': Jarvis noticed it from behaviour."
    )
    learned_at: datetime | None = Field(
        description="When the rule was stored; null for a rule older than the stamp."
    )
    last_reviewed_at: datetime | None = Field(
        description="When the user last answered a check-in on it; null before the first."
    )
    next_due_on: date = Field(
        description="The day the next check-in is due, on the user's clock (``timezone``)."
    )
    interval_days: int = Field(
        ge=1,
        description=(
            "Days from the last check-in (or, before the first, from learning the rule) to "
            "the next: it grows while the rule holds and drops back to 1 after a low rating."
        ),
    )


class RuleCheckInList(BaseModel):
    """Every rule the user has, split by whether its check-in is due."""

    timezone: str = Field(description="The user's zone, the clock of every date here.")
    today: date = Field(description="Today on the user's clock.")
    due_trackers: list[RuleCheckIn] = Field(
        description="Check-ins due today or earlier, the one waiting longest first."
    )
    upcoming_trackers: list[RuleCheckIn] = Field(
        description="Rules whose next check-in is after today, the soonest first."
    )


class CheckInReviewed(BaseModel):
    """A check-in was answered: when the rule comes back."""

    next_review_date: datetime = Field(description="When the next check-in is due (UTC).")
    next_interval_days: int = Field(ge=1, description="Days from now until that check-in.")
    next_due_on: date = Field(description="The day of that check-in, on the user's clock.")
