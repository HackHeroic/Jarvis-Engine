# tests/test_document_pipeline.py
"""Tests for the document intelligence pipeline: classify → dispatch → memory."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.documents.pipeline import document_intelligence_pipeline
from app.services.documents.registry import document_registry, register_default_document_types
from app.schemas.document import DocumentClassification


@pytest.fixture(autouse=True)
def setup_registry():
    register_default_document_types()


@pytest.mark.asyncio
async def test_pipeline_classifies_and_dispatches():
    """Pipeline should classify the document and call the correct handler."""
    mock_memory_store = MagicMock()
    mock_memory_store.store_memory.return_value = {"id": "mem-1"}

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=DocumentClassification(
            document_type="practice_problems",
            confidence=0.9,
            topics_covered=["CNNs", "backpropagation"],
            problem_count=10,
        ),
    ):
        result = await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Problem 1: Explain convolution...\nProblem 2: Derive backprop...",
            source="direct_upload",
            source_id="src-1",
            memory_store=mock_memory_store,
        )

    assert result.document_type == "practice_problems"
    assert result.confidence == 0.9
    # Memory should be stored for the ingestion event
    mock_memory_store.store_memory.assert_called_once()
    stored = mock_memory_store.store_memory.call_args
    assert "practice_problems" in stored[0][1]["content"]


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_reference():
    """Unknown document types should fall back to reference handler."""
    mock_memory_store = MagicMock()
    mock_memory_store.store_memory.return_value = {"id": "mem-1"}

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=DocumentClassification(
            document_type="unknown_type",
            confidence=0.3,
            topics_covered=["misc"],
        ),
    ):
        result = await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Random content...",
            source="direct_upload",
            source_id="src-2",
            memory_store=mock_memory_store,
        )

    # Should fall back to reference (the registry's fallback_key)
    # The pipeline should not crash
    assert result is not None


@pytest.mark.asyncio
async def test_pipeline_stores_memory_on_ingestion():
    """Every document ingestion should create a memory record."""
    mock_memory_store = MagicMock()
    mock_memory_store.store_memory.return_value = {"id": "mem-1"}

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=DocumentClassification(
            document_type="lecture_notes",
            confidence=0.85,
            topics_covered=["neural networks", "deep learning"],
        ),
    ):
        await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Lecture 5: Neural Networks...",
            source="direct_upload",
            source_id="src-3",
            memory_store=mock_memory_store,
        )

    mock_memory_store.store_memory.assert_called_once()
    call_args = mock_memory_store.store_memory.call_args[0]
    assert call_args[0] == "u1"  # user_id
    mem_dict = call_args[1]
    assert mem_dict["type"] == "fact"
    assert "lecture_notes" in mem_dict["content"]
    assert mem_dict["source"] == "ingestion"


@pytest.mark.asyncio
async def test_pipeline_handles_classification_error():
    """If classification fails, pipeline should not crash."""
    mock_memory_store = MagicMock()

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        side_effect=Exception("LLM failed"),
    ):
        result = await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Some content...",
            source="direct_upload",
            source_id="src-4",
            memory_store=mock_memory_store,
        )

    # Should return None or a fallback, not crash
    assert result is None


class TestDocumentClassificationSchema:
    def test_valid_classification(self):
        cls = DocumentClassification(
            document_type="practice_problems",
            confidence=0.9,
            topics_covered=["CNNs"],
            problem_count=5,
        )
        assert cls.document_type == "practice_problems"
        assert cls.confidence == 0.9
        assert cls.problem_count == 5

    def test_defaults(self):
        cls = DocumentClassification(
            document_type="reference",
            confidence=0.5,
        )
        assert cls.topics_covered == []
        assert cls.problem_count is None
        assert cls.deadline_detected is None
        assert cls.difficulty_estimate is None

    def test_confidence_bounds(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DocumentClassification(document_type="x", confidence=1.5)
        with pytest.raises(ValidationError):
            DocumentClassification(document_type="x", confidence=-0.1)
