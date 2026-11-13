# tests/test_chat_accept_schedule.py
"""HTTP-level pins for POST /api/v1/chat/accept-schedule.

The frontend still calls this route as the live no-draft fallback
(`jarvis-frontend/lib/api.ts:515`), so it is the only thing standing between an
accepted plan and `user_tasks`. It used to answer `{"status": "accepted"}`
unconditionally — including when there was no Supabase client at all and when
`_persist_fused_tasks` swallowed its own exception — which told the user their
day was saved while nothing had been written.

Same duty as `/api/v1/drafts/{id}/accept` (see test_draft_endpoints.py): never
call an unverified write a success.
"""

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeDBClient, FakeSupabase

_UNSET = object()

TASKS = [
    {
        "task_id": "goal_task_1",
        "title": "Read ch1",
        "duration_minutes": 25,
        "difficulty_weight": 0.5,
        "completion_criteria": "Summarise chapter 1 without notes",
    },
    {
        "task_id": "goal_task_2",
        "title": "Practice problems",
        "duration_minutes": 20,
        "difficulty_weight": 0.6,
        "completion_criteria": "Solve 3 problems unaided",
    },
]


@pytest.fixture
def client():
    from app.main import app

    original_db = getattr(app.state, "db_client", _UNSET)
    original_memory = getattr(app.state, "memory_store", _UNSET)

    supabase = FakeSupabase()
    app.state.db_client = FakeDBClient(supabase)
    # detect_patterns is fire-and-forget on the success path; leaving a real
    # store wired would spawn a background task this test never awaits.
    app.state.memory_store = None
    app.state._test_supabase = supabase

    yield TestClient(app)

    for name, original in (("db_client", original_db), ("memory_store", original_memory)):
        if original is _UNSET:
            delattr(app.state, name)
        else:
            setattr(app.state, name, original)


def _post(client, **overrides):
    body = {"user_id": "test_user", "tasks": TASKS}
    body.update(overrides)
    return client.post("/api/v1/chat/accept-schedule", json=body)


def _rows(client):
    return client.app.state._test_supabase.rows.get("user_tasks", [])


def test_accept_schedule__persists_and_reports_accepted(client):
    resp = _post(client)

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert resp.json()["task_count"] == 2
    assert {r["task_id"] for r in _rows(client)} == {"goal_task_1", "goal_task_2"}
    assert all(r["user_id"] == "test_user" and r["status"] == "pending" for r in _rows(client))


def test_accept_schedule__write_failed__does_not_report_accepted(client, monkeypatch):
    """A swallowed persistence exception reads as `None` from the helper."""
    monkeypatch.setattr(
        "app.services.analytical.control_policy._persist_fused_tasks",
        lambda *a, **k: None,
    )

    resp = _post(client)

    assert resp.status_code == 503
    assert resp.json()["status"] == "failed"
    assert resp.json()["detail"] == "schedule could not be persisted"
    assert _rows(client) == []


def test_accept_schedule__no_supabase__does_not_report_accepted(client):
    """Degraded startup: nothing to persist through, so nothing was accepted."""
    from app.main import app

    app.state.db_client = None

    resp = _post(client)

    assert resp.status_code == 503
    assert resp.json()["status"] == "failed"


def test_accept_schedule__write_did_not_land__does_not_report_accepted(client, monkeypatch):
    """A plan_id is not proof. The rows carrying it have to be readable back.

    `_persist_fused_tasks` mints the plan_id before it writes and swallows
    everything after, so a returned id with no rows behind it is exactly what a
    half-failed insert looks like.
    """
    monkeypatch.setattr(
        "app.services.analytical.control_policy._persist_fused_tasks",
        lambda *a, **k: "plan-that-wrote-nothing",
    )

    resp = _post(client)

    assert resp.status_code == 503
    assert resp.json()["status"] == "failed"
    assert _rows(client) == []


def test_accept_schedule__invalid_tasks__still_reports_the_parse_error(client):
    """Unchanged behaviour: bad input is a task-shape problem, not an outage."""
    resp = _post(client, tasks=[{"task_id": "t1"}])

    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert resp.json()["task_count"] == 0
    assert _rows(client) == []


@pytest.mark.parametrize("path", ["/api/v1/chat/accept-schedule", "/api/v1/chat/confirm-schedule"])
def test_v1_adjacent_schedule_routes_are_marked_deprecated(client, path):
    """Both are v1-adjacent surfaces slated for retirement; OpenAPI must say so
    so the frontend's generated client warns instead of silently depending."""
    spec = client.get("/openapi.json").json()
    assert spec["paths"][path]["post"]["deprecated"] is True


# --- the unique key on user_tasks (user_id, task_id) ---------------------------
# `_persist_fused_tasks` deletes the user's pending rows and then inserts. If the
# insert then fails on the unique key, the pending rows are already gone. So it
# refuses before deleting anything when an incoming id is taken or repeated.

_STILL_PENDING = {
    "user_id": "test_user", "task_id": "other_goal_task_1", "title": "keep me",
    "status": "pending", "duration_minutes": 25,
}


def test_accept_schedule__id_held_by_completed_task__refuses_and_deletes_nothing(client):
    finished = {**_STILL_PENDING, "task_id": "goal_task_1", "status": "completed"}
    client.app.state._test_supabase.rows["user_tasks"] = [finished, dict(_STILL_PENDING)]

    resp = _post(client)

    assert resp.status_code == 503
    assert resp.json()["status"] == "failed"
    assert _rows(client) == [finished, _STILL_PENDING]


def test_accept_schedule__duplicate_ids_in_payload__refuses_and_deletes_nothing(client):
    client.app.state._test_supabase.rows["user_tasks"] = [dict(_STILL_PENDING)]

    resp = _post(client, tasks=[TASKS[0], {**TASKS[1], "task_id": TASKS[0]["task_id"]}])

    assert resp.status_code == 503
    assert resp.json()["status"] == "failed"
    assert _rows(client) == [_STILL_PENDING]


def test_accept_schedule__client_sends_goal_id__row_keeps_it(client):
    """A v2 plan accepted through this fallback: `learn_dsa_…` must not be filed under `learn`."""
    tasks = [{**t, "task_id": f"learn_dsa_ab12cd_task_{n}", "goal_id": "learn_dsa"}
             for n, t in enumerate(TASKS, start=1)]

    resp = _post(client, tasks=tasks)

    assert resp.status_code == 200
    assert {r["goal_id"] for r in _rows(client)} == {"learn_dsa"}
