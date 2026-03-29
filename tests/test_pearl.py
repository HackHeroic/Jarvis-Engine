# tests/test_pearl.py
"""Tests for PEARL behavioral pattern detection."""

import pytest
from unittest.mock import MagicMock
from app.services.memory.pearl import (
    pearl_registry,
    register_default_patterns,
    detect_patterns,
    generate_proactive_insights,
    MIN_OBSERVATIONS,
    MIN_PATTERN_RATE,
)


class TestPearlRegistry:
    def setup_method(self):
        register_default_patterns()

    def test_default_patterns_registered(self):
        names = pearl_registry.registered_names()
        assert "skip_time_window" in names
        assert "completion_time_preference" in names
        assert len(names) >= 2

    def test_all_patterns_have_handlers(self):
        for name in pearl_registry.registered_names():
            entry = pearl_registry.get(name)
            assert callable(entry.handler), f"Handler for {name} not callable"

    def test_all_patterns_have_metadata(self):
        for name in pearl_registry.registered_names():
            entry = pearl_registry.get(name)
            assert "min_observations" in entry.metadata
            assert "min_rate" in entry.metadata
            assert "constraint_type" in entry.metadata


class TestPatternThresholds:
    def test_min_observations_is_3(self):
        assert MIN_OBSERVATIONS == 3

    def test_min_pattern_rate_is_70_percent(self):
        assert MIN_PATTERN_RATE == 0.7


class TestDetectPatterns:
    def test_detects_skip_time_pattern(self):
        """When user skips 3+ tasks at hour 8 with 70%+ rate, detect pattern."""
        mock_supabase = MagicMock()
        # Simulate: user has 5 tasks at hour 8, skipped 4 of them
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"task_id": "t1", "status": "skipped", "scheduled_hour": 8},
            {"task_id": "t2", "status": "skipped", "scheduled_hour": 8},
            {"task_id": "t3", "status": "skipped", "scheduled_hour": 8},
            {"task_id": "t4", "status": "completed", "scheduled_hour": 8},
            {"task_id": "t5", "status": "skipped", "scheduled_hour": 8},
            {"task_id": "t6", "status": "completed", "scheduled_hour": 10},
            {"task_id": "t7", "status": "completed", "scheduled_hour": 14},
        ]

        mock_memory_store = MagicMock()
        mock_memory_store.find_similar_memory.return_value = None
        mock_memory_store.store_memory.return_value = {"id": "pattern-1"}

        register_default_patterns()
        detected = detect_patterns("u1", mock_supabase, mock_memory_store)

        assert len(detected) >= 1
        skip_patterns = [d for d in detected if d["pattern"] == "skip_time_window"]
        assert len(skip_patterns) >= 1
        assert skip_patterns[0]["hour"] == 8

    def test_no_pattern_below_threshold(self):
        """When skip rate is below 70%, no pattern should be detected."""
        mock_supabase = MagicMock()
        # 4 tasks at hour 9, only 1 skipped (25% rate)
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"task_id": "t1", "status": "completed", "scheduled_hour": 9},
            {"task_id": "t2", "status": "completed", "scheduled_hour": 9},
            {"task_id": "t3", "status": "completed", "scheduled_hour": 9},
            {"task_id": "t4", "status": "skipped", "scheduled_hour": 9},
        ]

        mock_memory_store = MagicMock()
        register_default_patterns()
        detected = detect_patterns("u1", mock_supabase, mock_memory_store)

        skip_patterns = [d for d in detected if d["pattern"] == "skip_time_window"]
        assert len(skip_patterns) == 0

    def test_no_pattern_below_min_observations(self):
        """When fewer than 3 observations, no pattern even at 100% rate."""
        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"task_id": "t1", "status": "skipped", "scheduled_hour": 7},
            {"task_id": "t2", "status": "skipped", "scheduled_hour": 7},
        ]

        mock_memory_store = MagicMock()
        register_default_patterns()
        detected = detect_patterns("u1", mock_supabase, mock_memory_store)

        skip_patterns = [d for d in detected if d["pattern"] == "skip_time_window"]
        assert len(skip_patterns) == 0

    def test_reinforces_existing_pattern(self):
        """When pattern already exists in memory, reinforce instead of creating new."""
        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"task_id": "t1", "status": "skipped", "scheduled_hour": 8},
            {"task_id": "t2", "status": "skipped", "scheduled_hour": 8},
            {"task_id": "t3", "status": "skipped", "scheduled_hour": 8},
        ]

        mock_memory_store = MagicMock()
        mock_memory_store.find_similar_memory.return_value = {"id": "existing-pattern"}
        mock_memory_store.reinforce_memory.return_value = True

        register_default_patterns()
        detect_patterns("u1", mock_supabase, mock_memory_store)

        mock_memory_store.reinforce_memory.assert_called()
        mock_memory_store.store_memory.assert_not_called()


class TestProactiveInsights:
    def test_generates_insight_for_skip_pattern(self):
        """Should produce a user-friendly message for detected patterns."""
        mock_memory_store = MagicMock()
        mock_memory_store.get_memories_by_type.return_value = [
            {
                "id": "p1",
                "content": "User avoids tasks during hour 8 (skipped 80%)",
                "confidence": 0.85,
                "observation_count": 5,
            },
        ]

        insights = generate_proactive_insights("u1", mock_memory_store)

        assert len(insights) >= 1
        assert "morning" in insights[0].lower() or "8" in insights[0] or "avoid" in insights[0].lower()

    def test_no_insights_without_patterns(self):
        mock_memory_store = MagicMock()
        mock_memory_store.get_memories_by_type.return_value = []

        insights = generate_proactive_insights("u1", mock_memory_store)
        assert insights == []

    def test_only_high_confidence_patterns_surface(self):
        """Patterns below 0.6 confidence should not generate insights."""
        mock_memory_store = MagicMock()
        mock_memory_store.get_memories_by_type.return_value = [
            {
                "id": "p1",
                "content": "User avoids tasks during hour 8",
                "confidence": 0.4,
                "observation_count": 2,
            },
        ]

        insights = generate_proactive_insights("u1", mock_memory_store)
        assert insights == []
