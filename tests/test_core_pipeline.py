"""Integration tests for the core pipeline: brain dump → schedule → draft."""

import pytest
from unittest.mock import MagicMock
from app.core.registry import BaseRegistry, RegistryEntry
from app.services.intent_registry import intent_registry, register_default_intents


class TestIntentClassification:
    """Test that the intent registry generates valid classification prompts."""

    def setup_method(self):
        register_default_intents()

    def test_classification_prompt_is_valid(self):
        prompt = intent_registry.classification_prompt()
        assert len(prompt) > 100
        assert "PLAN_DAY" in prompt
        assert "CHAT" in prompt

    def test_all_intents_have_handlers(self):
        for name in intent_registry.registered_names():
            entry = intent_registry.get(name)
            assert entry is not None, f"Missing entry for {name}"
            assert callable(entry.handler), f"Handler for {name} is not callable"

    def test_all_intents_have_examples(self):
        for name in intent_registry.registered_names():
            entry = intent_registry.get(name)
            assert len(entry.examples) > 0, f"No examples for {name}"

    def test_fallback_to_chat(self):
        result = intent_registry.get_or_fallback("BANANA")
        assert result.name == "CHAT"


class TestDraftStore:
    """Test DraftStore with mock Supabase."""

    def test_create_and_get_draft(self, mock_supabase):
        from app.services.draft_store import DraftStore
        from app.schemas.draft import DraftTask

        mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
            {"id": "d1", "user_id": "u1", "status": "pending"}
        ]

        store = DraftStore(supabase_client=mock_supabase)
        tasks = [DraftTask(
            task_id="t1", title="Test", start_min=0,
            duration_minutes=25, difficulty_weight=0.5,
            completion_criteria="Done",
        )]
        result = store.create_draft("u1", tasks, "2026-03-29T08:00:00Z")
        assert result is not None

    def test_edit_task_in_draft(self, mock_supabase):
        from app.services.draft_store import DraftStore

        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {
                "id": "d1", "user_id": "u1",
                "tasks": [
                    {"task_id": "t1", "title": "Original", "start_min": 0,
                     "duration_minutes": 25, "difficulty_weight": 0.5,
                     "completion_criteria": "Done"},
                ],
                "status": "pending",
            }
        ]

        store = DraftStore(supabase_client=mock_supabase)
        result = store.edit_task_in_draft("d1", "u1", "t1", {"title": "Edited"})
        assert result is not None

    def test_reject_draft(self, mock_supabase):
        from app.services.draft_store import DraftStore

        store = DraftStore(supabase_client=mock_supabase)
        result = store.reject_draft("d1", "u1", reason="Too much work")
        assert result is True


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
