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
import re

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
            match = re.search(r"hour (\d+)", content)
            if match:
                hour = int(match.group(1))
                if hour == 0:
                    time_desc = "12 AM"
                elif hour < 12:
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
            match = re.search(r"hour (\d+)", content)
            if match:
                hour = int(match.group(1))
                if hour == 0:
                    time_desc = "12 AM"
                elif hour < 12:
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
