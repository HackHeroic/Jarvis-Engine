"""Conversation module (CHAT-only) + synthesize_response (orchestrator step).

Conversation handles general chat. synthesize_response wraps
other modules' output in Voice of Jarvis personality.
"""

from app.core.jarvis_logger import JARVIS_LOGGER as logger


async def run_general_chat(state: dict) -> dict:
    """Handle CHAT intent — general conversation."""
    user_message = state.get("user_message", "")
    try:
        from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
        execution_summary = {"intent": "CHAT", "user_prompt": user_message}
        message, thinking = await synthesize_jarvis_response(execution_summary)
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
    """Orchestrator step — wrap module output in Voice of Jarvis personality."""
    try:
        from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
        execution_summary = {
            "intent": state.get("intent", "CHAT"),
            "schedule": state.get("schedule"),
            "execution_graph": state.get("execution_graph"),
            "research_results": state.get("research_results"),
            "ingestion_result": state.get("ingestion_result"),
            "clarification_request": state.get("clarification_request"),
            "error": state.get("error"),
            "user_prompt": state.get("user_message", ""),
        }
        message, thinking = await synthesize_jarvis_response(execution_summary)
        return {"response_message": message, "thinking_process": thinking}
    except Exception as e:
        logger.error(f"Voice of Jarvis synthesis error: {e}")
        return {"response_message": state.get("clarification_request", "Here's what I've got for you.")}
