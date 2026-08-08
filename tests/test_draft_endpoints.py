# tests/test_draft_endpoints.py
"""HTTP-level tests for /api/v1/drafts.

These were `xfail(strict=True)` until Task 10: `app/api/v1/endpoints/drafts.py`
still drove the *component* API (`store.get`, `accept_component`,
`reject_component`, `update_component_data`) that commit 3674f6d replaced with
Supabase persistence and 9d3c09f reduced to no-op shims — `get()` was a
hardcoded `return None`, so every read/accept/reject 404'd regardless of what
was seeded, while `jarvis-frontend/lib/api.ts:485-556` called all four.

The endpoints now run on the real DraftStore API, so the markers are gone. The
fixture seeds through `create_draft`, exactly as the planning sub-graph does.
"""

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeDBClient, FakeSupabase

_UNSET = object()

HORIZON = "2026-08-08T08:00:00Z"


@pytest.fixture
def client():
    from app.main import app
    from app.services.draft_store import DraftStore

    original_store = getattr(app.state, "draft_store", _UNSET)
    original_db = getattr(app.state, "db_client", _UNSET)

    # One FakeSupabase behind both the draft store and the db client, so an
    # accept can be observed writing user_tasks in the same place it reads
    # draft_schedules — the real deployment shares one client too.
    supabase = FakeSupabase()
    store = DraftStore(supabase_client=supabase, ttl_seconds=300)
    draft = store.create_draft(
        user_id="test_user",
        tasks=[
            {"task_id": "t1", "title": "Read ch1", "duration_minutes": 25},
            {"task_id": "t2", "title": "Practice problems", "duration_minutes": 20},
        ],
        horizon_start=HORIZON,
    )
    app.state.draft_store = store
    app.state.db_client = FakeDBClient(supabase)
    app.state._test_draft_id = draft["id"]
    app.state._test_supabase = supabase

    yield TestClient(app)

    for name, original in (("draft_store", original_store), ("db_client", original_db)):
        if original is _UNSET:
            delattr(app.state, name)
        else:
            setattr(app.state, name, original)


def _draft_row(client, draft_id: str, user_id: str = "test_user") -> dict | None:
    return client.app.state.draft_store.get_draft(draft_id, user_id)


# ── read ────────────────────────────────────────────────────────────────────


def test_get_draft(client):
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=test_user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["draft_id"] == draft_id
    assert body["status"] == "pending"
    assert [t["task_id"] for t in body["tasks"]] == ["t1", "t2"]
    assert body["horizon_start"] == HORIZON


def test_get_draft_wrong_user(client):
    """A non-owner must never read the draft."""
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=wrong_user")
    assert resp.status_code == 404


def test_get_draft_unknown_id(client):
    resp = client.get("/api/v1/drafts/does-not-exist?user_id=test_user")
    assert resp.status_code == 404


# ── accept ──────────────────────────────────────────────────────────────────


def test_accept_component(client):
    """Legacy name kept for the audit trail — accept is whole-draft now."""
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/accept",
        json={"user_id": "test_user", "components": ["tasks"]},
    )
    assert resp.status_code == 200
    assert resp.json()["draft_id"] == draft_id


def test_accept_all(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(f"/api/v1/drafts/{draft_id}/accept", json={"user_id": "test_user"})
    assert resp.status_code == 200


def test_accept__marks_the_draft_accepted(client):
    draft_id = client.app.state._test_draft_id
    client.post(f"/api/v1/drafts/{draft_id}/accept", json={"user_id": "test_user"})
    assert _draft_row(client, draft_id)["status"] == "accepted"


def test_accept__persists_the_tasks(client):
    """The whole point: accepting is the only moment tasks reach user_tasks."""
    draft_id = client.app.state._test_draft_id
    resp = client.post(f"/api/v1/drafts/{draft_id}/accept", json={"user_id": "test_user"})

    assert resp.json()["task_count"] == 2
    rows = client.app.state._test_supabase.rows.get("user_tasks", [])
    assert {r["task_id"] for r in rows} == {"t1", "t2"}
    assert all(r["user_id"] == "test_user" and r["status"] == "pending" for r in rows)
    # completion_criteria is required on TaskChunk but absent from stored draft
    # tasks — the shared coercer has to fill it or validation would drop them.
    assert all(r["completion_criteria"] for r in rows)


def test_accept_wrong_user(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(f"/api/v1/drafts/{draft_id}/accept", json={"user_id": "wrong_user"})

    assert resp.status_code == 404
    assert client.app.state._test_supabase.rows.get("user_tasks", []) == []
    assert _draft_row(client, draft_id)["status"] == "pending"


# ── reject ──────────────────────────────────────────────────────────────────


def test_reject_component(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/reject",
        json={"user_id": "test_user", "components": ["tasks"]},
    )
    assert resp.status_code == 200
    assert resp.json()["draft_id"] == draft_id


def test_reject__marks_the_draft_rejected_with_reason(client):
    draft_id = client.app.state._test_draft_id
    client.post(
        f"/api/v1/drafts/{draft_id}/reject",
        json={"user_id": "test_user", "components": ["tasks"], "reason": "too heavy"},
    )
    row = _draft_row(client, draft_id)
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == "too heavy"


def test_reject__never_persists_tasks(client):
    draft_id = client.app.state._test_draft_id
    client.post(
        f"/api/v1/drafts/{draft_id}/reject",
        json={"user_id": "test_user", "components": ["tasks"]},
    )
    assert client.app.state._test_supabase.rows.get("user_tasks", []) == []


def test_reject_wrong_user(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/reject",
        json={"user_id": "wrong_user", "components": ["tasks"]},
    )
    assert resp.status_code == 404
    assert _draft_row(client, draft_id)["status"] == "pending"


# ── edit / rearrange / delete ───────────────────────────────────────────────


def test_edit_task(client):
    draft_id = client.app.state._test_draft_id
    resp = client.patch(
        f"/api/v1/drafts/{draft_id}/tasks/t1?user_id=test_user",
        json={"title": "Read ch1 slowly", "duration_minutes": 15},
    )
    assert resp.status_code == 200
    tasks = _draft_row(client, draft_id)["tasks"]
    assert tasks[0]["title"] == "Read ch1 slowly"
    assert tasks[0]["duration_minutes"] == 15


def test_edit_task_wrong_user(client):
    draft_id = client.app.state._test_draft_id
    resp = client.patch(
        f"/api/v1/drafts/{draft_id}/tasks/t1?user_id=wrong_user",
        json={"title": "hijacked"},
    )
    assert resp.status_code == 404
    assert _draft_row(client, draft_id)["tasks"][0]["title"] == "Read ch1"


def test_rearrange(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/rearrange",
        json={"user_id": "test_user", "task_order": ["t2", "t1"]},
    )
    assert resp.status_code == 200
    assert [t["task_id"] for t in _draft_row(client, draft_id)["tasks"]] == ["t2", "t1"]


def test_rearrange_wrong_user(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/rearrange",
        json={"user_id": "wrong_user", "task_order": ["t2", "t1"]},
    )
    assert resp.status_code == 404
    assert [t["task_id"] for t in _draft_row(client, draft_id)["tasks"]] == ["t1", "t2"]


def test_modify_tasks(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/modify",
        json={
            "user_id": "test_user",
            "component": "tasks",
            "data": [{"task_id": "t9", "title": "Only this", "duration_minutes": 10}],
        },
    )
    assert resp.status_code == 200
    assert [t["task_id"] for t in _draft_row(client, draft_id)["tasks"]] == ["t9"]


def test_modify_unknown_component(client):
    """The component model is gone — only the task list is modifiable."""
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/modify",
        json={"user_id": "test_user", "component": "habits", "data": []},
    )
    assert resp.status_code == 400


def test_delete_draft(client):
    draft_id = client.app.state._test_draft_id
    assert client.delete(f"/api/v1/drafts/{draft_id}?user_id=test_user").status_code == 200
    assert _draft_row(client, draft_id) is None


def test_delete_draft_wrong_user(client):
    draft_id = client.app.state._test_draft_id
    assert client.delete(f"/api/v1/drafts/{draft_id}?user_id=wrong_user").status_code == 404
    assert _draft_row(client, draft_id) is not None


def test_endpoints_require_a_draft_store(client):
    """Degraded startup (no Supabase) must answer 503, not crash."""
    from app.main import app

    app.state.draft_store = None
    assert client.get("/api/v1/drafts/whatever?user_id=test_user").status_code == 503


def test_accept__persist_failure__does_not_report_accepted(client, monkeypatch):
    """The endpoint shares accept_draft_and_persist with the orchestrator node,
    so it inherits the same duty: never call an unverified write a success."""
    monkeypatch.setattr(
        "app.services.analytical.control_policy._persist_fused_tasks",
        lambda *a, **k: None,  # what a swallowed exception looks like
    )
    draft_id = client.app.state._test_draft_id
    resp = client.post(f"/api/v1/drafts/{draft_id}/accept", json={"user_id": "test_user"})

    assert resp.status_code == 500
    assert _draft_row(client, draft_id)["status"] == "pending"  # retryable


def test_accept__draft_with_no_tasks__is_not_reported_accepted(client):
    draft_id = client.app.state._test_draft_id
    client.app.state.draft_store.replace_tasks(draft_id, "test_user", [])

    resp = client.post(f"/api/v1/drafts/{draft_id}/accept", json={"user_id": "test_user"})

    assert resp.status_code == 200
    assert resp.json()["status"] != "accepted"
    assert resp.json()["task_count"] == 0
