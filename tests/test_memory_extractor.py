# tests/test_memory_extractor.py
"""Tests for LLM-based memory extraction from conversation turns."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.memory.extractor import (
    extract_memories_from_turn,
    safe_extract_memories,
    EXTRACTION_PROMPT,
)


def test_extraction_prompt_has_placeholders():
    """The extraction prompt should have the right format placeholders."""
    assert "{existing_memories}" in EXTRACTION_PROMPT
    assert "{user_message}" in EXTRACTION_PROMPT
    assert "{assistant_response}" in EXTRACTION_PROMPT


@pytest.mark.asyncio
async def test_extract_memories_returns_list():
    """Extraction should return a list of extracted memories."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []
    mock_store.find_similar_memory.return_value = None
    mock_store.store_memory.return_value = {"id": "new-1"}

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": [
            {"type": "fact", "content": "User is a CS student", "confidence": 0.8, "contradicts": None, "expires_at": None}
        ]},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="I'm a CS student at VIT",
            assistant_response="Great! I'll keep that in mind.",
            memory_store=mock_store,
        )

    assert len(result) == 1
    assert result[0]["type"] == "fact"
    mock_store.store_memory.assert_called_once()


@pytest.mark.asyncio
async def test_extract_handles_contradiction():
    """When LLM marks a contradiction, supersede_memory should be called."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = [
        {"id": "old-1", "memory_type": "preference", "content": "User likes mornings"}
    ]
    mock_store.supersede_memory.return_value = {"id": "new-1"}

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": [
            {"type": "preference", "content": "User prefers evenings", "confidence": 0.7, "contradicts": "old-1", "expires_at": None}
        ]},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="Actually I work best in the evenings",
            assistant_response="Noted — I'll adjust your schedule.",
            memory_store=mock_store,
        )

    assert len(result) == 1
    mock_store.supersede_memory.assert_called_once_with("old-1", "u1", "User prefers evenings")


@pytest.mark.asyncio
async def test_extract_handles_duplicate():
    """When a similar memory already exists, reinforce instead of creating new."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []
    mock_store.find_similar_memory.return_value = {"id": "existing-1"}
    mock_store.reinforce_memory.return_value = True

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": [
            {"type": "fact", "content": "User is a CS student", "confidence": 0.8, "contradicts": None, "expires_at": None}
        ]},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="As a CS student, I need...",
            assistant_response="Sure!",
            memory_store=mock_store,
        )

    mock_store.reinforce_memory.assert_called_once_with("existing-1")
    mock_store.store_memory.assert_not_called()


@pytest.mark.asyncio
async def test_extract_returns_empty_when_nothing_new():
    """When LLM finds nothing new, return empty list."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": []},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="Hello",
            assistant_response="Hi there!",
            memory_store=mock_store,
        )

    assert result == []
    mock_store.store_memory.assert_not_called()


@pytest.mark.asyncio
async def test_safe_extract_catches_errors():
    """safe_extract_memories should never raise — fire and forget."""
    mock_store = MagicMock()
    mock_store.get_active_memories.side_effect = Exception("DB down")

    # Should NOT raise
    await safe_extract_memories(
        user_id="u1",
        user_message="test",
        assistant_response="test",
        memory_store=mock_store,
    )
