"""Session management endpoints for chat history.

The session and message rows have no repository yet (Plan 3 cut Task 6): these routes
read and write ``chat_sessions`` through the app's Supabase client, a documented
leftover of the layering test's raw-table allow-list. No database is 503
``persistence_unavailable``; a session that is not the caller's is 404 ``not_found``.
"""

import asyncio
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_supabase
from app.domain import errors
from app.services.chat_history import (
    list_sessions,
    get_recent_messages,
)

router = APIRouter()

_UNREACHABLE = "Could not reach the database."


class SessionSummary(BaseModel):
    """One conversation in the list: the columns the sidebar shows."""

    id: str = Field(description="The session's id (the chat's conversation_id).")
    title: Optional[str] = Field(default=None, description="Its title, if it has one.")
    created_at: Optional[str] = Field(default=None, description="When it was started.")
    updated_at: Optional[str] = Field(default=None, description="When it was last active.")
    is_archived: Optional[bool] = Field(default=False, description="Archived (hidden from the list).")
    is_pinned: Optional[bool] = Field(default=False, description="Pinned to the top of the list.")


class SessionRecord(SessionSummary):
    """A session's whole row, as stored: the summary columns and any others it has."""

    model_config = ConfigDict(extra="allow")

    user_id: Optional[str] = Field(default=None, description="The session's owner.")


class SessionMessage(BaseModel):
    """One message of a conversation."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(description="The message's id.")
    session_id: Optional[str] = Field(default=None, description="The session it belongs to.")
    role: str = Field(description="'user' or 'assistant'.")
    content: str = Field(description="What was said.")
    intent: Optional[str] = Field(default=None, description="The turn's intent, if recorded.")
    metadata: Optional[dict[str, Any]] = Field(
        default=None, description="What the turn left with it (schedule, draft id, intent)."
    )
    created_at: Optional[str] = Field(default=None, description="When it was said.")


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary] = Field(
        description="The caller's unarchived sessions, pinned first, then most recent."
    )


class SessionMessagesResponse(BaseModel):
    messages: list[SessionMessage] = Field(description="The session's messages, oldest first.")


class SessionDetailResponse(BaseModel):
    session: SessionRecord = Field(description="The session's row.")
    messages: list[SessionMessage] = Field(
        description="Its newest 100 messages, oldest first."
    )


class SessionUpdateResponse(BaseModel):
    status: Literal["updated", "no_changes"] = Field(
        description="'updated', or 'no_changes' when the request named nothing to change."
    )
    session_id: Optional[str] = Field(
        default=None, description="The session updated; absent for 'no_changes'."
    )


class SessionArchiveResponse(BaseModel):
    status: Literal["archived"] = Field(description="Always 'archived'.")
    session_id: str = Field(description="The session archived.")


class SessionsArchivedResponse(BaseModel):
    status: Literal["archived"] = Field(description="Always 'archived'.")
    archived_count: int = Field(ge=0, description="How many sessions were archived.")


async def _run(call: Any) -> Any:
    """``call`` off the event loop; a failed query is ``PersistenceError``."""
    try:
        return await asyncio.to_thread(call)
    except Exception as exc:
        raise errors.PersistenceError(_UNREACHABLE) from exc


@router.get("/", response_model=SessionListResponse, summary="List chat sessions")
async def get_sessions(
    user_id: str, http_request: Request, limit: int = 50
) -> SessionListResponse:
    """List user's chat sessions ordered by most recent (none without a database)."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = getattr(db_client, "supabase", None)
    sessions = await list_sessions(user_id, limit=limit, supabase=supabase)
    return SessionListResponse(sessions=sessions)


@router.get(
    "/{session_id}/messages",
    response_model=SessionMessagesResponse,
    summary="Get session messages",
)
async def get_session_messages(
    session_id: str, user_id: str, http_request: Request, limit: int = 100
) -> SessionMessagesResponse:
    """Retrieve messages for a session."""
    supabase = require_supabase(http_request)
    messages = await get_recent_messages(session_id, user_id, limit=limit, supabase=supabase)
    return SessionMessagesResponse(messages=messages)


@router.get(
    "/{session_id}", response_model=SessionDetailResponse, summary="Get session with messages"
)
async def get_session(
    session_id: str, user_id: str, http_request: Request
) -> SessionDetailResponse:
    """Retrieve a session and its messages."""
    supabase = require_supabase(http_request)

    def _get_session():
        resp = (
            supabase.table("chat_sessions")
            .select("*")
            .eq("id", session_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    session = await _run(_get_session)
    if not session:
        raise errors.NotFoundError("Session not found.")

    messages = await get_recent_messages(session_id, user_id, limit=100, supabase=supabase)
    return SessionDetailResponse(session=session, messages=messages)


class UpdateSessionRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    title: Optional[str] = Field(default=None, description="New title")
    is_archived: Optional[bool] = Field(default=None, description="Archive/unarchive")
    is_pinned: Optional[bool] = Field(default=None, description="Pin/unpin")


@router.put("/{session_id}", response_model=SessionUpdateResponse, summary="Update session")
async def update_session(
    session_id: str, request: UpdateSessionRequest, http_request: Request
) -> SessionUpdateResponse:
    """Update session title or archive status."""
    supabase = require_supabase(http_request)

    updates = {}
    if request.title is not None:
        updates["title"] = request.title
    if request.is_archived is not None:
        updates["is_archived"] = request.is_archived
    if request.is_pinned is not None:
        updates["is_pinned"] = request.is_pinned

    if not updates:
        return SessionUpdateResponse(status="no_changes")

    def _update():
        supabase.table("chat_sessions").update(updates).eq(
            "id", session_id
        ).eq("user_id", request.user_id).execute()

    await _run(_update)
    return SessionUpdateResponse(status="updated", session_id=session_id)


@router.delete("/", response_model=SessionsArchivedResponse, summary="Archive all sessions")
async def delete_all_sessions(user_id: str, http_request: Request) -> SessionsArchivedResponse:
    """Soft-delete (archive) all sessions for a user."""
    supabase = require_supabase(http_request)

    def _archive_all():
        result = (
            supabase.table("chat_sessions")
            .update({"is_archived": True})
            .eq("user_id", user_id)
            .eq("is_archived", False)
            .execute()
        )
        return len(result.data) if result.data else 0

    count = await _run(_archive_all)
    return SessionsArchivedResponse(status="archived", archived_count=count)


@router.delete(
    "/{session_id}", response_model=SessionArchiveResponse, summary="Archive session"
)
async def delete_session(
    session_id: str, user_id: str, http_request: Request
) -> SessionArchiveResponse:
    """Soft-delete (archive) a session."""
    supabase = require_supabase(http_request)

    def _archive():
        supabase.table("chat_sessions").update(
            {"is_archived": True}
        ).eq("id", session_id).eq("user_id", user_id).execute()

    await _run(_archive)
    return SessionArchiveResponse(status="archived", session_id=session_id)
