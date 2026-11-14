"""Routes that read or write a user's rows are scoped to the caller.

``user_id`` is required (the ``CurrentUser`` auth seam) and every query filters
by it, so another user's row is indistinguishable from a missing one: 404, and
the row is left untouched. A database failure is a 503 envelope, never an empty
list and never the exception's text.
"""

import pytest
from fastapi.testclient import TestClient

from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

_UNSET = object()

_SLOT = {"name": "OS lecture", "start_min": 60, "end_min": 120, "availability": "blocked"}
# habit_trackers.id is a uuid: an id of another form could never name a tracker.
_ALICES_TRACKER = "7d0c0f3e-5b1a-4c2e-9f00-00000000a11c"


def _seed() -> dict[str, list[dict]]:
    return {
        "pending_calendar_updates": [
            {"id": "pc-alice", "user_id": "alice", "status": "pending", "extracted_slots": [_SLOT]},
            {"id": "pc-bob", "user_id": "bob", "status": "pending", "extracted_slots": []},
        ],
        "habit_trackers": [
            {"id": _ALICES_TRACKER, "user_id": "alice", "repetitions": 0, "ef": 2.5,
             "next_interval_days": 1},
        ],
    }


class _RaisingSupabase:
    """A client whose every call fails the way a dropped connection does."""

    def table(self, name: str):
        raise RuntimeError("connection refused: postgres://admin:hunter2@db.internal")


def _container_over(client):
    """The composition root over ``client``, as the lifespan builds it."""
    import app.core.config as cfg
    from app.container import AppContainer
    from app.orchestrator.hooks import ActionHooks

    return AppContainer.build(
        supabase_client=client, cfg=cfg, hooks=ActionHooks(),
        embedder=BagOfWordsEmbedder(), install_chain=False,
    )


@pytest.fixture
def app_state(monkeypatch):
    from app.main import app

    original = getattr(app.state, "db_client", _UNSET)
    supabase = FakeSupabase(_seed())
    app.state.db_client = FakeDBClient(supabase)
    # The composition root over the same database: the routes read and write through it.
    monkeypatch.setattr(app.state, "container", _container_over(supabase), raising=False)
    yield app, supabase
    if original is _UNSET:
        delattr(app.state, "db_client")
    else:
        app.state.db_client = original


@pytest.fixture
def client(app_state) -> TestClient:
    app, _ = app_state
    return TestClient(app, raise_server_exceptions=False)


def _row(supabase: FakeSupabase, table: str, row_id: str) -> dict:
    return next(r for r in supabase.rows[table] if r["id"] == row_id)


# ── pending calendar ────────────────────────────────────────


def test_pending_calendar_list__no_user_id__422(client):
    resp = client.get("/api/v1/ingestion/pending-calendar")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_pending_calendar_list__as_alice__only_alice_rows(client):
    resp = client.get("/api/v1/ingestion/pending-calendar", params={"user_id": "alice"})

    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()["pending"]] == ["pc-alice"]


def test_pending_calendar_approve__owner_approves__200_and_row_approved(client, app_state):
    _, supabase = app_state

    resp = client.post(
        "/api/v1/ingestion/pending-calendar/pc-alice/approve", params={"user_id": "alice"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert [slot["name"] for slot in body["daily_context"]] == ["OS lecture"]
    assert _row(supabase, "pending_calendar_updates", "pc-alice")["status"] == "approved"


def test_pending_calendar_approve__bob_approves_alice_row__404_and_row_still_pending(
    client, app_state
):
    _, supabase = app_state

    resp = client.post(
        "/api/v1/ingestion/pending-calendar/pc-alice/approve", params={"user_id": "bob"}
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert _row(supabase, "pending_calendar_updates", "pc-alice")["status"] == "pending"


def test_pending_calendar_reject__bob_rejects_alice_row__404_and_row_still_pending(
    client, app_state
):
    _, supabase = app_state

    resp = client.post(
        "/api/v1/ingestion/pending-calendar/pc-alice/reject", params={"user_id": "bob"}
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert _row(supabase, "pending_calendar_updates", "pc-alice")["status"] == "pending"


def test_pending_calendar_list__db_unavailable__503_envelope(client, app_state):
    app, _ = app_state
    app.state.db_client.supabase = None

    resp = client.get("/api/v1/ingestion/pending-calendar", params={"user_id": "alice"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "persistence_unavailable"


def test_pending_calendar_approve__db_raises__503_without_exception_text(client, app_state):
    app, _ = app_state
    app.state.db_client.supabase = _RaisingSupabase()

    resp = client.post(
        "/api/v1/ingestion/pending-calendar/pc-alice/approve", params={"user_id": "alice"}
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "persistence_unavailable"
    assert "hunter2" not in resp.text
    assert "connection refused" not in resp.text


# ── habit tracker ───────────────────────────────────────────


def test_tracker_complete__no_user_id__422(client):
    resp = client.post(f"/api/v1/habits/tracker/{_ALICES_TRACKER}/complete", json={"quality": 4})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_tracker_complete__wrong_user__404(client, app_state):
    _, supabase = app_state

    resp = client.post(
        f"/api/v1/habits/tracker/{_ALICES_TRACKER}/complete",
        params={"user_id": "bob"},
        json={"quality": 4},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert _row(supabase, "habit_trackers", _ALICES_TRACKER)["repetitions"] == 0


@pytest.mark.parametrize("path", ["/api/v1/habits/tracker/due", "/api/v1/habits/constraints"])
@pytest.mark.parametrize("outage", ["no_client", "client_raises"])
def test_habits_lists__db_unavailable__503_not_an_empty_list(
    client, app_state, path, outage, monkeypatch
):
    app, _ = app_state
    if outage == "no_client":
        app.state.db_client.supabase = None
    else:  # the database drops: the client the repositories hold fails every call
        app.state.db_client.supabase = _RaisingSupabase()
        monkeypatch.setattr(app.state, "container", _container_over(_RaisingSupabase()))

    resp = client.get(path, params={"user_id": "alice"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "persistence_unavailable"
    assert "hunter2" not in resp.text


def test_tracker_due__as_alice__only_alices_trackers(client):
    resp = client.get("/api/v1/habits/tracker/due", params={"user_id": "alice"})

    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()["due_trackers"]] == [_ALICES_TRACKER]


# ── task workspace ──────────────────────────────────────────


def test_task_workspace__builder_raises__500_without_exception_text(client, monkeypatch):
    async def _explode(**_kwargs):
        raise RuntimeError("connection refused: postgres://admin:hunter2@db.internal")

    monkeypatch.setattr(
        "app.api.v1.endpoints.workspace.get_or_build_task_workspace", _explode
    )

    resp = client.get("/api/v1/tasks/t1/workspace", params={"user_id": "alice"})

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in resp.text
    assert "connection refused" not in resp.text
