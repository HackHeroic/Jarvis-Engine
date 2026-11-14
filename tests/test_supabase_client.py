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
import pytest

from app.db.supabase_py import build_supabase_client


def _pool(client: httpx.Client):
    # httpcore's connection pool, under the one-retry wrapper around the HTTP transport
    return client._transport.inner._pool


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


def test_one_way_to_build_a_client__nothing_else_calls_create_client():
    """The app, the seed and the live check all build their client with
    ``build_supabase_client``: a second way would bring the shared HTTP/2 connection back."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    callers = []
    for path in sorted([*(root / "app").rglob("*.py"), *(root / "scripts").rglob("*.py")]):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "create_client":
                    callers.append(str(path.relative_to(root)))

    assert callers == ["app/db/supabase_py.py"]


# --- a brief network blip: one retry when the connection could not be made ----------------


class _Network(httpx.BaseTransport):
    """The network under the retry: each call answers or raises the next scripted outcome."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json=[], request=request)


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF in violation of protocol")


def _retrying(network: _Network, slept: list[float]):
    from app.db.supabase_py import ConnectRetryTransport

    return httpx.Client(transport=ConnectRetryTransport(network, sleep=slept.append))


@pytest.mark.parametrize("first_failure", [
    _connect_error(), httpx.ConnectTimeout("timed out while connecting"),
])
def test_connect_retry__first_connect_fails__retried_once_after_a_short_jittered_pause(
    first_failure,
):
    network, slept = _Network(first_failure, 200), []

    response = _retrying(network, slept).get("https://example.supabase.co/rest/v1/user_tasks")

    assert response.status_code == 200
    assert network.calls == 2
    (pause,) = slept
    assert 0.05 <= pause <= 0.5  # short, and random so a crowd of retries spreads out


def test_connect_retry__connect_fails_twice__the_second_error_is_raised():
    network, slept = _Network(_connect_error(), _connect_error()), []

    with pytest.raises(httpx.ConnectError):
        _retrying(network, slept).get("https://example.supabase.co/rest/v1/user_tasks")

    assert network.calls == 2 and len(slept) == 1  # one retry, bounded


@pytest.mark.parametrize("outcome", [
    httpx.ReadTimeout("the request was sent; no answer yet"),
    httpx.RemoteProtocolError("server disconnected"),
    503,
])
def test_connect_retry__request_may_have_been_sent__never_retried(outcome):
    """A read timeout, a dropped connection or an HTTP error may follow a request that
    reached the server: retrying a write then could apply it twice."""
    network, slept = _Network(outcome, 200), []
    client = _retrying(network, slept)

    if isinstance(outcome, Exception):
        with pytest.raises(type(outcome)):
            client.post("https://example.supabase.co/rest/v1/user_tasks", json={"a": 1})
    else:
        assert client.post(
            "https://example.supabase.co/rest/v1/user_tasks", json={"a": 1}
        ).status_code == 503

    assert network.calls == 1 and slept == []


def test_build_supabase_client__one_failed_connect__the_query_still_answers(monkeypatch):
    import app.db.supabase_py as supabase_py

    monkeypatch.setattr(supabase_py.time, "sleep", lambda _s: None)
    network = _Network(_connect_error(), 200)
    supabase = build_supabase_client("https://example.supabase.co", "service-role-key",
                                     network=network)

    result = supabase.table("user_tasks").select("task_id").eq("user_id", "u1").execute()

    assert result.data == []
    assert network.calls == 2


@pytest.mark.asyncio
async def test_health__one_failed_connect__healthy_not_503(monkeypatch):
    """A blip of one failed connection does not flap /health."""
    from app.db.supabase_py import DatabaseClient
    import app.db.supabase_py as supabase_py
    from app.main import app

    monkeypatch.setattr(supabase_py.time, "sleep", lambda _s: None)
    db_client = DatabaseClient.__new__(DatabaseClient)
    db_client.supabase = build_supabase_client(
        "https://example.supabase.co", "service-role-key", network=_Network(_connect_error(), 200)
    )
    monkeypatch.setattr(app.state, "db_client", db_client, raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "database": "connected"}


def test_build_supabase_client__pooled_keep_alive_bounded_and_a_short_connect_timeout():
    from app.db.supabase_py import ConnectRetryTransport

    session = build_supabase_client("https://example.supabase.co", "service-role-key").postgrest.session

    assert isinstance(session._transport, ConnectRetryTransport)
    pool = _pool(session)
    assert pool._max_connections == 32 and pool._max_keepalive_connections == 16
    assert pool._keepalive_expiry == 20.0
    assert pool._retries == 0  # the one retry is ConnectRetryTransport's, never doubled
    assert session.timeout.connect == 10.0 and session.timeout.read == 120.0
