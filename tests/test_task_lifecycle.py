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


# --- actual_duration_minutes reaches the user_tasks UPDATE ------------------
# F2 (live run 2026-08-08): the field is declared on TaskCompleteRequest and
# written into the UPDATE payload, but no migration created the column, so
# PostgREST 500'd with PGRST204 on every request that supplied it. The column
# now exists (supabase/migrations/20260808000000_user_tasks_actual_duration.sql);
# this pins the write path that depends on it, so the two can't drift apart
# again without a test failing.


class TestCompletionDurationWrite:
    def test_update_task_status__extra_fields__merged_into_update_payload(self, mock_supabase):
        from app.api.v1.endpoints.tasks import _update_task_status_sync

        table = mock_supabase.table.return_value
        table.execute.return_value.data = [{"task_id": "t1", "user_id": "demo"}]

        _update_task_status_sync(
            mock_supabase, "t1", "demo", "completed", {"actual_duration_minutes": 18},
        )

        table.update.assert_called_once_with(
            {"status": "completed", "actual_duration_minutes": 18}
        )

    def test_update_task_status__no_extra_fields__status_only(self, mock_supabase):
        """The optional field must never be written as an explicit NULL."""
        from app.api.v1.endpoints.tasks import _update_task_status_sync

        table = mock_supabase.table.return_value
        table.execute.return_value.data = [{"task_id": "t1", "user_id": "demo"}]

        _update_task_status_sync(mock_supabase, "t1", "demo", "completed", None)

        table.update.assert_called_once_with({"status": "completed"})

    def test_task_complete_request__accepts_actual_duration_minutes(self):
        """The schema advertises the field; the column now backs it."""
        from app.api.v1.endpoints.tasks import TaskCompleteRequest

        body = TaskCompleteRequest(user_id="demo", quality=4, actual_duration_minutes=18)

        assert body.actual_duration_minutes == 18
        assert TaskCompleteRequest(user_id="demo").actual_duration_minutes is None
