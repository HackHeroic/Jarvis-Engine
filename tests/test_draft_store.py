import pytest
from unittest.mock import MagicMock
from app.services.draft_store import DraftStore
from app.schemas.draft import DraftTask


def test_create_draft(mock_supabase):
    mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [{"id": "test-draft-id"}]
    store = DraftStore(supabase_client=mock_supabase)
    tasks = [DraftTask(task_id="t1", title="Study CNNs", start_min=480, duration_minutes=25, difficulty_weight=0.5, completion_criteria="Explain convolution operation")]
    draft = store.create_draft(user_id="user-1", tasks=tasks, horizon_start="2026-03-29T08:00:00Z")
    assert draft is not None
    mock_supabase.table.assert_called_with("draft_schedules")


def test_get_draft(mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "d1", "user_id": "user-1", "tasks": [], "horizon_start": "2026-03-29T08:00:00Z", "status": "pending"}
    ]
    store = DraftStore(supabase_client=mock_supabase)
    result = store.get_draft("d1", "user-1")
    assert result is not None
    assert result["status"] == "pending"


def test_accept_draft(mock_supabase):
    mock_supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"id": "d1", "status": "accepted"}]
    store = DraftStore(supabase_client=mock_supabase)
    result = store.accept_draft("d1", "user-1")
    assert result is True


def test_reject_draft(mock_supabase):
    mock_supabase.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"id": "d1", "status": "rejected"}]
    store = DraftStore(supabase_client=mock_supabase)
    result = store.reject_draft("d1", "user-1", reason="Too cramped")
    assert result is True


# ── Legacy alias call-pattern tests ──────────────────────
# These test the exact positional args used by drafts.py and control_policy.py
# to prevent TypeError crashes at runtime.

def test_legacy_get_with_user_id(mock_supabase):
    """drafts.py:36 calls store.get(draft_id, user_id)"""
    store = DraftStore(supabase_client=mock_supabase)
    result = store.get("draft-id", "user-id")
    assert result is None  # no-op returns None


def test_legacy_add_component_with_four_args(mock_supabase):
    """control_policy.py:897 calls draft_store.add_component(draft_id, user_id, key, DraftComponent)"""
    from app.services.draft_store import DraftComponent
    store = DraftStore(supabase_client=mock_supabase)
    comp = DraftComponent(component_type="tasks", data=[], status="pending")
    result = store.add_component("draft-id", "user-id", "tasks", comp)
    assert result is True


def test_legacy_accept_component_with_three_args(mock_supabase):
    """drafts.py:115 calls store.accept_component(draft_id, user_id, key)"""
    store = DraftStore(supabase_client=mock_supabase)
    result = store.accept_component("draft-id", "user-id", "tasks")
    assert result is True


def test_legacy_reject_component_with_three_args(mock_supabase):
    """drafts.py:136 calls store.reject_component(draft_id, user_id, key)"""
    store = DraftStore(supabase_client=mock_supabase)
    result = store.reject_component("draft-id", "user-id", "tasks")
    assert result is True


def test_legacy_update_component_data_with_four_args(mock_supabase):
    """drafts.py:151 calls store.update_component_data(draft_id, user_id, component, data)"""
    store = DraftStore(supabase_client=mock_supabase)
    result = store.update_component_data("draft-id", "user-id", "tasks", {"key": "val"})
    assert result is True


def test_legacy_create_returns_draft_with_id(mock_supabase):
    """control_policy.py:1230 calls draft = draft_store.create(user_id, metadata={...}) then draft.draft_id"""
    store = DraftStore(supabase_client=mock_supabase)
    draft = store.create("user-id", metadata={"goal": "test"})
    assert hasattr(draft, "draft_id")
    assert draft.draft_id is not None
    assert draft.user_id == "user-id"


def test_legacy_delete_with_user_id(mock_supabase):
    """drafts.py:166 calls store.delete(draft_id, user_id)"""
    store = DraftStore(supabase_client=mock_supabase)
    result = store.delete("draft-id", "user-id")
    assert result is True
