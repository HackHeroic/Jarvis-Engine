"""Supabase client connection logic."""

import asyncio
import logging
import random
import time
from collections.abc import Callable

import httpx
from supabase import Client, ClientOptions, create_client

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

# PostgREST's own default for one request (reading and writing).
POSTGREST_TIMEOUT_S = 120.0
# Establishing a connection (TCP and TLS) takes well under a second when the database is
# reachable; past this the attempt is a ConnectTimeout, which is retried once.
CONNECT_TIMEOUT_S = 10.0
# Kept-alive HTTP/1.1 connections, one per concurrent query. The default thread pool
# that runs the queries (asyncio.to_thread) has at most 32 workers, so 32 connections
# never queue a query; 16 stay open between bursts (each new one costs a TLS handshake),
# and an idle one is closed after 20 s, before the server's own idle cut-off.
POOL_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=20.0)
# The pause before the one retry: short, and random so many queries that failed together
# do not all reconnect at the same instant.
CONNECT_RETRY_PAUSE_S = (0.1, 0.4)


class ConnectRetryTransport(httpx.BaseTransport):
    """Sends a request again, once, when it provably did no work on the server.

    ``ConnectError`` (TCP or TLS handshake failed: the ``[SSL…]`` errors of a network
    blip) and ``ConnectTimeout``: the request was never sent, so a retry cannot apply a
    write twice. Also a dropped connection (``RemoteProtocolError``: the server closed a
    kept-alive connection this side was about to reuse) when the request is a read
    (``GET``/``HEAD``): sending a read twice changes nothing. A dropped connection on a
    write, a read timeout or an HTTP error may follow a request the server received and
    applied, and is raised as it came. One retry after a short jittered pause; a second
    failure is raised (the caller's PersistenceError, 503).
    """

    _READ_METHODS = frozenset({"GET", "HEAD"})

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        pause_s: tuple[float, float] = CONNECT_RETRY_PAUSE_S,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.inner = inner
        self._pause_s = pause_s
        # Resolved at call time by default, so a test can stub time.sleep.
        self._sleep = sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return self.inner.handle_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            if (
                isinstance(exc, httpx.RemoteProtocolError)
                and request.method.upper() not in self._READ_METHODS
            ):
                raise
            pause = random.uniform(*self._pause_s)
            logger.warning(
                "Database connection could not be made (%s); retrying once in %.2f s",
                type(exc).__name__, pause,
            )
            (self._sleep or time.sleep)(pause)
            return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def build_supabase_client(
    url: str, key: str, *, network: httpx.BaseTransport | None = None
) -> Client:
    """A Supabase client whose PostgREST calls run over pooled HTTP/1.1 connections,
    retried once when a connection cannot be made (``ConnectRetryTransport``).

    supabase-py's default session speaks HTTP/2 over one shared connection. Every
    repository query runs on a worker thread (``asyncio.to_thread``), so concurrent
    queries multiplex onto that connection from several threads, and httpcore's sync
    HTTP/2 does not survive it: one thread's read fails (``ReadError: [Errno 35]``),
    the connection keeps that error and raises it for every other request in flight
    on it, which then reads as an outage (503). Over HTTP/1.1 each concurrent query
    takes its own kept-alive connection from the pool (``POOL_LIMITS``). Requests carry
    absolute URLs and their own headers (the service key), so the session needs neither
    a base URL nor them. Every repository and ``/health`` share this one client, so a
    brief blip (one failed connect) reaches none of them.

    ``network`` replaces the HTTP transport under the retry (tests only).
    """
    inner = network or httpx.HTTPTransport(http1=True, http2=False, limits=POOL_LIMITS)
    session = httpx.Client(
        transport=ConnectRetryTransport(inner),
        timeout=httpx.Timeout(POSTGREST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        follow_redirects=True,
    )
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
