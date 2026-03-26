"""User preferences for Proactive Task Workspace (learning style, etc.)."""

from typing import Optional

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from supabase import create_client


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def get_learning_style(
    user_id: str,
    supabase_client=None,
) -> str:
    """Get user's learning style preference. Defaults to 'reader' if not set."""
    client = supabase_client or _get_supabase()
    if not client:
        return "reader"
    try:
        result = (
            client.table("user_preferences")
            .select("learning_style")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if result.data and len(result.data) > 0:
            style = result.data[0].get("learning_style")
            if style in ("watcher", "reader", "interactive"):
                return style
    except Exception:
        pass
    return "reader"
