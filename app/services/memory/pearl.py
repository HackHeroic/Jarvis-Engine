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


def detect_duration_preference(
    user_id: str, tasks: list[dict], memory_store, **kwargs
) -> list[dict]:
    """Detect if user consistently edits task durations shorter."""
    edited = [t for t in tasks if t.get("original_duration_minutes") and t.get("duration_minutes")]
    if len(edited) < MIN_OBSERVATIONS:
        return []

    shorter_count = sum(
        1 for t in edited
        if t["duration_minutes"] < t["original_duration_minutes"]
    )
    rate = shorter_count / len(edited)

    if rate < MIN_PATTERN_RATE:
        return []

    avg_original = sum(t["original_duration_minutes"] for t in edited) / len(edited)
    avg_edited = sum(t["duration_minutes"] for t in edited) / len(edited)
    pattern_content = f"User prefers shorter tasks (avg edited from {avg_original:.0f} to {avg_edited:.0f} minutes)"

    existing = memory_store.find_similar_memory(user_id, pattern_content, memory_type="behavioral_pattern")
    if existing:
        memory_store.reinforce_memory(existing["id"], user_id=user_id)
        return [{"pattern": "duration_preference", "action": "reinforced"}]

    memory_store.store_memory(user_id, {
        "type": "behavioral_pattern",
        "content": pattern_content,
        "confidence": min(0.9, rate),
        "source": "behavior",
        "applied_as": "adjust_defaults",
        "observation_count": len(edited),
    })
    return [{"pattern": "duration_preference", "action": "created", "avg_duration": avg_edited}]


def detect_deadline_buffer(
    user_id: str, tasks: list[dict], memory_store, **kwargs
) -> list[dict]:
    """Detect if user consistently extends deadlines.

    Looks at tasks that have deadline edit metadata (original vs current
    deadline_hint). If the user extends deadlines on >= MIN_PATTERN_RATE
    of tasks with MIN_OBSERVATIONS+ edits, create a behavioral pattern
    recording the average extension days.
    """
    detected = []

    # Collect tasks that have deadline extension info
    extensions: list[float] = []
    tasks_with_deadlines = 0

    for task in tasks:
        original_deadline = task.get("original_deadline_hint")
        current_deadline = task.get("deadline_hint")

        if not original_deadline or not current_deadline:
            continue

        tasks_with_deadlines += 1

        # Parse ISO date strings to compute extension days
        try:
            from datetime import datetime
            orig = datetime.fromisoformat(original_deadline.replace("Z", "+00:00"))
            curr = datetime.fromisoformat(current_deadline.replace("Z", "+00:00"))
            delta_days = (curr - orig).total_seconds() / 86400
            if delta_days > 0:
                extensions.append(delta_days)
        except (ValueError, TypeError):
            continue

    if tasks_with_deadlines < MIN_OBSERVATIONS:
        return detected

    extension_rate = len(extensions) / tasks_with_deadlines

    if len(extensions) >= MIN_OBSERVATIONS and extension_rate >= MIN_PATTERN_RATE:
        avg_days = sum(extensions) / len(extensions)
        inference = (
            f"User tends to extend deadlines by ~{avg_days:.1f} days "
            f"({int(extension_rate * 100)}% of deadlined tasks)"
        )

        existing = memory_store.find_similar_memory(
            user_id, inference, memory_type="behavioral_pattern"
        )

        if existing:
            memory_store.reinforce_memory(existing["id"], user_id=user_id)
        else:
            memory_store.store_memory(user_id, {
                "type": "behavioral_pattern",
                "content": inference,
                "confidence": min(0.9, extension_rate),
                "source": "behavior",
                "applied_as": "soft_preference",
                "observation_count": tasks_with_deadlines,
            })

        detected.append({
            "pattern": "deadline_buffer",
            "avg_extension_days": avg_days,
            "rate": extension_rate,
            "total": tasks_with_deadlines,
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
        name="duration_preference",
        description="User consistently edits task durations shorter",
        handler=detect_duration_preference,
        examples=["always shortens 30-min tasks to 15", "prefers shorter durations"],
        metadata={
            "min_observations": MIN_OBSERVATIONS,
            "min_rate": MIN_PATTERN_RATE,
            "constraint_type": "soft_preference",
        },
    ))

    pearl_registry.register(RegistryEntry(
        name="deadline_buffer",
        description="User consistently extends deadlines by a certain number of days",
        handler=detect_deadline_buffer,
        examples=["always pushes deadlines back 2 days", "extends due dates"],
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
            .select("task_id, status, scheduled_hour, duration_minutes, original_duration_minutes, difficulty_weight, deadline_hint, original_deadline_hint")
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


def _hour_to_time_str(hour: int) -> str:
    """Convert a 24-hour integer to a human-readable time string."""
    if hour == 0:
        return "12 AM"
    if hour < 12:
        return f"{hour} AM"
    if hour == 12:
        return "12 PM"
    return f"{hour - 12} PM"


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
                time_desc = _hour_to_time_str(hour)
                insights.append(
                    f"I've noticed you tend to skip tasks around {time_desc}. "
                    f"I've adjusted your schedule to avoid scheduling deep work at that time."
                )
        elif "most productive during hour" in content:
            match = re.search(r"hour (\d+)", content)
            if match:
                hour = int(match.group(1))
                time_desc = _hour_to_time_str(hour)
                insights.append(
                    f"I've noticed you're most productive around {time_desc}. "
                    f"I'll prioritise your hardest tasks during that window."
                )
        elif "prefers shorter tasks" in content:
            insights.append(
                f"I've noticed you often shorten task durations. "
                f"I'll default to shorter time blocks for new tasks."
            )
        elif "extend" in content and "deadline" in content:
            insights.append(
                f"I've noticed you tend to extend deadlines. "
                f"I'll build in a buffer when scheduling around due dates."
            )
        else:
            insights.append(f"Pattern observed: {content}")

    return insights
