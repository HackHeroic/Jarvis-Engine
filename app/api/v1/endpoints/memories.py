"""The memories API (the Memory panel): what Jarvis remembers, and the user's controls.

Routing and parsing only. Every route is a ``MemoryService`` move, taken from the
composition root (``get_container``). Another user's memory reads as absent and
answers 404, never 403, which would confirm it exists. A dismissed or forgotten memory
cannot be confirmed or edited (409). Errors use the one envelope.

Every route first requires the database (``require_supabase``): without it the
container runs on in-memory repositories, where the user's memories would read as
an empty list and every one as unknown. An outage answers 503, never "no memories".
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, get_container, require_supabase
from app.domain.entities import Memory, MemorySource, MemoryType
from app.services.memory.memory_service import MemoryService

router = APIRouter()


class MemoryOut(BaseModel):
    """One memory as the Memory panel shows it."""

    memory_id: str = Field(description="The memory's id.")
    memory_type: MemoryType = Field(description="What kind of thing is remembered.")
    content: str = Field(description="The memory in words.")
    confidence: float = Field(description="How sure Jarvis is that it holds, 0 to 1.")
    provenance: MemorySource = Field(
        description="'said': the user said it. 'inferred': Jarvis noticed it from what happened."
    )
    freshness: float = Field(
        description="Strength now: 1 when just learned or confirmed, falling while unused."
    )
    noted_at: datetime = Field(description="When it was first stored.")
    last_confirmed_at: datetime | None = Field(
        description="When it was last said again or confirmed, if ever."
    )
    observation_count: int = Field(description="How many times it has been said or observed.")
    pattern_key: str | None = Field(
        description="Stable identity of a learned pattern, if it is one."
    )

    @classmethod
    def of(cls, memory: Memory, now: datetime) -> "MemoryOut":
        return cls(
            memory_id=memory.id,
            memory_type=memory.memory_type,
            content=memory.content,
            confidence=memory.confidence,
            provenance=memory.provenance,
            freshness=memory.strength_at(now),
            noted_at=memory.created_at,
            last_confirmed_at=memory.last_confirmed_at,
            observation_count=memory.observation_count,
            pattern_key=memory.pattern_key,
        )


class MemoryListResponse(BaseModel):
    """The memories Jarvis is using for the user."""

    memories: list[MemoryOut] = Field(description="Active memories, oldest first.")
    count: int = Field(description="How many memories are listed.")


class ForgetResponse(BaseModel):
    """The memory is forgotten."""

    status: Literal["forgotten"] = Field(description="Always 'forgotten'.")


class MemoryEdit(BaseModel):
    """New words or a new type for a memory; at least one."""

    content: str | None = Field(
        default=None, min_length=1, max_length=2000, description="The memory's new words."
    )
    memory_type: MemoryType | None = Field(default=None, description="The memory's new type.")


def _memories(request: Request) -> MemoryService:
    """The memory service, once the database is known to be there (else 503)."""
    require_supabase(request)
    return get_container(request).memory_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get(
    "/",
    response_model=MemoryListResponse,
    summary="List what Jarvis remembers",
    description=(
        "The user's active memories: not dismissed, forgotten, faded, superseded or "
        "expired, and still strong enough to be used."
    ),
)
async def list_memories(
    request: Request,
    user: CurrentUser,
    memory_type: MemoryType | None = Query(default=None, description="Only this type."),
) -> MemoryListResponse:
    memories = _memories(request)
    types = frozenset({memory_type}) if memory_type is not None else None
    now = _now()
    shown = [
        MemoryOut.of(m, now) for m in await memories.list_active(user, types=types, now=now)
    ]
    return MemoryListResponse(memories=shown, count=len(shown))


@router.patch(
    "/{memory_id}",
    response_model=MemoryOut,
    summary="Edit a memory",
    description=(
        "Change a memory's words or type. Unknown or another user's: 404; dismissed or "
        "forgotten: 409."
    ),
)
async def edit_memory(
    memory_id: str, edit: MemoryEdit, request: Request, user: CurrentUser
) -> MemoryOut:
    memory = await _memories(request).edit(
        user, memory_id, content=edit.content, memory_type=edit.memory_type
    )
    return MemoryOut.of(memory, _now())


@router.delete(
    "/{memory_id}",
    response_model=ForgetResponse,
    summary="Forget a memory for good",
    description=(
        "Erases the memory's words at once and removes it from every list. A tombstone "
        "stays: its embedding and pattern key are kept, so when the same fact is said or "
        "noticed again Jarvis recognises it and does not store it again. A dismissed memory "
        "can be forgotten too. The memory of stating a rule takes the rule with it: plans "
        "stop applying it. Another user's memory: 404."
    ),
)
async def forget_memory(memory_id: str, request: Request, user: CurrentUser) -> ForgetResponse:
    _memories(request)  # the database first (503), as on every route
    await get_container(request).stated_rules.forget_memory(user, memory_id)
    return ForgetResponse(status="forgotten")


@router.post(
    "/{memory_id}/confirm",
    response_model=MemoryOut,
    summary="Confirm a memory",
    description=(
        "The user says it holds: it is reinforced and Jarvis is at least 0.9 sure of it. "
        "A dismissed or forgotten memory stays let go of: 409."
    ),
)
async def confirm_memory(memory_id: str, request: Request, user: CurrentUser) -> MemoryOut:
    return MemoryOut.of(await _memories(request).confirm(user, memory_id), _now())


@router.post(
    "/{memory_id}/dismiss",
    response_model=MemoryOut,
    summary="Dismiss a memory",
    description=(
        "The user says it does not hold: it is hidden for good, and the same fact said or "
        "noticed again is not stored. A forgotten memory: 409."
    ),
)
async def dismiss_memory(memory_id: str, request: Request, user: CurrentUser) -> MemoryOut:
    return MemoryOut.of(await _memories(request).dismiss(user, memory_id), _now())
