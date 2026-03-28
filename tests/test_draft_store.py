import pytest
from unittest.mock import MagicMock
from app.services.draft_store import DraftStore
from app.schemas.draft import DraftTask


@pytest.fixture
def mock_supabase():
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table
    execute_result = MagicMock()
    execute_result.data = [{"id": "test-draft-id"}]
    table.insert.return_value.execute.return_value = execute_result
    table.select.return_value.eq.return_value.eq.return_value.execute.return_value = execute_result
    table.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = execute_result
    table.update.return_value.eq.return_value.eq.return_value.execute.return_value = execute_result
    table.delete.return_value.eq.return_value.eq.return_value.execute.return_value = execute_result
    return client


@pytest.fixture
def store(mock_supabase):
    return DraftStore(supabase_client=mock_supabase)


def test_create_draft(store, mock_supabase):
    tasks = [
        DraftTask(
            task_id="t1", title="Study CNNs", start_min=480,
            duration_minutes=25, difficulty_weight=0.5,
            completion_criteria="Explain convolution operation",
        )
    ]
    draft = store.create_draft(user_id="user-1", tasks=tasks, horizon_start="2026-03-29T08:00:00Z")
    assert draft is not None
    mock_supabase.table.assert_called_with("draft_schedules")


def test_get_draft(store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "d1", "user_id": "user-1", "tasks": [], "horizon_start": "2026-03-29T08:00:00Z", "status": "pending"}
    ]
    result = store.get_draft("d1", "user-1")
    assert result is not None
    assert result["status"] == "pending"


def test_accept_draft(store):
    result = store.accept_draft("d1", "user-1")
    assert result is True


def test_reject_draft(store):
    result = store.reject_draft("d1", "user-1", reason="Too cramped")
    assert result is True
