"""Supabase client connection logic."""

import asyncio
from supabase import Client, create_client

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL


class DatabaseClient:
    """Supabase database client with async connection verification."""

    def __init__(self) -> None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise ValueError(
                "Supabase not configured: set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env "
                f"(SUPABASE_URL present: {bool(SUPABASE_URL)}, key present: {bool(SUPABASE_SERVICE_KEY)})"
            )
        self.supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    async def check_connection(self) -> bool:
        """
        Verify the database is reachable by performing a basic query.
        Returns True if successful.
        """
        def _check() -> bool:
            # user_tasks is the core table (created by 004, altered by the CLI
            # migrations); no migration ever created user_state.
            response = self.supabase.table("user_tasks").select("*").limit(1).execute()
            return response is not None

        return await asyncio.to_thread(_check)
