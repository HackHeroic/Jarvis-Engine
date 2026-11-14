"""The You page API: what Jarvis knows about the user, and the settings they control.

Routing and parsing only; ``UserModelService`` does the work. Every route first
requires the database (``require_supabase``): without it the container runs on
in-memory repositories, where the page would read as a brand-new user. An outage
answers 503 in the one error envelope, never an empty page.
"""

from datetime import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import CurrentUser, get_container, require_supabase
from app.api.v1.endpoints.memories import MemoryOut
from app.domain.entities import AutonomyMode, FocusProfile, UserProfile
from app.services.user_model_service import (
    PatternShown,
    PrivacyReceipt,
    RuleShown,
    UserModelService,
)

router = APIRouter()


class YouProfile(BaseModel):
    """The user's settings, as the You page shows them."""

    display_name: str | None = Field(description="Name Jarvis addresses the user by, if set.")
    timezone: str = Field(description="IANA zone the user's day runs in.")
    wake_time: str = Field(
        pattern=r"^\d{2}:\d{2}$", description="Usual local wake-up time, HH:MM."
    )
    autonomy_mode: AutonomyMode = Field(
        description="How far Jarvis may act on its own: suggest_only, ask_first or auto_low_risk."
    )

    @classmethod
    def of(cls, profile: UserProfile) -> "YouProfile":
        return cls(
            display_name=profile.display_name,
            timezone=profile.timezone,
            wake_time=profile.wake_time.strftime("%H:%M"),
            autonomy_mode=profile.autonomy_mode,
        )


class YouResponse(BaseModel):
    """Everything Jarvis knows about the user, in one read."""

    profile: YouProfile = Field(description="The user's settings.")
    patterns: list[PatternShown] = Field(
        description="Patterns Jarvis sees in what happened, each with its evidence."
    )
    rules: list[RuleShown] = Field(description="Rules the user's plans respect.")
    memories: list[MemoryOut] = Field(
        description=(
            "Memories in use, oldest first. A learned pattern's memory is listed here only"
            " when the pattern is not among `patterns` (its evidence aged out): it may still"
            " shape plans, so it stays visible, as noticed, with its freshness."
        )
    )
    focus: FocusProfile = Field(description="Focus by local hour: learned, or the prior.")
    receipt: PrivacyReceipt = Field(
        description=(
            "Where the user's model calls of the last 30 days ran; `since` opens the window."
        )
    )


class SettingsUpdate(BaseModel):
    """Settings to change; any left out stay as they are. An unknown key is a 422."""

    model_config = ConfigDict(extra="forbid")

    autonomy_mode: AutonomyMode | None = Field(
        default=None, description="suggest_only, ask_first or auto_low_risk."
    )
    wake_time: str | None = Field(
        default=None, pattern=r"^\d{2}:\d{2}$", description="Usual local wake-up time, HH:MM."
    )
    timezone: str | None = Field(
        default=None, max_length=64, description="IANA zone name, e.g. Asia/Kolkata."
    )

    @field_validator("wake_time")
    @classmethod
    def _a_time_of_day(cls, value: str | None) -> str | None:
        if value is not None:
            time.fromisoformat(value)  # ValueError (422) for 25:99
        return value


def _service(request: Request) -> UserModelService:
    """The user model service, once the database is known to be there (else 503)."""
    require_supabase(request)
    return get_container(request).user_model_service


@router.get(
    "",
    response_model=YouResponse,
    summary="What Jarvis knows about you",
    description=(
        "The profile, the patterns Jarvis sees in what happened (each with its evidence and "
        "the memory to confirm or dismiss), the rules plans respect, the memories in use, the "
        "focus profile and the privacy receipt of the last 30 days. Looks for new patterns "
        "first; asks no model."
    ),
)
async def get_you(request: Request, user: CurrentUser) -> YouResponse:
    view = await _service(request).snapshot(user)
    return YouResponse(
        profile=YouProfile.of(view.profile),
        patterns=view.patterns,
        rules=view.rules,
        memories=[MemoryOut.of(memory, view.as_of) for memory in view.memories],
        focus=view.focus,
        receipt=view.receipt,
    )


@router.patch(
    "/settings",
    response_model=YouProfile,
    summary="Change your settings",
    description=(
        "Autonomy mode, wake time (HH:MM) and timezone (an IANA name). Settings left out "
        "stay as they are; an unknown timezone is 422 and nothing changes."
    ),
)
async def update_settings(
    update: SettingsUpdate, request: Request, user: CurrentUser
) -> YouProfile:
    profile = await _service(request).update_settings(
        user,
        autonomy_mode=update.autonomy_mode,
        wake_time=time.fromisoformat(update.wake_time) if update.wake_time else None,
        timezone=update.timezone,
    )
    return YouProfile.of(profile)
