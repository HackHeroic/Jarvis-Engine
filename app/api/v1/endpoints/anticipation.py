"""The suggestions Jarvis makes on its own: list them, accept or dismiss one, run a trigger.

Routing and request parsing only; ``AnticipationService`` does the work. Errors
are ``JarvisError``s, answered in the one error envelope.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, get_container
from app.domain.anticipation import ActionResult, ConsentDecision
from app.domain.entities import ConsentReasonKind, Suggestion, SuggestionStatus

router = APIRouter()


class SuggestionOut(BaseModel):
    """A suggestion as the app shows it."""

    id: str = Field(description="Suggestion id.")
    trigger: str = Field(description="Trigger that made it (e.g. morning_brief).")
    kind: str = Field(description="Kind of suggestion.")
    title: str = Field(description="Short headline.")
    body: str = Field(description="Full text.")
    status: SuggestionStatus = Field(description="Where it is in its lifecycle.")
    consent_behavior: Literal["allow", "ask", "deny"] = Field(
        description="Whether it was applied without asking or is waiting for the user."
    )
    consent_kind: ConsentReasonKind | None = Field(
        description="Which consent rule decided; null on older suggestions."
    )
    consent_reason: str = Field(description="Why, in words the user can read.")
    created_at: datetime = Field(description="When it was made.")
    expires_at: datetime | None = Field(description="After this instant it is stale, if ever.")
    details: dict = Field(description="The action accepting it would run, as stored.")

    @classmethod
    def of(cls, s: Suggestion) -> "SuggestionOut":
        return cls(
            id=s.id or "",
            trigger=s.trigger,
            kind=s.kind,
            title=s.title,
            body=s.body,
            status=s.status,
            consent_behavior=s.consent_behavior,
            consent_kind=s.consent_kind,
            consent_reason=s.consent_reason,
            created_at=s.created_at,
            expires_at=s.expires_at,
            details=s.action,
        )


class SuggestionListResponse(BaseModel):
    suggestions: list[SuggestionOut] = Field(description="Newest first.")


class ActionResultOut(BaseModel):
    """What accepting a suggestion did."""

    kind: str = Field(description="Kind of the action that ran.")
    message: str = Field(description="What happened, in words the user can read.")
    tasks_written: int | None = Field(description="Tasks written to the plan, if any.")
    draft_id: str | None = Field(description="Draft proposed for review, if one was.")

    @classmethod
    def of(cls, r: ActionResult) -> "ActionResultOut":
        return cls(
            kind=r.kind, message=r.message, tasks_written=r.tasks_written, draft_id=r.draft_id
        )


class AcceptSuggestionResponse(BaseModel):
    suggestion: SuggestionOut = Field(description="The suggestion, now accepted.")
    result: ActionResultOut = Field(description="What accepting it did.")


class ConsentOut(BaseModel):
    """The consent decision behind a trigger run."""

    behavior: Literal["allow", "ask", "deny"] = Field(
        description="Apply without asking, ask first, or not at all."
    )
    kind: ConsentReasonKind = Field(description="Which consent rule decided.")
    message: str = Field(description="Why, in words the user can read.")

    @classmethod
    def of(cls, d: ConsentDecision) -> "ConsentOut":
        return cls(behavior=d.behavior, kind=d.kind, message=d.message)


class RunTriggerRequest(BaseModel):
    trigger: Literal["morning_brief", "pattern_rule"] = Field(
        description="Trigger to run now, whether or not it is due; once a day still holds."
    )


class RunTriggerResponse(BaseModel):
    status: Literal["created", "already_done", "not_due", "nothing_to_say", "denied"] = Field(
        description="How the run ended."
    )
    suggestion: SuggestionOut | None = Field(description="The suggestion made, if one was.")
    consent: ConsentOut | None = Field(description="The consent decision, if consent was asked.")


@router.get(
    "/suggestions",
    response_model=SuggestionListResponse,
    summary="List suggestions",
    description="The user's suggestions in one status (pending unless asked), newest first.",
)
async def list_suggestions(
    request: Request,
    user: CurrentUser,
    status: SuggestionStatus = Query(
        default=SuggestionStatus.PENDING, description="Only suggestions in this status."
    ),
) -> SuggestionListResponse:
    found = await get_container(request).anticipation.list_suggestions(user, status=status)
    return SuggestionListResponse(suggestions=[SuggestionOut.of(s) for s in found])


@router.post(
    "/suggestions/{suggestion_id}/accept",
    response_model=AcceptSuggestionResponse,
    summary="Accept a suggestion",
    description=(
        "Runs the suggestion's action once. A second accept, or an accept of a "
        "suggestion no longer pending, is a 409; one whose plan was replaced is a 409 "
        "and is marked expired."
    ),
)
async def accept_suggestion(
    suggestion_id: str, request: Request, user: CurrentUser
) -> AcceptSuggestionResponse:
    suggestion, result = await get_container(request).anticipation.accept(user, suggestion_id)
    return AcceptSuggestionResponse(
        suggestion=SuggestionOut.of(suggestion), result=ActionResultOut.of(result)
    )


@router.post(
    "/suggestions/{suggestion_id}/dismiss",
    response_model=SuggestionOut,
    summary="Dismiss a suggestion",
    description="Dismisses a pending suggestion; the same suggestion is never made again.",
)
async def dismiss_suggestion(
    suggestion_id: str, request: Request, user: CurrentUser
) -> SuggestionOut:
    return SuggestionOut.of(await get_container(request).anticipation.dismiss(user, suggestion_id))


@router.post(
    "/run",
    response_model=RunTriggerResponse,
    summary="Run a trigger now",
    description="Runs a trigger for the user even if it is not due yet (once a day still holds).",
)
async def run_trigger(
    body: RunTriggerRequest, request: Request, user: CurrentUser
) -> RunTriggerResponse:
    outcome = await get_container(request).anticipation.run_trigger(
        user, body.trigger, force=True
    )
    return RunTriggerResponse(
        status=outcome.status,
        suggestion=SuggestionOut.of(outcome.suggestion) if outcome.suggestion else None,
        consent=ConsentOut.of(outcome.decision) if outcome.decision else None,
    )
