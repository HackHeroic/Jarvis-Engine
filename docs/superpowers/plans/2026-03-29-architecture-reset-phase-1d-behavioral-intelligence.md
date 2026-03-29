# Architecture Reset Phase 1D: Behavioral Intelligence (PEARL)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Jarvis learn from user behavior — detect recurring patterns (skipping morning tasks, preferring shorter durations, extending deadlines) and automatically apply them as soft scheduling constraints. After Phase 1D, the system adapts without the user explicitly stating preferences.

**Architecture:** PEARL-lite (rule-based, no ML). Pattern detectors query `user_tasks` for behavioral signals (skips, completions, edits). When a pattern exceeds the threshold (3+ observations, 70%+ rate), it's stored as a `behavioral_pattern` memory in `user_memories`. The existing constraint bridge (Phase 1B) already converts these memories into OR-Tools `TimeSlot` constraints. Phase 1D adds the detection layer and wires it into task lifecycle events. A PEARL Pattern Registry (using BaseRegistry) makes it extensible — new pattern detectors are added via registration.

**Tech Stack:** FastAPI, Pydantic v2, BaseRegistry, Supabase (PostgreSQL), MemoryStore (Phase 1B)

**Spec:** `docs/superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md` (section: PEARL Behavioral Pattern Detection)

**Prerequisite:** Phase 1A (BaseRegistry) + Phase 1B (MemoryStore, constraint_bridge)

**Produces:** Working PEARL pattern detection that observes task skips/completions, detects recurring patterns, stores them as memories, and surfaces insights. The existing constraint bridge (Phase 1B) automatically converts these patterns into scheduling constraints.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/services/memory/pearl.py` | PEARL pattern detection + pattern registry + proactive surfacing |
| Create | `tests/test_pearl.py` | Unit tests for pattern detection, registry, surfacing |
| Create | `tests/test_pearl_integration.py` | Integration tests for full PEARL → memory → constraint flow |

---

### Task 1: PEARL Pattern Detection + Registry

**Files:**
- Create: `app/services/memory/pearl.py`
- Create: `tests/test_pearl.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_pearl.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement PEARL pattern detection**

```python
# app/services/memory/pearl.py
"""
PEARL-lite: Pattern detection from user behavior.

Reference: "PEARL: Self-Evolving Assistant for Time Management
with Reinforcement Learning" (arXiv 2601.11957v2)

Phase 1 implementation: Rule-based pattern detection.
Phase 2 (future): RL-based policy learning (see FUTURE_ARCHITECTURE.md).

The key insight: Don't wait for the user to tell you their preferences.
Observe their actions and infer rules.
"""

import logging
from collections import Counter

from app.core.registry import BaseRegistry, RegistryEntry

logger = logging.getLogger(__name__)

# Pattern detection thresholds
MIN_OBSERVATIONS = 3
MIN_PATTERN_RATE = 0.7

# PEARL Pattern Registry — extensible via registration
pearl_registry = BaseRegistry[dict](name="pearl_pattern")


# ── Pattern Detector Handlers ─────────────────────────────

def detect_skip_time_window(
    user_id: str, tasks: list[dict], memory_store, **kwargs
) -> list[dict]:
    """Detect hours where user consistently skips tasks.

    Scans all tasks with a scheduled hour. Groups by hour,
    computes skip rate. If rate >= MIN_PATTERN_RATE and
    count >= MIN_OBSERVATIONS, create/reinforce a pattern.
    """
    detected = []

    # Group tasks by scheduled hour
    hour_buckets: dict[int, list[dict]] = {}
    for task in tasks:
        hour = task.get("scheduled_hour")
        if hour is not None:
            hour_buckets.setdefault(hour, []).append(task)

    for hour, bucket in hour_buckets.items():
        total = len(bucket)
        if total < MIN_OBSERVATIONS:
            continue

        skipped = sum(1 for t in bucket if t.get("status") == "skipped")
        rate = skipped / total

        if rate >= MIN_PATTERN_RATE:
            inference = f"User avoids tasks during hour {hour} (skipped {int(rate * 100)}%)"

            existing = memory_store.find_similar_memory(
                user_id, inference, memory_type="behavioral_pattern"
            )

            if existing:
                memory_store.reinforce_memory(existing["id"], user_id=user_id)
            else:
                memory_store.store_memory(user_id, {
                    "type": "behavioral_pattern",
                    "content": inference,
                    "confidence": min(0.9, rate),
                    "source": "behavior",
                    "applied_as": "soft_preference",
                    "observation_count": total,
                })

            detected.append({
                "pattern": "skip_time_window",
                "hour": hour,
                "rate": rate,
                "total": total,
                "inference": inference,
            })

    return detected


def detect_completion_time_preference(
    user_id: str, tasks: list[dict], memory_store, **kwargs
) -> list[dict]:
    """Detect which hours the user completes tasks most successfully.

    The inverse of skip detection — finds productive time windows.
    """
    detected = []

    hour_buckets: dict[int, list[dict]] = {}
    for task in tasks:
        hour = task.get("scheduled_hour")
        if hour is not None:
            hour_buckets.setdefault(hour, []).append(task)

    for hour, bucket in hour_buckets.items():
        total = len(bucket)
        if total < MIN_OBSERVATIONS:
            continue

        completed = sum(1 for t in bucket if t.get("status") == "completed")
        rate = completed / total

        if rate >= MIN_PATTERN_RATE:
            inference = f"User is most productive during hour {hour} (completed {int(rate * 100)}%)"

            existing = memory_store.find_similar_memory(
                user_id, inference, memory_type="behavioral_pattern"
            )

            if existing:
                memory_store.reinforce_memory(existing["id"], user_id=user_id)
            else:
                memory_store.store_memory(user_id, {
                    "type": "behavioral_pattern",
                    "content": inference,
                    "confidence": min(0.9, rate),
                    "source": "behavior",
                    "applied_as": "soft_preference",
                    "observation_count": total,
                })

            detected.append({
                "pattern": "completion_time_preference",
                "hour": hour,
                "rate": rate,
                "inference": inference,
            })

    return detected


# ── Registration ──────────────────────────────────────────

def register_default_patterns() -> None:
    """Register the built-in PEARL pattern detectors."""

    pearl_registry.register(RegistryEntry(
        name="skip_time_window",
        description="User consistently skips tasks in a specific time window",
        handler=detect_skip_time_window,
        examples=["skips morning tasks", "avoids late evening work"],
        metadata={
            "min_observations": MIN_OBSERVATIONS,
            "min_rate": MIN_PATTERN_RATE,
            "constraint_type": "soft_preference",
        },
    ))

    pearl_registry.register(RegistryEntry(
        name="completion_time_preference",
        description="User completes tasks most successfully at specific hours",
        handler=detect_completion_time_preference,
        examples=["productive in afternoon", "best focus 2-4 PM"],
        metadata={
            "min_observations": MIN_OBSERVATIONS,
            "min_rate": MIN_PATTERN_RATE,
            "constraint_type": "soft_preference",
        },
    ))


# ── Main Detection Entry Point ────────────────────────────

def detect_patterns(
    user_id: str,
    supabase_client,
    memory_store,
) -> list[dict]:
    """Scan user behavior data for recurring patterns.

    Fetches all tasks for the user, then runs each registered
    pattern detector. Detectors create/reinforce memories in the
    memory store.

    Args:
        user_id: User to analyze.
        supabase_client: Supabase client for querying user_tasks.
        memory_store: MemoryStore instance for creating pattern memories.

    Returns:
        List of detected patterns with metadata.
    """
    # Fetch all tasks for this user (with status and scheduled info)
    try:
        result = (
            supabase_client.table("user_tasks")
            .select("task_id, status, scheduled_hour, duration_minutes, difficulty_weight")
            .eq("user_id", user_id)
            .execute()
        )
        tasks = result.data or []
    except Exception as e:
        logger.warning("PEARL: failed to fetch tasks for %s: %s", user_id, e)
        return []

    if not tasks:
        return []

    all_detected = []

    # Run each registered pattern detector
    for name in pearl_registry.registered_names():
        entry = pearl_registry.get(name)
        if entry and callable(entry.handler):
            try:
                patterns = entry.handler(
                    user_id=user_id,
                    tasks=tasks,
                    memory_store=memory_store,
                )
                all_detected.extend(patterns)
            except Exception as e:
                logger.warning("PEARL: detector %s failed: %s", name, e)

    return all_detected


# ── Proactive Surfacing ───────────────────────────────────

def generate_proactive_insights(
    user_id: str,
    memory_store,
    min_confidence: float = 0.6,
) -> list[str]:
    """Generate user-facing insights from detected behavioral patterns.

    Returns a list of natural language strings that Jarvis can surface
    to the user, e.g., "I've noticed you tend to skip tasks before 10 AM.
    I've adjusted your schedule to avoid early morning deep work."

    Only surfaces patterns with confidence >= min_confidence.
    """
    patterns = memory_store.get_memories_by_type(
        user_id, "behavioral_pattern", min_confidence=min_confidence
    )

    insights = []
    for pattern in patterns:
        content = pattern.get("content", "")
        confidence = pattern.get("confidence", 0)
        count = pattern.get("observation_count", 0)

        if confidence < min_confidence:
            continue

        # Generate user-friendly message
        if "avoids tasks during hour" in content:
            import re
            match = re.search(r"hour (\d+)", content)
            if match:
                hour = int(match.group(1))
                if hour < 12:
                    time_desc = f"{hour} AM"
                elif hour == 12:
                    time_desc = "12 PM"
                else:
                    time_desc = f"{hour - 12} PM"
                insights.append(
                    f"I've noticed you tend to skip tasks around {time_desc}. "
                    f"I've adjusted your schedule to avoid scheduling deep work at that time."
                )
        elif "most productive during hour" in content:
            import re
            match = re.search(r"hour (\d+)", content)
            if match:
                hour = int(match.group(1))
                if hour < 12:
                    time_desc = f"{hour} AM"
                elif hour == 12:
                    time_desc = "12 PM"
                else:
                    time_desc = f"{hour - 12} PM"
                insights.append(
                    f"You seem to be most productive around {time_desc}. "
                    f"I'll prioritize your hardest tasks during that window."
                )
        else:
            insights.append(f"Pattern observed: {content}")

    return insights
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_pearl.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/memory/pearl.py tests/test_pearl.py
git commit -m "feat(pearl): add PEARL behavioral pattern detection with registry + proactive insights"
```

---

### Task 2: PEARL Integration Tests — Full Pipeline

**Files:**
- Create: `tests/test_pearl_integration.py`

- [ ] **Step 1: Write integration tests**

```python
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
```

- [ ] **Step 2: Run integration tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_pearl_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full Phase 1D test suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_pearl.py tests/test_pearl_integration.py -v`
Expected: All tests PASS

- [ ] **Step 4: Run combined 1A+1B+1C+1D suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_registry.py tests/test_intent_routing.py tests/test_draft_store.py tests/test_core_pipeline.py tests/test_memory_store.py tests/test_memory_retriever.py tests/test_memory_extractor.py tests/test_memory_constraint_bridge.py tests/test_memory_integration.py tests/test_document_registry.py tests/test_document_pipeline.py tests/test_document_integration.py tests/test_pearl.py tests/test_pearl_integration.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add tests/test_pearl_integration.py
git commit -m "test: add PEARL integration tests — pattern→memory→constraint pipeline"
```

---

## Phase 1D Complete Checklist

After completing both tasks, verify:

- [ ] PEARL Pattern Registry has 2 default detectors (skip_time_window, completion_time_preference)
- [ ] Pattern detection analyzes task data by hour, applies threshold (3+ obs, 70%+ rate)
- [ ] Detected patterns stored as `behavioral_pattern` memories in MemoryStore
- [ ] Existing patterns reinforced (not duplicated)
- [ ] Proactive insights generate user-friendly natural language messages
- [ ] Low-confidence patterns (< 0.6) do not surface
- [ ] End-to-end: pattern detection → memory → constraint bridge → TimeSlot
- [ ] Pattern registry is extensible (new detectors can be registered)
- [ ] Empty task data doesn't crash anything
- [ ] All tests pass: Phase 1A (32) + 1B (41) + 1C (23) + 1D (new)

**Next phase:** Phase 1E (Stabilize & Document) — full test suite, documentation rewrite, FUTURE_ARCHITECTURE.md.
