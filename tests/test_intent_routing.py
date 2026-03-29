import pytest
from app.services.intent_registry import intent_registry, register_default_intents


def test_default_intents_registered():
    register_default_intents()
    names = intent_registry.registered_names()
    assert "PLAN_DAY" in names
    assert "EDIT_TASK" in names
    assert "CHAT" in names
    assert "ACCEPT_DRAFT" in names
    assert "REJECT_DRAFT" in names
    assert "INGEST_DOCUMENT" in names
    assert "CHECK_PROGRESS" in names
    assert "ADD_CONSTRAINT" in names
    assert "REARRANGE" in names


def test_chat_is_fallback():
    register_default_intents()
    result = intent_registry.get_or_fallback("NONEXISTENT_INTENT")
    assert result.name == "CHAT"


def test_classification_prompt_generated():
    register_default_intents()
    prompt = intent_registry.classification_prompt()
    assert "PLAN_DAY" in prompt
    assert "CHAT" in prompt


def test_plan_day_has_handler():
    register_default_intents()
    entry = intent_registry.get("PLAN_DAY")
    assert entry is not None
    assert callable(entry.handler)
    assert entry.metadata.get("triggers_replan") is False


def test_edit_task_triggers_replan():
    register_default_intents()
    entry = intent_registry.get("EDIT_TASK")
    assert entry is not None
    assert entry.metadata.get("triggers_replan") is True
