"""Observation Loop — post-turn behavioral intelligence.

Runs after every interaction (~200-500ms, blocking).
1. Extract memories (Gemma fast)
2. Detect PEARL patterns (stats)
3. Update cognitive state (math)
4. Bridge patterns → constraints
"""

import asyncio
import json

from app.core.jarvis_logger import JARVIS_LOGGER as logger


def _emit_event(state: dict, event_type: str, data: dict) -> None:
    """Emit a typed SSE event directly onto the progress_queue."""
    queue = state.get("progress_queue")
    if not queue:
        return
    queue.put_nowait(json.dumps({"_event_type": event_type, **data}))


async def extract_and_store_memories(state: dict) -> list[dict]:
    """Extract memories from this conversation turn. Returns extracted memories."""
    user_model = state.get("user_model")
    if not user_model:
        return []
    memory_store = await user_model.get_memory_store()
    if not memory_store:
        return []
    user_message = state.get("user_message", "")
    response_message = state.get("response_message", "")
    if not user_message and not response_message:
        return []
    # Placeholder: real extraction will use LLM. For now, emit a stub observation.
    return []


async def detect_pearl_patterns(state: dict) -> list[dict]:
    """Detect recurring behavioral patterns. Returns detected patterns."""
    user_model = state.get("user_model")
    if not user_model:
        return []
    patterns = await user_model.get_pearl_patterns()
    return patterns


async def update_cognitive_state(state: dict) -> None:
    """Update estimated cognitive energy for this user."""
    user_model = state.get("user_model")
    if not user_model:
        return
    energy = await user_model.get_estimated_energy()
    # Future: update UserModel with fresh energy estimate


async def bridge_patterns_to_constraints(state: dict, patterns: list[dict]) -> int:
    """Convert high-confidence patterns to scheduler constraints. Returns count bridged."""
    bridged = 0
    for pattern in patterns:
        confidence = pattern.get("confidence", 0.0)
        if confidence < 0.7:
            continue
        pattern_type = pattern.get("type", "")
        logger.debug(f"PEARL pattern {pattern_type} (conf={confidence}) ready for bridging")
        bridged += 1
    return bridged


async def run_observation_loop(state: dict) -> dict:
    """Post-turn behavioral intelligence. Blocking but fast (~200-500ms)."""
    modules_invoked = state.get("modules_invoked", [])
    if len(modules_invoked) >= 10:
        return {"needs_followup": False}

    try:
        coro = _run_observation_inner(state)
        return await asyncio.wait_for(coro, timeout=0.5)
    except asyncio.TimeoutError:
        logger.warning("Observation loop exceeded 500ms budget — skipping")
        return {"needs_followup": False}


async def _run_observation_inner(state: dict) -> dict:
    """Inner observation loop with timeout guard."""
    memories = await extract_and_store_memories(state)
    for mem in memories:
        _emit_event(state, "memory_extracted", {
            "type": mem.get("memory_type", "observation"),
            "content": mem.get("content", ""),
            "confidence": mem.get("confidence", 0.5),
        })

    patterns = await detect_pearl_patterns(state)
    for pattern in patterns:
        _emit_event(state, "pattern_detected", {
            "type": pattern.get("type", "behavioral_pattern"),
            "content": pattern.get("content", ""),
            "confidence": pattern.get("confidence", 0.0),
            "occurrence_count": pattern.get("occurrence_count", 1),
            "action": pattern.get("action", "none"),
        })

    await update_cognitive_state(state)
    bridged = await bridge_patterns_to_constraints(state, patterns)

    return {
        "needs_followup": False,
        "memories_extracted_count": len(memories),
        "patterns_detected_count": len(patterns),
    }
