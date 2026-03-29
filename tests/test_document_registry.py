# tests/test_document_registry.py
"""Tests for the document type registry."""

import pytest
from app.services.documents.registry import document_registry, register_default_document_types


def test_default_document_types_registered():
    register_default_document_types()
    names = document_registry.registered_names()
    assert "practice_problems" in names
    assert "lecture_notes" in names
    assert "syllabus" in names
    assert "assignment" in names
    assert "reference" in names
    assert len(names) == 5


def test_reference_is_fallback():
    register_default_document_types()
    result = document_registry.get_or_fallback("unknown_doc_type")
    assert result.name == "reference"


def test_classification_prompt_generated():
    register_default_document_types()
    prompt = document_registry.classification_prompt()
    assert "practice_problems" in prompt
    assert "lecture_notes" in prompt
    assert "reference" in prompt
    assert "Problem sets" in prompt or "exercises" in prompt


def test_practice_problems_has_correct_metadata():
    register_default_document_types()
    entry = document_registry.get("practice_problems")
    assert entry is not None
    assert entry.metadata.get("modifies_tasks") is True
    assert entry.metadata.get("triggers_replan") is True


def test_lecture_notes_does_not_modify_tasks():
    register_default_document_types()
    entry = document_registry.get("lecture_notes")
    assert entry is not None
    assert entry.metadata.get("modifies_tasks") is False
    assert entry.metadata.get("triggers_replan") is False


def test_reference_does_not_modify_tasks():
    register_default_document_types()
    entry = document_registry.get("reference")
    assert entry is not None
    assert entry.metadata.get("modifies_tasks") is False


def test_all_handlers_are_callable():
    register_default_document_types()
    for name in document_registry.registered_names():
        entry = document_registry.get(name)
        assert callable(entry.handler), f"Handler for {name} is not callable"


def test_all_entries_have_examples():
    register_default_document_types()
    for name in document_registry.registered_names():
        entry = document_registry.get(name)
        assert len(entry.examples) > 0, f"No examples for {name}"
