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
    """Extract memories from this conversation turn using LLM."""
    user_model = state.get("user_model")
    if not user_model:
        return []
    memory_store = await user_model.get_memory_store()
    if not memory_store:
        return []
    user_message = state.get("user_message", "")
    response_message = state.get("response_message", "")
    if not user_message.strip():
        return []
    try:
        from app.services.memory.extractor import extract_memories_from_turn
        extracted = await extract_memories_from_turn(
            user_model.user_id, user_message, response_message, memory_store
        )
        return extracted if isinstance(extracted, list) else []
    except Exception as e:
        logger.warning(f"Memory extraction failed (non-fatal): {e}")
        return []


async def detect_pearl_patterns(state: dict) -> list[dict]:
    """Detect recurring behavioral patterns from task history."""
    user_model = state.get("user_model")
    if not user_model:
        return []
    try:
        db = getattr(user_model, "_db", None)
        supabase = db.supabase if db and hasattr(db, "supabase") else None
        if not supabase:
            return []
        memory_store = await user_model.get_memory_store()
        if not memory_store:
            return []
        from app.services.memory.pearl import detect_patterns
        import asyncio
        patterns = await asyncio.to_thread(detect_patterns, user_model.user_id, supabase, memory_store)
        return patterns if isinstance(patterns, list) else []
    except Exception as e:
        logger.warning(f"PEARL pattern detection failed (non-fatal): {e}")
        return []


async def update_cognitive_state(state: dict) -> None:
    """Update estimated cognitive energy for this user."""
    user_model = state.get("user_model")
    if not user_model:
        return
    energy = await user_model.get_estimated_energy()
    # Future: update UserModel with fresh energy estimate


async def bridge_patterns_to_constraints(state: dict, patterns: list[dict]) -> int:
    """Persist high-confidence PEARL patterns as memory constraints.

    Patterns with rate/confidence >= 0.7 are already stored in the memory store
    by the PEARL detectors themselves. This function logs the count and returns it
    so the caller can report how many were bridged.

    The actual memory → TimeSlot conversion happens in planning_graph.memory_to_constraints
    (called during _run_plan_day_flow before solve_schedule).
    """
    bridged = 0
    for pattern in patterns:
        # Normalise: PEARL detectors use "rate"; generic patterns use "confidence"
        confidence = pattern.get("rate", pattern.get("confidence", 0.0))
        if confidence < 0.7:
            continue
        pattern_type = pattern.get("pattern", pattern.get("type", ""))
        logger.debug(f"PEARL pattern '{pattern_type}' (conf={confidence:.2f}) stored in memory — will apply on next replan")
        bridged += 1
    return bridged


# Strong-reference set so background tasks don't get GC'd mid-await.
# Add task → discard on completion. See Python asyncio.create_task docs.
_BACKGROUND_TASKS: set = set()


def _spawn_background(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t


async def run_observation_loop(state: dict) -> dict:
    """Post-turn behavioral intelligence.

    - Emits phase_start + phase_end SSE so frontend timing is accurate.
    - Skips entirely on trivial inputs (greetings/emotional) — no memories from "hi".
    - Memory extraction runs as a tracked BACKGROUND task (chat returns instantly).
    - PEARL + cognitive update run inline with a real 500ms cap (no shield —
      cancellation must propagate so we honor the cap).
    """
    modules_invoked = state.get("modules_invoked", [])
    if len(modules_invoked) >= 10:
        return {"needs_followup": False}

    user_msg = (state.get("user_message") or "").strip().lower()
    is_trivial = state.get("trivial_input") or len(user_msg) < 6
    if is_trivial:
        _emit_event(state, "phase_end", {"phase": "learning", "skipped": True})
        return {"needs_followup": False, "memories_extracted_count": 0, "patterns_detected_count": 0}

    _emit_event(state, "phase_start", {"phase": "learning"})

    # Memory extraction → tracked background task (held in _BACKGROUND_TASKS so it
    # cannot be GC'd before completing).
    try:
        _spawn_background(_background_memory_extraction(state))
    except Exception as e:
        logger.warning(f"Failed to schedule background memory extraction: {e}")

    # PEARL + cognitive state are cheap stats/math; run inline with a REAL 500ms cap.
    # No asyncio.shield — we want the work cancelled when the cap fires, not leaked.
    try:
        result = await asyncio.wait_for(_run_inline_observation(state), timeout=0.5)
        _emit_event(state, "phase_end", {"phase": "learning"})
        return result
    except asyncio.TimeoutError:
        logger.warning("Inline observation (PEARL+cognitive) exceeded 500ms — cancelled and skipped")
        _emit_event(state, "phase_end", {"phase": "learning", "timeout": True})
        return {"needs_followup": False}


async def _background_memory_extraction(state: dict) -> None:
    """Run memory extraction off the chat critical path. Logs failures, never raises."""
    try:
        memories = await extract_and_store_memories(state)
        for mem in memories:
            _emit_event(state, "memory_extracted", {
                "type": mem.get("memory_type", "observation"),
                "content": mem.get("content", ""),
                "confidence": mem.get("confidence", 0.5),
            })
    except Exception as e:
        logger.warning(f"Background memory extraction failed: {e}")


async def _run_inline_observation(state: dict) -> dict:
    """Cheap inline work — PEARL stats + cognitive math. Targets <500ms."""
    patterns = await detect_pearl_patterns(state)
    for pattern in patterns:
        _emit_event(state, "pattern_detected", {
            "type": pattern.get("pattern", pattern.get("type", "behavioral_pattern")),
            "content": pattern.get("inference", pattern.get("content", "")),
            "confidence": pattern.get("rate", pattern.get("confidence", 0.0)),
            "occurrence_count": pattern.get("total", pattern.get("occurrence_count", 1)),
            "action": pattern.get("action", "detected"),
        })

    await update_cognitive_state(state)
    await bridge_patterns_to_constraints(state, patterns)

    return {
        "needs_followup": False,
        "patterns_detected_count": len(patterns),
    }
