"""
Integration tests for the draft reject lifecycle.
"""

import sys
import pytest
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


class TestDraftReject:
    @pytest.mark.asyncio
    async def test_draft_reject__stores_reason(self):
        from app.domain.entities import DraftStatus
        from app.domain.events import NullEventBus
        from app.infrastructure.persistence.factory import RepositoryFactory
        from app.services.draft_service import DraftService

        repos = RepositoryFactory.in_memory()
        service = DraftService(repos.drafts, NullEventBus())
        user_id = "test-user-reject"
        draft = await service.propose(
            user_id, tasks=[], horizon_start="2026-03-29T08:00:00Z", goal_id=None, schedule=None
        )

        await service.reject(user_id, draft.id, reason="Tasks too long")

        stored = await repos.drafts.get(user_id, draft.id)
        assert (stored.status, stored.rejection_reason) == (DraftStatus.REJECTED, "Tasks too long")
