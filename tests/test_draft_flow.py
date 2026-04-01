"""
Integration tests for the draft accept/reject lifecycle.
"""

import sys
import pytest
import uuid
from unittest.mock import MagicMock

# ── Pre-import mocks for modules that crash/are missing in test env ─────────
# Each top-level package needs its own MagicMock so sub-attribute access works.
_unavailable_packages = {
    "ortools": ["ortools", "ortools.sat", "ortools.sat.python", "ortools.sat.python.cp_model"],
    "docling": [
        "docling", "docling.document_converter", "docling.datamodel",
        "docling.datamodel.base_models", "docling.datamodel.pipeline_options",
    ],
    "docling_core": ["docling_core", "docling_core.types", "docling_core.types.doc"],
}
for _pkg, _submodules in _unavailable_packages.items():
    _pkg_mock = MagicMock()
    for _mod in _submodules:
        sys.modules[_mod] = _pkg_mock

# Ensure supabase.create_client is available
if "supabase" not in sys.modules or not hasattr(sys.modules.get("supabase"), "create_client"):
    _sb_mock = MagicMock()
    _sb_mock.create_client = MagicMock()
    sys.modules["supabase"] = _sb_mock


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


@pytest.fixture
def sample_draft_tasks():
    return [
        {
            "task_id": f"task-{uuid.uuid4().hex[:8]}",
            "title": "Study CNNs - convolution layers",
            "duration_minutes": 25,
            "difficulty_weight": 0.6,
            "dependencies": [],
            "completion_criteria": "Understand convolution operation",
        },
        {
            "task_id": f"task-{uuid.uuid4().hex[:8]}",
            "title": "Practice backpropagation math",
            "duration_minutes": 20,
            "difficulty_weight": 0.7,
            "dependencies": [],
            "completion_criteria": "Solve 3 gradient descent problems",
        },
    ]


@pytest.fixture
def sample_schedule(sample_draft_tasks):
    return {
        sample_draft_tasks[0]["task_id"]: {
            "start_min": 120, "end_min": 145, "tmt_score": 22.0,
            "title": sample_draft_tasks[0]["title"],
        },
        sample_draft_tasks[1]["task_id"]: {
            "start_min": 150, "end_min": 170, "tmt_score": 18.0,
            "title": sample_draft_tasks[1]["title"],
        },
    }


class TestDraftAccept:
    def test_draft_accept__persists_tasks(
        self, mock_supabase, sample_draft_tasks, sample_schedule
    ):
        from app.services.analytical.control_policy import _persist_fused_tasks

        user_id = "test-user-accept"
        horizon_start = "2026-04-01T08:00:00"

        _persist_fused_tasks(
            user_id=user_id,
            chunks=sample_draft_tasks,
            supabase_client=mock_supabase,
            schedule=sample_schedule,
            horizon_start=horizon_start,
        )

        # Verify table was accessed for user_tasks
        mock_supabase.table.assert_any_call("user_tasks")

    def test_draft_accept__empty_chunks__no_insert(self, mock_supabase):
        from app.services.analytical.control_policy import _persist_fused_tasks

        _persist_fused_tasks(
            user_id="test-user",
            chunks=[],
            supabase_client=mock_supabase,
            schedule={},
            horizon_start="2026-04-01T08:00:00",
        )

        insert_calls = mock_supabase.table.return_value.insert.call_args_list
        assert len(insert_calls) == 0


class TestDraftReject:
    def test_draft_reject__stores_reason(self, mock_supabase):
        from app.services.draft_store import DraftStore

        store = DraftStore(mock_supabase)
        draft_id = str(uuid.uuid4())
        user_id = "test-user-reject"

        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = {
            "id": draft_id,
            "user_id": user_id,
            "status": "pending",
            "tasks": [],
        }

        result = store.reject_draft(draft_id, user_id, reason="Tasks too long")
        mock_supabase.table.assert_any_call("draft_schedules")
