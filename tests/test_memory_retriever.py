# tests/test_memory_retriever.py
"""Tests for memory scoring, retrieval, and context formatting."""

import math
from datetime import datetime, timezone, timedelta

from app.services.memory.retriever import (
    compute_memory_strength,
    score_memory,
    format_memory_block,
    IMPORTANCE_WEIGHTS,
)


def _make_memory(
    memory_type="fact",
    content="Test memory",
    confidence=0.8,
    strength=1.0,
    stability=1.0,
    last_reinforced=None,
    superseded_by=None,
    embedding=None,
):
    """Helper to create a memory-like dict for testing."""
    return {
        "id": "test-id",
        "user_id": "u1",
        "memory_type": memory_type,
        "content": content,
        "confidence": confidence,
        "strength": strength,
        "stability": stability,
        "last_reinforced": (last_reinforced or datetime.now(timezone.utc)).isoformat(),
        "superseded_by": superseded_by,
        "embedding": embedding or [0.1] * 10,
    }


class TestComputeMemoryStrength:
    def test_fresh_memory_has_full_strength(self):
        """A memory just reinforced should have strength close to 1.0."""
        mem = _make_memory(last_reinforced=datetime.now(timezone.utc))
        strength = compute_memory_strength(mem, datetime.now(timezone.utc))
        assert strength > 0.99

    def test_memory_decays_over_time(self):
        """A memory from a week ago with stability=1 should be significantly decayed."""
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        mem = _make_memory(last_reinforced=one_week_ago, stability=1.0)
        strength = compute_memory_strength(mem, datetime.now(timezone.utc))
        # e^(-1) ≈ 0.368
        assert 0.3 < strength < 0.4

    def test_higher_stability_slows_decay(self):
        """A memory with stability=5 should decay much slower than stability=1."""
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        mem_low = _make_memory(last_reinforced=one_week_ago, stability=1.0)
        mem_high = _make_memory(last_reinforced=one_week_ago, stability=5.0)
        now = datetime.now(timezone.utc)
        strength_low = compute_memory_strength(mem_low, now)
        strength_high = compute_memory_strength(mem_high, now)
        assert strength_high > strength_low
        # stability=5, 1 week: e^(-1/5) ≈ 0.819
        assert strength_high > 0.8

    def test_very_old_memory_approaches_zero(self):
        """A memory from 3 months ago with stability=1 should be near zero."""
        three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)
        mem = _make_memory(last_reinforced=three_months_ago, stability=1.0)
        strength = compute_memory_strength(mem, datetime.now(timezone.utc))
        assert strength < 0.01


class TestScoreMemory:
    def test_score_combines_all_factors(self):
        """Score should be relevance × recency × importance × confidence."""
        mem = _make_memory(
            memory_type="constraint",
            confidence=0.9,
            last_reinforced=datetime.now(timezone.utc),
        )
        query_emb = [0.1] * 10
        mem_emb = [0.1] * 10  # Identical = relevance 1.0
        score = score_memory(mem, query_emb, mem_emb, datetime.now(timezone.utc))
        # relevance=1.0, recency≈1.0, importance=1.0 (constraint), confidence=0.9
        assert score > 0.85

    def test_constraint_scores_higher_than_fact(self):
        """Constraints should score higher than facts, all else equal."""
        now = datetime.now(timezone.utc)
        query_emb = [0.1] * 10
        mem_emb = [0.1] * 10

        constraint = _make_memory(memory_type="constraint", last_reinforced=now)
        fact = _make_memory(memory_type="fact", last_reinforced=now)

        score_c = score_memory(constraint, query_emb, mem_emb, now)
        score_f = score_memory(fact, query_emb, mem_emb, now)
        assert score_c > score_f


class TestFormatMemoryBlock:
    def test_empty_memories_returns_empty_string(self):
        assert format_memory_block([]) == ""

    def test_formats_by_type(self):
        memories = [
            {"memory_type": "constraint", "content": "No work after 6 PM"},
            {"memory_type": "fact", "content": "CS student at VIT"},
            {"memory_type": "goal", "content": "Finish DSA by April"},
        ]
        result = format_memory_block(memories)
        assert "Scheduling Constraints" in result
        assert "No work after 6 PM" in result
        assert "Facts" in result
        assert "CS student at VIT" in result
        assert "Active Goals" in result

    def test_groups_same_type(self):
        memories = [
            {"memory_type": "fact", "content": "Fact 1"},
            {"memory_type": "fact", "content": "Fact 2"},
        ]
        result = format_memory_block(memories)
        assert result.count("Facts") == 1  # Only one header
        assert "Fact 1" in result
        assert "Fact 2" in result


class TestBuildMemoryContext:
    def test_build_memory_context__returns_formatted_block(self):
        """build_memory_context fetches active memories, scores, returns formatted string."""
        from app.services.memory.retriever import build_memory_context

        class MockStore:
            def get_active_memories(self, user_id):
                return [
                    {
                        "id": "m1", "user_id": "u1", "memory_type": "constraint",
                        "content": "No tasks between 2 PM and 3 PM",
                        "confidence": 0.9, "strength": 1.0, "stability": 2.0,
                        "last_reinforced": datetime.now(timezone.utc).isoformat(),
                    },
                    {
                        "id": "m2", "user_id": "u1", "memory_type": "goal",
                        "content": "Finish DSA by April",
                        "confidence": 0.8, "strength": 1.0, "stability": 1.0,
                        "last_reinforced": datetime.now(timezone.utc).isoformat(),
                    },
                    {
                        "id": "m3", "user_id": "u1", "memory_type": "fact",
                        "content": "CS student at VIT",
                        "confidence": 0.7, "strength": 0.5, "stability": 1.0,
                        "last_reinforced": datetime.now(timezone.utc).isoformat(),
                    },
                ]

        result = build_memory_context("u1", MockStore())
        assert "No tasks between 2 PM and 3 PM" in result
        assert "Finish DSA by April" in result
        assert "CS student at VIT" in result

    def test_build_memory_context__empty_memories(self):
        """Returns empty string when no memories."""
        from app.services.memory.retriever import build_memory_context

        class EmptyStore:
            def get_active_memories(self, user_id):
                return []

        result = build_memory_context("u1", EmptyStore())
        assert result == ""
