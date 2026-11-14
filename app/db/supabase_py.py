"""Supabase client connection logic."""

import asyncio

import httpx
from supabase import Client, ClientOptions, create_client

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL

# PostgREST's own default for one request.
POSTGREST_TIMEOUT_S = 120.0


def build_supabase_client(url: str, key: str) -> Client:
    """A Supabase client whose PostgREST calls run over plain HTTP/1.1 connections.

    supabase-py's default session speaks HTTP/2 over one shared connection. Every
    repository query runs on a worker thread (``asyncio.to_thread``), so concurrent
    queries multiplex onto that connection from several threads, and httpcore's sync
    HTTP/2 does not survive it: one thread's read fails (``ReadError: [Errno 35]``),
    the connection keeps that error and raises it for every other request in flight
    on it, which then reads as an outage (503). Over HTTP/1.1 each concurrent query
    takes its own connection from the pool. Requests carry absolute URLs and their
    own headers (the service key), so the session needs neither a base URL nor them.
    """
    session = httpx.Client(http2=False, timeout=POSTGREST_TIMEOUT_S, follow_redirects=True)
    return create_client(url, key, options=ClientOptions(httpx_client=session))


class DatabaseClient:
    """Supabase database client with async connection verification."""

    def __init__(self) -> None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise ValueError(
                "Supabase not configured: set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env "
                f"(SUPABASE_URL present: {bool(SUPABASE_URL)}, key present: {bool(SUPABASE_SERVICE_KEY)})"
            )
        self.supabase: Client = build_supabase_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

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
