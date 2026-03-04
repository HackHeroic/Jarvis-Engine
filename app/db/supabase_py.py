"""Supabase client connection logic."""

import asyncio
from supabase import Client, create_client

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL


class DatabaseClient:
    """Supabase database client with async connection verification."""

    def __init__(self) -> None:
        self.supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    async def check_connection(self) -> bool:
        """
        Verify the database is reachable by performing a basic query.
        Returns True if successful.
        """
        def _check() -> bool:
            # Simple query to verify connectivity (user_state from PDF schema)
            response = self.supabase.table("user_state").select("*").limit(1).execute()
            return response is not None

        return await asyncio.to_thread(_check)
