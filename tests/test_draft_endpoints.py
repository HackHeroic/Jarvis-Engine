# tests/test_draft_endpoints.py
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client with draft store."""
    from app.main import app
    from app.services.draft_store import DraftStore, Draft, DraftComponent

    # Save original state for cleanup
    original_draft_store = getattr(app.state, "draft_store", None)

    # Seed a draft for testing
    store = DraftStore(ttl_seconds=300)
    draft = store.create("test_user")
    store.add_component(
        draft.draft_id, "test_user", "habits",
        DraftComponent(component_type="habits", data=[{"raw_text": "no work before 11 AM"}], status="pending"),
    )
    store.add_component(
        draft.draft_id, "test_user", "tasks",
        DraftComponent(component_type="tasks", data=[{"task_id": "t1", "title": "Read ch1"}], status="pending"),
    )
    app.state.draft_store = store
    app.state._test_draft_id = draft.draft_id

    yield TestClient(app)

    # Restore original state
    if original_draft_store is not None:
        app.state.draft_store = original_draft_store


def test_get_draft(client):
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=test_user")
    assert resp.status_code == 200
    data = resp.json()
    assert data["draft_id"] == draft_id
    assert "habits" in data["components"]
    assert data["components"]["habits"]["status"] == "pending"


def test_get_draft_wrong_user(client):
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=wrong_user")
    assert resp.status_code == 404


def test_accept_component(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/accept",
        json={"user_id": "test_user", "components": ["habits"]},
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == ["habits"]


def test_reject_component(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/reject",
        json={"user_id": "test_user", "components": ["tasks"]},
    )
    assert resp.status_code == 200
    assert resp.json()["rejected"] == ["tasks"]


def test_accept_all(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/accept",
        json={"user_id": "test_user"},
    )
    assert resp.status_code == 200
