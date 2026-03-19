import time
import pytest
from app.services.draft_store import DraftStore, Draft, DraftComponent


def test_create_and_get_draft():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    assert draft.draft_id
    assert draft.user_id == "user_123"
    assert draft.components == {}

    retrieved = store.get(draft.draft_id, "user_123")
    assert retrieved is not None
    assert retrieved.draft_id == draft.draft_id


def test_get_draft_wrong_user():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    assert store.get(draft.draft_id, "user_456") is None


def test_add_component():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    store.add_component(
        draft.draft_id,
        "user_123",
        "habits",
        DraftComponent(
            component_type="habits",
            data=[{"raw_text": "no work before 11 AM"}],
            status="pending",
        ),
    )
    updated = store.get(draft.draft_id, "user_123")
    assert "habits" in updated.components
    assert updated.components["habits"].status == "pending"


def test_accept_component():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    store.add_component(
        draft.draft_id,
        "user_123",
        "habits",
        DraftComponent(component_type="habits", data=[], status="pending"),
    )
    store.accept_component(draft.draft_id, "user_123", "habits")
    updated = store.get(draft.draft_id, "user_123")
    assert updated.components["habits"].status == "accepted"


def test_reject_component():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    store.add_component(
        draft.draft_id,
        "user_123",
        "habits",
        DraftComponent(component_type="habits", data=[], status="pending"),
    )
    store.reject_component(draft.draft_id, "user_123", "habits")
    updated = store.get(draft.draft_id, "user_123")
    assert updated.components["habits"].status == "rejected"


def test_draft_expires():
    store = DraftStore(ttl_seconds=0)  # Immediate expiry
    draft = store.create("user_123")
    time.sleep(0.01)
    assert store.get(draft.draft_id, "user_123") is None


def test_cleanup_expired():
    store = DraftStore(ttl_seconds=0)
    store.create("user_123")
    store.create("user_123")
    time.sleep(0.01)
    removed = store.cleanup_expired()
    assert removed >= 2
