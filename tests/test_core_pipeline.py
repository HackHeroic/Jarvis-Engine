"""Integration tests for the core pipeline: brain dump → schedule → draft."""

import pytest
from unittest.mock import MagicMock
from app.core.registry import BaseRegistry, RegistryEntry


class TestDraftService:
    """Test DraftService over the in-memory repositories."""

    @staticmethod
    def _service():
        from app.domain.events import NullEventBus
        from app.infrastructure.persistence.factory import RepositoryFactory
        from app.services.draft_service import DraftService

        return DraftService(RepositoryFactory.in_memory().drafts, NullEventBus())

    @pytest.mark.asyncio
    async def test_create_and_get_draft(self):
        from app.schemas.draft import DraftTask

        service = self._service()
        tasks = [DraftTask(
            task_id="t1", title="Test", start_min=0,
            duration_minutes=25, difficulty_weight=0.5,
            completion_criteria="Done",
        ).model_dump()]
        result = await service.propose(
            "u1", tasks=tasks, horizon_start="2026-03-29T08:00:00Z", goal_id=None, schedule=None
        )
        assert result is not None
        assert (await service.get("u1", result.id)).tasks == tasks

    @pytest.mark.asyncio
    async def test_edit_task_in_draft(self):
        service = self._service()
        draft = await service.propose(
            "u1",
            tasks=[
                {"task_id": "t1", "title": "Original", "start_min": 0,
                 "duration_minutes": 25, "difficulty_weight": 0.5,
                 "completion_criteria": "Done"},
            ],
            horizon_start="2026-03-29T08:00:00Z", goal_id=None, schedule=None,
        )

        result = await service.edit_task("u1", draft.id, "t1", {"title": "Edited"})
        assert result is not None
        assert result.tasks[0]["title"] == "Edited"

    @pytest.mark.asyncio
    async def test_reject_draft(self):
        from app.domain.entities import DraftStatus

        service = self._service()
        draft = await service.propose(
            "u1", tasks=[], horizon_start="2026-03-29T08:00:00Z", goal_id=None, schedule=None
        )
        result = await service.reject("u1", draft.id, reason="Too much work")
        assert result.status is DraftStatus.REJECTED


class TestTimeSlotSchema:
    """Test that TimeSlot now has source field."""

    def test_timeslot_has_source_default(self):
        from app.schemas.context import TimeSlot
        slot = TimeSlot(name="test", start_min=0, end_min=60, availability="blocked")
        assert slot.source == "user"

    def test_timeslot_accepts_pearl_source(self):
        from app.schemas.context import TimeSlot
        slot = TimeSlot(
            name="inferred", start_min=0, end_min=60,
            availability="minimal_work", source="pearl_inferred"
        )
        assert slot.source == "pearl_inferred"
