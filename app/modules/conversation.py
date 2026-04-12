"""Conversation module (CHAT-only) + synthesize_response (orchestrator step).

Conversation handles general chat by calling the LLM directly with a
conversational system prompt. synthesize_response wraps other modules'
structured output in Voice of Jarvis personality.
"""

import re

from app.core.jarvis_logger import JARVIS_LOGGER as logger

JARVIS_CHAT_PROMPT = (
    "You are Jarvis, a warm, intelligent, and supportive AI productivity assistant. "
    "You help users plan their days, manage tasks, build habits, and stay focused — "
    "without guilt or pressure. You speak in a friendly, concise tone. "
    "If the user greets you, greet them back warmly. "
    "If they ask a question, answer helpfully. "
    "Keep responses conversational and under 3 paragraphs unless they ask for detail."
)


async def run_general_chat(state: dict) -> dict:
    """Handle CHAT intent — call LLM directly for conversational response."""
    user_message = state.get("user_message", "")
    if not user_message.strip():
        return {
            "response_message": "Hey! What's on your mind?",
            "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
        }

    try:
        from app.core.model_router import route_llm_call

        # Build system prompt with memory context for personalization
        system_prompt = JARVIS_CHAT_PROMPT
        memory_context = state.get("memory_context", "")
        if memory_context:
            system_prompt += f"\n\nWhat you know about this user:\n{memory_context}"

        result = await route_llm_call(
            task="voice_of_jarvis",
            prompt=user_message,
            system_prompt=system_prompt,
            response_schema=None,
            conversation_history=state.get("conversation_history"),
        )
        message = result if isinstance(result, str) else str(result)
        message = message.strip() if message else "I'm here to help!"

        # Extract thinking process if model produces <think> blocks
        thinking = None
        think_match = re.search(r"<think>(.*)</think>", message, flags=re.DOTALL | re.IGNORECASE)
        if think_match:
            thinking = think_match.group(1).strip()
            message = re.sub(r"<think>.*</think>", "", message, flags=re.DOTALL | re.IGNORECASE).strip()

        return {
            "response_message": message,
            "thinking_process": thinking,
            "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
        }
    except Exception as e:
        logger.error(f"Conversation module error: {e}")
        return {
            "response_message": "I'm here to help! Could you tell me more?",
            "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
        }


async def voice_of_jarvis_synthesis(state: dict) -> dict:
    """Orchestrator step — wrap module output in Voice of Jarvis personality.

    Runs after Planning, Research, Coach, Knowledge modules.
    NOT used for CHAT (conversation module handles its own synthesis).
    """
    try:
        from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

        execution_summary = {
            "intent": state.get("intent", "CHAT"),
            "schedule": state.get("schedule"),
            "execution_graph": state.get("execution_graph"),
            "research_results": state.get("research_results"),
            "ingestion_result": state.get("ingestion_result"),
            "knowledge_ingested": bool(state.get("ingestion_result")),
            "clarification_request": state.get("clarification_request"),
            "error": state.get("error"),
            "user_prompt": state.get("user_message", ""),
            "memory_context": state.get("memory_context", ""),
        }
        message, thinking = await synthesize_jarvis_response(
            execution_summary,
            conversation_history=state.get("conversation_history"),
        )
        return {"response_message": message, "thinking_process": thinking}
    except Exception as e:
        logger.error(f"Voice of Jarvis synthesis error: {e}")
        return {"response_message": state.get("clarification_request", "Here's what I've got for you.")}
