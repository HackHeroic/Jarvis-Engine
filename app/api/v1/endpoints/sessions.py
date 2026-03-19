"""Session management endpoints for chat history."""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.chat_history import (
    list_sessions,
    get_recent_messages,
)

router = APIRouter()


@router.get("/", summary="List chat sessions")
async def get_sessions(user_id: str, limit: int = 50, http_request: Request = None):
    """List user's chat sessions ordered by most recent."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    sessions = await list_sessions(user_id, limit=limit, supabase=supabase)
    return {"sessions": sessions}


@router.get("/{session_id}", summary="Get session with messages")
async def get_session(session_id: str, user_id: str, http_request: Request):
    """Retrieve a session and its messages."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

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

    session = await asyncio.to_thread(_get_session)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = await get_recent_messages(
        session_id, user_id, limit=100, supabase=supabase
    )
    return {"session": session, "messages": messages}


class UpdateSessionRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    title: Optional[str] = Field(default=None, description="New title")
    is_archived: Optional[bool] = Field(default=None, description="Archive/unarchive")


@router.put("/{session_id}", summary="Update session")
async def update_session(
    session_id: str, request: UpdateSessionRequest, http_request: Request
):
    """Update session title or archive status."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    updates = {}
    if request.title is not None:
        updates["title"] = request.title
    if request.is_archived is not None:
        updates["is_archived"] = request.is_archived

    if not updates:
        return {"status": "no_changes"}

    def _update():
        supabase.table("chat_sessions").update(updates).eq(
            "session_id", session_id
        ).eq("user_id", request.user_id).execute()

    await asyncio.to_thread(_update)
    return {"status": "updated", "session_id": session_id}


@router.delete("/{session_id}", summary="Archive session")
async def delete_session(session_id: str, user_id: str, http_request: Request):
    """Soft-delete (archive) a session."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")

    def _archive():
        supabase.table("chat_sessions").update(
            {"is_archived": True}
        ).eq("session_id", session_id).eq("user_id", user_id).execute()

    await asyncio.to_thread(_archive)
    return {"status": "archived", "session_id": session_id}
