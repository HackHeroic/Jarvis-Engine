# tests/test_pearl_integration.py
"""Integration tests for PEARL → memory → constraint bridge pipeline."""

import pytest
from unittest.mock import MagicMock
from app.services.memory.pearl import (
    pearl_registry,
    register_default_patterns,
    detect_patterns,
    generate_proactive_insights,
)
from app.services.memory.constraint_bridge import memories_to_constraints


class TestPearlToConstraintBridge:
    """End-to-end: PEARL detects pattern → memory stored → constraint bridge produces TimeSlot."""

    def test_skip_pattern_becomes_soft_block(self):
        """Detected skip pattern should flow through to a minimal_work TimeSlot."""
        # Step 1: Set up tasks with clear skip pattern at hour 8
        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"task_id": f"t{i}", "status": "skipped", "scheduled_hour": 8}
            for i in range(4)
        ] + [
            {"task_id": "t5", "status": "completed", "scheduled_hour": 14},
        ]

        # Step 2: Memory store that stores the pattern
        stored_memories = []
        mock_memory_store = MagicMock()
        mock_memory_store.find_similar_memory.return_value = None
        mock_memory_store.store_memory.side_effect = lambda uid, mem: stored_memories.append(mem) or {"id": "new-1"}

        # Step 3: Detect patterns
        register_default_patterns()
        detected = detect_patterns("u1", mock_supabase, mock_memory_store)
        assert len(detected) >= 1

        # Step 4: Verify memory was stored
        assert len(stored_memories) >= 1
        pattern_mem = stored_memories[0]
        assert pattern_mem["type"] == "behavioral_pattern"
        assert "hour 8" in pattern_mem["content"]

        # Step 5: Constraint bridge should convert this to a TimeSlot
        # Set up memory_store to return the pattern we just created
        mock_bridge_store = MagicMock()
        mock_bridge_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: (
            [{"id": "p1", "content": pattern_mem["content"], "confidence": 0.85}]
            if mtype == "behavioral_pattern" else []
        )

        slots = memories_to_constraints("u1", mock_bridge_store)
        pattern_slots = [s for s in slots if s.source == "pearl_inferred"]
        assert len(pattern_slots) >= 1
        assert pattern_slots[0].start_min == 480  # 8 * 60
        assert pattern_slots[0].end_min == 540    # 9 * 60
        assert pattern_slots[0].availability == "minimal_work"


class TestPearlRegistryExtensibility:
    def test_add_custom_pattern_detector(self):
        """Adding a new pattern detector via registry should work."""
        from app.core.registry import RegistryEntry

        def detect_weekend_preference(user_id, tasks, memory_store, **kwargs):
            return [{"pattern": "weekend_preference", "inference": "User works on weekends"}]

        register_default_patterns()
        pearl_registry.register(RegistryEntry(
            name="weekend_preference",
            description="User works on weekends",
            handler=detect_weekend_preference,
            metadata={"min_observations": 3, "min_rate": 0.7, "constraint_type": "soft_preference"},
        ))

        assert "weekend_preference" in pearl_registry.registered_names()

    def test_detect_patterns_runs_all_registered(self):
        """detect_patterns should run ALL registered detectors."""
        register_default_patterns()

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []

        mock_memory_store = MagicMock()

        # Should not crash even with empty tasks
        result = detect_patterns("u1", mock_supabase, mock_memory_store)
        assert isinstance(result, list)


class TestProactiveInsightsIntegration:
    def test_insight_message_is_user_friendly(self):
        """Insights should be natural language, not technical."""
        mock_memory_store = MagicMock()
        mock_memory_store.get_memories_by_type.return_value = [
            {
                "id": "p1",
                "content": "User avoids tasks during hour 9 (skipped 85%)",
                "confidence": 0.85,
                "observation_count": 7,
            },
        ]

        insights = generate_proactive_insights("u1", mock_memory_store)
        assert len(insights) == 1
        # Should mention a time, not "hour 9"
        assert "9 AM" in insights[0]
        assert "skip" in insights[0].lower() or "avoid" in insights[0].lower()

    def test_productive_pattern_insight(self):
        """Productive time detection should generate positive insight."""
        mock_memory_store = MagicMock()
        mock_memory_store.get_memories_by_type.return_value = [
            {
                "id": "p2",
                "content": "User is most productive during hour 14 (completed 90%)",
                "confidence": 0.9,
                "observation_count": 10,
            },
        ]

        insights = generate_proactive_insights("u1", mock_memory_store)
        assert len(insights) == 1
        assert "2 PM" in insights[0]
        assert "productive" in insights[0].lower()
