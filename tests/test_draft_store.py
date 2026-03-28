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
