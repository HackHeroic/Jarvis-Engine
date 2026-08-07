# tests/test_draft_endpoints.py
"""HTTP-level tests for /api/v1/drafts.

KNOWN BROKEN — the endpoints, not these tests. `app/api/v1/endpoints/drafts.py`
still drives the *component* API (`store.get`, `accept_component`,
`reject_component`, `update_component_data`). Commit 3674f6d migrated
DraftStore to Supabase persistence and 9d3c09f reduced those methods to no-op
shims — `get()` is a hardcoded `return None`. So every read/accept/reject 404s
regardless of what is seeded, and `modify` returns 200 while writing nothing.
`jarvis-frontend/lib/api.ts:485-556` still calls all four.

The fixture below seeds through the store API that *does* work, so these tests
describe the contract any correct implementation must meet. They are
`xfail(strict=True)`: once drafts.py is rewritten against the real API they
will XPASS, which fails the suite and forces these markers to be removed.
Assertions are deliberately implementation-agnostic (status code + draft id)
so they do not presume the shape of the replacement response body.
"""

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeSupabase

_BROKEN = pytest.mark.xfail(
    strict=True,
    reason="drafts.py still calls the removed component API; store.get() always returns None",
)

_UNSET = object()


@pytest.fixture
def client():
    from app.main import app
    from app.services.draft_store import DraftStore

    original = getattr(app.state, "draft_store", _UNSET)

    store = DraftStore(supabase_client=FakeSupabase(), ttl_seconds=300)
    draft = store.create_draft(
        user_id="test_user",
        tasks=[{"task_id": "t1", "title": "Read ch1", "duration_minutes": 25}],
        horizon_start="2026-08-08T08:00:00Z",
    )
    app.state.draft_store = store
    app.state._test_draft_id = draft["id"]

    yield TestClient(app)

    if original is _UNSET:
        del app.state.draft_store
    else:
        app.state.draft_store = original


@_BROKEN
def test_get_draft(client):
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=test_user")
    assert resp.status_code == 200
    assert resp.json()["draft_id"] == draft_id


def test_get_draft_wrong_user(client):
    """A non-owner must never read the draft.

    NOTE: this currently passes vacuously — the endpoint 404s for *everyone*
    (see module docstring). It stays un-xfailed because 404 is the correct
    answer here and must remain so after drafts.py is fixed.
    """
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=wrong_user")
    assert resp.status_code == 404


@_BROKEN
def test_accept_component(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/accept",
        json={"user_id": "test_user", "components": ["tasks"]},
    )
    assert resp.status_code == 200
    assert resp.json()["draft_id"] == draft_id


@_BROKEN
def test_reject_component(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/reject",
        json={"user_id": "test_user", "components": ["tasks"]},
    )
    assert resp.status_code == 200
    assert resp.json()["draft_id"] == draft_id


@_BROKEN
def test_accept_all(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/accept",
        json={"user_id": "test_user"},
    )
    assert resp.status_code == 200
