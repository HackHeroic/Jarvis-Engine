"""The Supabase client every repository shares: plain HTTP/1.1 connections.

supabase-py's default PostgREST session speaks HTTP/2 over one connection. The
repositories run each query on a worker thread (``asyncio.to_thread``), so concurrent
queries multiplex onto that one connection from several threads, and httpcore's sync
HTTP/2 does not survive it: one thread's socket read fails (``ReadError: [Errno 35]
Resource temporarily unavailable``), the connection stores that error and raises it
for every other stream on it, and each of those requests answered 503 "Could not
reach the database." Two You page loads at once were enough. Over HTTP/1.1 each
concurrent query gets its own pooled connection. Built offline: no request is sent.
"""

import httpx

from app.db.supabase_py import build_supabase_client


def _pool(client: httpx.Client):
    return client._transport._pool  # httpcore's connection pool behind the client


def test_build_supabase_client__postgrest_session__http1_only_and_one_shared_pool():
    supabase = build_supabase_client("https://example.supabase.co", "service-role-key")

    session = supabase.postgrest.session
    assert isinstance(session, httpx.Client)
    assert _pool(session)._http2 is False and _pool(session)._http1 is True
    assert supabase.options.httpx_client is session  # every sub-client shares the one pool


def test_build_supabase_client__requests_keep_the_service_key_and_absolute_urls():
    supabase = build_supabase_client("https://example.supabase.co", "service-role-key")

    request = supabase.table("user_tasks").select("task_id").eq("user_id", "u1").request
    assert str(request.path).startswith("https://example.supabase.co/rest/v1/user_tasks")
    assert request.headers["apikey"] == "service-role-key"
    assert request.headers["authorization"] == "Bearer service-role-key"
