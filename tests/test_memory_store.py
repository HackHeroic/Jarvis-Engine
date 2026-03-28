"""Tests for memory store and supporting utilities."""

from app.utils.embedding import cosine_similarity


def test_cosine_similarity_identical_vectors():
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert abs(cosine_similarity(a, b) - 1.0) < 0.001


def test_cosine_similarity_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(cosine_similarity(a, b)) < 0.001


def test_cosine_similarity_opposite_vectors():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(cosine_similarity(a, b) - (-1.0)) < 0.001


def test_cosine_similarity_zero_vector():
    a = [0.0, 0.0]
    b = [1.0, 1.0]
    assert cosine_similarity(a, b) == 0.0


import pytest
from unittest.mock import MagicMock, patch
from app.services.memory.store import MemoryStore


@pytest.fixture
def memory_store(mock_supabase):
    return MemoryStore(supabase_client=mock_supabase)


def test_store_memory(memory_store, mock_supabase):
    mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "mem-1", "user_id": "u1", "content": "User is a CS student"}
    ]
    with patch("app.services.memory.store.embed_text", return_value=[0.1] * 384):
        result = memory_store.store_memory("u1", {
            "type": "fact", "content": "User is a CS student", "confidence": 0.8,
        })
    assert result is not None
    assert result["id"] == "mem-1"
    mock_supabase.table.assert_called_with("user_memories")


def test_get_active_memories(memory_store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.is_.return_value.gt.return_value.execute.return_value.data = [
        {"id": "m1", "memory_type": "fact", "content": "CS student", "confidence": 0.8, "strength": 1.0, "stability": 1.0},
        {"id": "m2", "memory_type": "preference", "content": "Hates mornings", "confidence": 0.7, "strength": 0.9, "stability": 2.0},
    ]
    result = memory_store.get_active_memories("u1")
    assert len(result) == 2


def test_get_memories_by_type(memory_store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.is_.return_value.gt.return_value.execute.return_value.data = [
        {"id": "m1", "memory_type": "constraint", "content": "No work after 6 PM"},
    ]
    result = memory_store.get_memories_by_type("u1", "constraint")
    assert len(result) == 1


def test_update_memory(memory_store, mock_supabase):
    mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
        {"id": "m1", "strength": 1.0, "stability": 2.0}
    ]
    result = memory_store.update_memory("m1", {"strength": 1.0, "stability": 2.0}, user_id="u1")
    assert result is True


def test_supersede_memory(memory_store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "old-1", "user_id": "u1", "memory_type": "preference", "content": "Likes mornings"}
    ]
    mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "new-1", "user_id": "u1", "memory_type": "preference", "content": "Prefers evenings"}
    ]
    with patch("app.services.memory.store.embed_text", return_value=[0.1] * 384):
        new_mem = memory_store.supersede_memory("old-1", "u1", "Prefers evenings")
    assert new_mem is not None


def test_find_similar_memory(memory_store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.is_.return_value.gt.return_value.execute.return_value.data = [
        {"id": "m1", "content": "User is a CS student", "embedding": [0.1] * 384, "memory_type": "fact"},
    ]
    with patch("app.services.memory.store.embed_text", return_value=[0.1] * 384):
        result = memory_store.find_similar_memory("u1", "CS student at VIT", threshold=0.5)
    assert result is not None
