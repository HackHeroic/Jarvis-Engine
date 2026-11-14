"""
Integration tests for the draft reject lifecycle.
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
