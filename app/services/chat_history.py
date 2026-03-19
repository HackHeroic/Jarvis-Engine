"""Chat history service — session CRUD, message persistence, context building.

All DB queries filter by user_id (IDOR protection).
Supabase calls are wrapped in asyncio.to_thread because the client is sync.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure helpers (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def generate_session_title(first_message: str) -> str:
    """Generate a short session title from the first user message. No LLM call.

    Rules:
    - Empty / blank → "New conversation"
    - First sentence (up to first '.', '!', or '?') or first 60 chars,
      whichever is shorter.
    - If the chosen slice is longer than 60 chars, truncate to 60 + "..."
    """
    if not first_message or not first_message.strip():
        return "New conversation"

    text = first_message.strip()

    # Find first sentence boundary
    sentence_end = len(text)
    for i, ch in enumerate(text):
        if ch in (".", "!", "?"):
            sentence_end = i + 1  # include the punctuation
            break

    candidate = text[:min(sentence_end, 60)]

    if len(candidate) < sentence_end and sentence_end <= len(text):
        # Sentence was longer than 60 chars; we already capped at 60
        candidate = text[:60] + "..."
    elif sentence_end > 60:
        # No sentence boundary found within 60 chars
        candidate = text[:60] + "..."

    return candidate.strip()


def _truncate_messages(messages: list[dict], max_chars: int = 500) -> list[dict]:
    """Return a cleaned copy of messages for LLM context injection.

    - Keeps only ``role`` and ``content`` fields (strips ``metadata``,
      ``created_at``, ``intent``, and any other extra keys).
    - Truncates ``content`` to ``max_chars`` characters; appends "..." when
      truncated.
    - Skips messages that lack a ``role`` or ``content`` field.
    """
    result: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role is None or content is None:
            continue
        content_str = str(content)
        if len(content_str) > max_chars:
            content_str = content_str[:max_chars] + "..."
        result.append({"role": role, "content": content_str})
    return result


# ---------------------------------------------------------------------------
# Async DB helpers
# ---------------------------------------------------------------------------

async def get_or_create_session(
    user_id: str,
    conversation_id: Optional[str],
    supabase: Any,
) -> str:
    """Return an existing session_id that belongs to ``user_id``, or create a new one.

    Graceful degradation: if ``supabase`` is None, returns an ephemeral UUID
    that lives only for the duration of the request.
    """
    if supabase is None:
        return str(uuid.uuid4())

    if conversation_id:
        try:
            def _fetch() -> list[dict]:
                resp = (
                    supabase.table("chat_sessions")
                    .select("id")
                    .eq("id", conversation_id)
                    .eq("user_id", user_id)
                    .limit(1)
                    .execute()
                )
                return resp.data or []

            rows = await asyncio.to_thread(_fetch)
            if rows:
                return conversation_id
        except Exception:
            logger.warning("get_or_create_session: failed to look up session %s", conversation_id)

    # Create a new session
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        def _insert() -> None:
            supabase.table("chat_sessions").insert(
                {
                    "id": new_id,
                    "user_id": user_id,
                    "title": None,
                    "created_at": now,
                    "updated_at": now,
                    "is_archived": False,
                }
            ).execute()

        await asyncio.to_thread(_insert)
    except Exception:
        logger.warning("get_or_create_session: failed to persist new session; returning ephemeral id")

    return new_id


async def save_user_message(
    session_id: str,
    user_id: str,
    content: str,
    supabase: Any,
) -> Optional[str]:
    """Persist a user message.  Returns ``message_id`` or ``None`` if DB unavailable.

    Also:
    - Updates ``chat_sessions.updated_at``.
    - Sets the session title from this message if the session title is not yet set.
    """
    if supabase is None:
        return None

    message_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        def _write() -> None:
            supabase.table("chat_messages").insert(
                {
                    "id": message_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "user",
                    "content": content,
                    "created_at": now,
                }
            ).execute()

            # Bump session timestamp
            supabase.table("chat_sessions").update(
                {"updated_at": now}
            ).eq("id", session_id).eq("user_id", user_id).execute()

            # Set title if not yet set
            session_resp = (
                supabase.table("chat_sessions")
                .select("title")
                .eq("id", session_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            rows = session_resp.data or []
            if rows and not rows[0].get("title"):
                title = generate_session_title(content)
                supabase.table("chat_sessions").update(
                    {"title": title}
                ).eq("id", session_id).eq("user_id", user_id).execute()

        await asyncio.to_thread(_write)
        return message_id
    except Exception:
        logger.warning("save_user_message: failed to persist message for session %s", session_id)
        return None


async def save_assistant_message(
    session_id: str,
    user_id: str,
    content: str,
    intent: str,
    metadata: dict,
    supabase: Any,
) -> Optional[str]:
    """Persist an assistant response.  Returns ``message_id`` or ``None`` on failure."""
    if supabase is None:
        return None

    message_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        def _write() -> None:
            supabase.table("chat_messages").insert(
                {
                    "id": message_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "assistant",
                    "content": content,
                    "intent": intent,
                    "metadata": metadata,
                    "created_at": now,
                }
            ).execute()

            supabase.table("chat_sessions").update(
                {"updated_at": now}
            ).eq("id", session_id).eq("user_id", user_id).execute()

        await asyncio.to_thread(_write)
        return message_id
    except Exception:
        logger.warning("save_assistant_message: failed to persist message for session %s", session_id)
        return None


async def get_recent_messages(
    session_id: str,
    user_id: str,
    limit: int = 20,
    supabase: Any = None,
) -> list[dict]:
    """Fetch the last ``limit`` messages ordered by ``created_at`` ASC."""
    if supabase is None:
        return []

    try:
        def _fetch() -> list[dict]:
            resp = (
                supabase.table("chat_messages")
                .select("id, session_id, role, content, intent, metadata, created_at")
                .eq("session_id", session_id)
                .eq("user_id", user_id)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            return resp.data or []

        return await asyncio.to_thread(_fetch)
    except Exception:
        logger.warning("get_recent_messages: failed for session %s", session_id)
        return []


async def build_context_messages(
    session_id: str,
    user_id: str,
    max_messages: int = 10,
    max_chars_per_message: int = 500,
    supabase: Any = None,
) -> list[dict]:
    """Build an LLM-ready message list from conversation history.

    Returns a list of ``{"role": "user"|"assistant", "content": "..."}`` dicts.

    If there are more messages in the DB than ``max_messages``, a system
    message summarising the older turns is prepended to the window.

    The **current** user message is NOT included — this list is the prior context
    that is injected before the new turn.
    """
    if supabase is None:
        return []

    # Fetch one extra message to detect whether there is overflow history
    all_recent = await get_recent_messages(
        session_id, user_id, limit=max_messages + 1, supabase=supabase
    )

    overflow = len(all_recent) > max_messages
    window = all_recent[-max_messages:] if overflow else all_recent

    cleaned = _truncate_messages(window, max_chars=max_chars_per_message)

    if overflow:
        older_count = len(all_recent) - max_messages
        summary_msg = {
            "role": "system",
            "content": (
                f"[Context summary: there are {older_count} earlier message(s) in this "
                "conversation that are not shown here. The messages below represent the "
                "most recent context.]"
            ),
        }
        return [summary_msg] + cleaned

    return cleaned


async def list_sessions(
    user_id: str,
    limit: int = 50,
    supabase: Any = None,
) -> list[dict]:
    """List the user's non-archived sessions ordered by ``updated_at`` DESC."""
    if supabase is None:
        return []

    try:
        def _fetch() -> list[dict]:
            resp = (
                supabase.table("chat_sessions")
                .select("id, title, created_at, updated_at, is_archived")
                .eq("user_id", user_id)
                .eq("is_archived", False)
                .order("updated_at", desc=True)
                .limit(limit)
                .execute()
            )
            return resp.data or []

        return await asyncio.to_thread(_fetch)
    except Exception:
        logger.warning("list_sessions: failed for user %s", user_id)
        return []
