import pytest
from app.core.model_router import ModelRole, MODEL_ROUTING


def test_routing_table_completeness():
    expected_tasks = [
        "socratic_chunker", "habit_translation", "document_understanding",
        "research_summarization", "intent_classification", "brain_dump_extraction",
        "memory_extraction", "voice_of_jarvis", "calendar_parsing",
        "goal_validation", "web_search", "real_time_research",
    ]
    for task in expected_tasks:
        assert task in MODEL_ROUTING, f"Missing routing for {task}"


def test_primary_tasks_use_26b():
    assert MODEL_ROUTING["socratic_chunker"] == ModelRole.PRIMARY
    assert MODEL_ROUTING["habit_translation"] == ModelRole.PRIMARY


def test_fast_tasks_use_e4b():
    assert MODEL_ROUTING["intent_classification"] == ModelRole.FAST
    assert MODEL_ROUTING["voice_of_jarvis"] == ModelRole.FAST


def test_cloud_tasks_use_gemini():
    assert MODEL_ROUTING["web_search"] == ModelRole.CLOUD
    assert MODEL_ROUTING["real_time_research"] == ModelRole.CLOUD
