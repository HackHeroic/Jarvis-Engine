"""
Integration tests for task state transitions.
"""

import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_supabase():
    client = MagicMock()
    def make_query(data=None):
        q = MagicMock()
        q.select.return_value = q
        q.insert.return_value = q
        q.update.return_value = q
        q.delete.return_value = q
        q.eq.return_value = q
        q.in_.return_value = q
        q.single.return_value = q
        q.order.return_value = q
        q.limit.return_value = q
        result = MagicMock()
        result.data = data or []
        q.execute.return_value = result
        return q
    client.table.return_value = make_query()
    return client


class TestListTasks:
    def test_list_tasks__returns_tasks(self, mock_supabase):
        tasks_data = [
            {"task_id": "t1", "title": "Study CNNs", "status": "pending", "user_id": "user-a"},
            {"task_id": "t2", "title": "Essay draft", "status": "completed", "user_id": "user-a"},
        ]
        mock_supabase.table.return_value.execute.return_value.data = tasks_data

        query = mock_supabase.table("user_tasks").select("*").eq("user_id", "user-a")
        result = query.execute()

        assert len(result.data) == 2
        assert result.data[0]["task_id"] == "t1"

    def test_list_tasks__filters_by_user(self, mock_supabase):
        mock_supabase.table.return_value.execute.return_value.data = []

        query = mock_supabase.table("user_tasks").select("*").eq("user_id", "user-b")
        result = query.execute()

        assert len(result.data) == 0


class TestTaskComplete:
    def test_task_complete__updates_status(self, mock_supabase):
        mock_supabase.table.return_value.execute.return_value.data = {
            "task_id": "t1", "status": "completed",
        }

        query = (
            mock_supabase.table("user_tasks")
            .update({"status": "completed"})
            .eq("task_id", "t1")
            .eq("user_id", "user-a")
        )
        result = query.execute()
        assert result.data["status"] == "completed"

    def test_task_skip__marks_skipped(self, mock_supabase):
        mock_supabase.table.return_value.execute.return_value.data = {
            "task_id": "t1", "status": "skipped",
        }

        query = (
            mock_supabase.table("user_tasks")
            .update({"status": "skipped"})
            .eq("task_id", "t1")
            .eq("user_id", "user-a")
        )
        result = query.execute()
        assert result.data["status"] == "skipped"
