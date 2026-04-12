"""Observation Loop — post-turn behavioral intelligence.

Runs after every interaction (~200-500ms, blocking).
1. Extract memories (E4B)
2. Detect PEARL patterns (stats)
3. Update cognitive state (math)
4. Bridge patterns → constraints
"""

from app.core.jarvis_logger import JARVIS_LOGGER as logger


async def extract_and_store_memories(user_model, user_message: str, response_message: str) -> None:
    memory_store = await user_model.get_memory_store()
    if not memory_store:
        return
    # Future: call E4B to extract memories from conversation
    pass


async def detect_pearl_patterns(user_model) -> list[dict]:
    patterns = await user_model.get_pearl_patterns()
    return patterns


async def update_cognitive_state(user_model) -> None:
    energy = await user_model.get_estimated_energy()
    pass


async def bridge_patterns_to_constraints(user_model, patterns: list[dict]) -> None:
    for pattern in patterns:
        confidence = pattern.get("confidence", 0.0)
        if confidence < 0.7:
            continue
        pattern_type = pattern.get("type", "")
        logger.debug(f"PEARL pattern {pattern_type} (conf={confidence}) ready for bridging")


async def run_observation_loop(state: dict) -> dict:
    """Post-turn behavioral intelligence. Blocking but fast (~200-500ms)."""
    user_model = state.get("user_model")
    if not user_model:
        return {"needs_followup": False}

    user_message = state.get("user_message", "")
    response_message = state.get("response_message", "")

    await extract_and_store_memories(user_model, user_message, response_message)
    patterns = await detect_pearl_patterns(user_model)
    await update_cognitive_state(user_model)
    await bridge_patterns_to_constraints(user_model, patterns)

    return {"needs_followup": False}
