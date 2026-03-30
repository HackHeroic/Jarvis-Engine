# app/services/memory/extractor.py
"""LLM-based memory extraction from conversation turns.

Runs as a fire-and-forget background task after each /chat response.
Uses Qwen-4B (prefer_local=True) since this is a background task
that doesn't need to be perfect — a missed extraction is fine.
"""

import logging

from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.memory import MemoryExtractionResponse

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Analyze this conversation exchange and extract new information about the user.

EXISTING MEMORIES (what we already know):
{existing_memories}

CURRENT EXCHANGE:
User: {user_message}
Assistant: {assistant_response}

Extract ONLY genuinely new information. Do not repeat what we already know.
If the user contradicts an existing memory, mark it as a contradiction.

Return JSON object with "memories" array:
{{"memories": [
  {{
    "type": "fact|preference|behavioral_pattern|temporal_event|goal|feedback|constraint",
    "content": "concise statement of what was learned",
    "confidence": 0.5-1.0,
    "contradicts": "id of existing memory this contradicts, or null",
    "expires_at": "ISO date if temporal, or null"
  }}
]}}

Return {{"memories": []}} if nothing new was learned."""

EXTRACTION_SYSTEM_PROMPT = (
    "You are a memory extraction agent. Extract structured facts, "
    "preferences, and constraints from conversations. Return valid JSON only."
)


async def extract_memories_from_turn(
    user_id: str,
    user_message: str,
    assistant_response: str,
    memory_store,
) -> list[dict]:
    """Extract structured memories from a conversation turn.

    Args:
        user_id: User who sent the message.
        user_message: What the user said.
        assistant_response: What Jarvis replied.
        memory_store: MemoryStore instance for CRUD operations.

    Returns:
        List of extracted memory dicts.
    """
    existing = memory_store.get_active_memories(user_id)

    formatted_existing = "\n".join([
        f"[{m.get('id', '?')}] ({m.get('memory_type', '?')}) {m.get('content', '')}"
        for m in existing[:20]
    ])

    prompt = EXTRACTION_PROMPT.format(
        existing_memories=formatted_existing or "None yet.",
        user_message=user_message[:500],
        assistant_response=assistant_response[:500],
    )

    raw = await hybrid_route_query(
        user_prompt=prompt,
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
        response_schema=MemoryExtractionResponse,
        prefer_local=True,
    )

    if isinstance(raw, dict):
        memories_data = raw.get("memories", [])
    elif hasattr(raw, "memories"):
        memories_data = [m.model_dump() if hasattr(m, "model_dump") else m for m in raw.memories]
    else:
        memories_data = []

    results = []
    for mem in memories_data:
        contradicts = mem.get("contradicts")
        if contradicts:
            memory_store.supersede_memory(contradicts, user_id, mem.get("content", ""))
        else:
            similar = memory_store.find_similar_memory(
                user_id, mem.get("content", ""), threshold=0.85
            )
            if similar:
                memory_store.reinforce_memory(similar["id"], user_id=user_id)
            else:
                memory_store.store_memory(user_id, mem)

        results.append(mem)

    return results


async def safe_extract_memories(
    user_id: str,
    user_message: str,
    assistant_response: str,
    memory_store,
    db_client=None,
) -> None:
    """Fire-and-forget wrapper. Catches ALL errors.

    A failed extraction means we miss one memory — not that the user's
    request fails. Called via asyncio.create_task() after the response is sent.

    After extraction, chains PEARL pattern detection if a db_client is available.
    """
    try:
        await extract_memories_from_turn(
            user_id=user_id,
            user_message=user_message,
            assistant_response=assistant_response,
            memory_store=memory_store,
        )
        # Chain PEARL detection after extraction
        if db_client:
            from app.services.memory.pearl import detect_patterns
            detect_patterns(user_id, db_client, memory_store)
    except Exception as e:
        logger.debug("Memory extraction/PEARL failed (non-blocking): %s", e)
