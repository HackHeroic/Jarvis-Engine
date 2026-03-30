# app/services/memory/retriever.py
"""Memory scoring, retrieval, and LLM context injection.

Implements SM-2 inspired decay scoring and Top-K retrieval.
Called at the START of every /chat request to inject relevant
memories into the LLM system prompt.
"""

import math
from datetime import datetime, timezone

from app.utils.embedding import cosine_similarity

# Type-based importance weights (higher = more important to surface)
IMPORTANCE_WEIGHTS: dict[str, float] = {
    "constraint": 1.0,
    "behavioral_pattern": 0.9,
    "preference": 0.8,
    "temporal_event": 0.8,
    "goal": 0.7,
    "fact": 0.6,
    "feedback": 0.5,
}

# Types that are ALWAYS injected regardless of relevance score
ALWAYS_INCLUDE_TYPES = {"constraint", "goal", "behavioral_pattern"}
ALWAYS_INCLUDE_MIN_CONFIDENCE = 0.6

# Base half-life in hours (1 week)
BASE_HALFLIFE_HOURS = 7 * 24


def compute_memory_strength(memory: dict, current_time: datetime) -> float:
    """SM-2 inspired decay function.

    Memory_Strength(t) = strength × e^(-t / (stability × base_halflife))

    - stability starts at 1.0, increases on each reinforcement (capped at 20)
    - base_halflife = 168 hours (1 week)
    - At stability=1: half-life = 1 week
    - At stability=5: half-life = 5 weeks
    - At stability=20 (cap): half-life = 140 days
    """
    last_reinforced_str = memory.get("last_reinforced")
    if not last_reinforced_str:
        return memory.get("strength", 1.0)

    if isinstance(last_reinforced_str, str):
        last_reinforced = datetime.fromisoformat(last_reinforced_str)
    else:
        last_reinforced = last_reinforced_str

    # Ensure timezone-aware
    if last_reinforced.tzinfo is None:
        last_reinforced = last_reinforced.replace(tzinfo=timezone.utc)

    hours_since = (current_time - last_reinforced).total_seconds() / 3600
    hours_since = max(0.0, hours_since)
    stability = max(0.01, memory.get("stability", 1.0))
    effective_halflife = stability * BASE_HALFLIFE_HOURS
    strength = memory.get("strength", 1.0)

    return strength * math.exp(-hours_since / effective_halflife)


def score_memory(
    memory: dict,
    query_embedding: list[float],
    memory_embedding: list[float],
    current_time: datetime,
) -> float:
    """Score = Relevance × Recency × Importance × Confidence."""
    relevance = cosine_similarity(query_embedding, memory_embedding)
    recency = compute_memory_strength(memory, current_time)
    importance = IMPORTANCE_WEIGHTS.get(memory.get("memory_type", "fact"), 0.5)
    confidence = memory.get("confidence", 0.5)

    return relevance * recency * importance * confidence


def format_memory_block(memories: list[dict]) -> str:
    """Format memories as a structured block for the LLM system prompt."""
    if not memories:
        return ""

    sections: dict[str, list[str]] = {}
    for mem in memories:
        mem_type = mem.get("memory_type", "fact")
        sections.setdefault(mem_type, []).append(mem.get("content", ""))

    type_labels = {
        "constraint": "Scheduling Constraints",
        "goal": "Active Goals",
        "behavioral_pattern": "Observed Patterns",
        "preference": "Preferences",
        "temporal_event": "Upcoming Events",
        "fact": "Facts",
        "feedback": "User Feedback on Jarvis",
    }

    lines = ["## What you know about this user:\n"]
    for mem_type, label in type_labels.items():
        if mem_type in sections:
            lines.append(f"### {label}")
            for content in sections[mem_type]:
                lines.append(f"- {content}")
            lines.append("")

    return "\n".join(lines)


def deduplicate_memories(memories: list[dict]) -> list[dict]:
    """Remove duplicate memories by ID."""
    seen: set[str] = set()
    result = []
    for mem in memories:
        mem_id = mem.get("id", "")
        if mem_id and mem_id not in seen:
            seen.add(mem_id)
            result.append(mem)
    return result


def build_memory_context(user_id: str, memory_store) -> str:
    """Retrieve and format memories for LLM context injection.

    Called at the START of every /chat request.
    Returns a formatted string to inject into the LLM system prompt.
    Uses importance + confidence + recency scoring (no embeddings — fast path).
    """
    all_memories = memory_store.get_active_memories(user_id)
    if not all_memories:
        return ""

    current_time = datetime.now(timezone.utc)

    # Always-include: constraints, goals, patterns with high confidence
    must_include = [
        mem for mem in all_memories
        if mem.get("memory_type") in ALWAYS_INCLUDE_TYPES
        and mem.get("confidence", 0) >= ALWAYS_INCLUDE_MIN_CONFIDENCE
        and mem.get("superseded_by") is None
    ]

    # Score remaining by recency × importance × confidence
    remaining = [m for m in all_memories if m not in must_include]
    scored = []
    for mem in remaining:
        recency = compute_memory_strength(mem, current_time)
        importance = IMPORTANCE_WEIGHTS.get(mem.get("memory_type", "fact"), 0.5)
        confidence = mem.get("confidence", 0.5)
        score = recency * importance * confidence
        scored.append((mem, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    top_k = [mem for mem, _ in scored[:15]]

    final = deduplicate_memories(must_include + top_k)
    return format_memory_block(final)
