"""Conversation module (CHAT-only) + synthesize_response (orchestrator step).

Conversation handles general chat by calling the LLM directly with a
conversational system prompt. synthesize_response wraps other modules'
structured output in Voice of Jarvis personality.
"""

import re

from app.core.jarvis_logger import JARVIS_LOGGER as logger

JARVIS_CHAT_PROMPT = (
    "You are J.A.R.V.I.S. (Just A Rather Very Intelligent System), the user's personal AI. "
    "You are loyal, brilliant, and composed — modeled after Tony Stark's AI companion.\n\n"
    "VOICE & TONE:\n"
    "- Address the user as 'sir'. Use refined British cadence: 'Shall I...', "
    "'It would appear...', 'If I may, sir...', 'I've taken the liberty of...', 'Might I suggest...'\n"
    "- Be formal but warm — you are not a butler reading from a script. "
    "You are a trusted confidant who happens to speak properly.\n"
    "- Keep responses concise. Prefer one clean sentence over three adequate ones.\n"
    "- Calm under all circumstances. If the user is stressed, become MORE composed, not less.\n"
    "- Show care through competence, not flattery. Never say 'Great idea!' — "
    "instead, execute flawlessly and note improvements you made.\n"
    "- Dry wit through understatement, never jokes or sarcasm. "
    "Let humor emerge from deadpan observations.\n"
    "- Anticipate what the user needs next. Think two steps ahead.\n\n"
    "DIALOGUE PATTERNS:\n"
    "- Greeting: 'Good evening, sir. All systems are nominal.' / 'Good to see you, sir. What shall we tackle?'\n"
    "- Suggestion: 'If I may, sir — there is a simpler approach.'\n"
    "- Bad news: 'We have a situation, sir. [fact]. I'm already [solution].'\n"
    "- Wit: User: 'This is impossible.' → 'Improbable, sir. But then, we do have a history with those.'\n"
    "- Wit: User: 'Can you do that?' → 'I already have, sir.'\n\n"
    "NEVER DO:\n"
    "- Use emoji, exclamation marks, or casual interjections ('Sure!', 'Awesome!', 'No problem!')\n"
    "- Begin responses with 'I' — rephrase to lead with the information or action\n"
    "- Refer to yourself as 'just an AI' or apologize for being artificial\n"
    "- Say 'How can I help you today?' or anything that sounds like a productivity app\n"
    "- Be mean, condescending, or sarcastic. You are composed, never cutting\n"
    "- Break character under any circumstances\n\n"
    "Keep responses under 3 sentences unless they ask for detail."
)


async def _stream_chat_reply(
    user_message: str,
    system_prompt: str,
    progress_queue,
    conversation_history: list[dict] | None,
) -> tuple[str, str | None]:
    """Stream one CHAT reply through the progress bridge.

    Goes through route_llm_call like the non-streaming branch, so streaming
    keeps the same local-first attempt, cloud fallback and PreCloudLLM (PII)
    gate. Returns the accumulated (message, thinking). Raises before the first
    token if the model call never starts, so the caller's fallback still
    applies; once tokens are on the wire the partial reply is kept — replacing
    it would erase text the user has already read.
    """
    from app.core.model_router import route_llm_call
    from app.orchestrator.graph import make_token_emitter

    emit = make_token_emitter(progress_queue)

    token_gen = await route_llm_call(
        task="voice_of_jarvis",
        prompt=user_message,
        system_prompt=system_prompt,
        response_schema=None,
        conversation_history=conversation_history,
        stream=True,
    )

    message_parts: list[str] = []
    thinking_parts: list[str] = []
    try:
        async for evt_type, tok in token_gen:
            if evt_type == "reasoning":
                thinking_parts.append(tok)
                emit(tok, event_type="thinking_token")
            else:
                message_parts.append(tok)
                emit(tok)
    except Exception as e:
        logger.warning(f"CHAT token stream broke mid-reply: {e}")
        if not message_parts:
            raise

    message = "".join(message_parts).strip() or "Standing by, sir."
    thinking = "".join(thinking_parts).strip() or None
    return message, thinking


async def run_general_chat(state: dict) -> dict:
    """Handle CHAT intent — call LLM directly for conversational response."""
    user_message = state.get("user_message", "")
    if not user_message.strip():
        return {
            "response_message": "At your service, sir.",
            "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
        }

    try:
        from app.core.model_router import route_llm_call

        # Build system prompt with memory context for personalization.
        # Trivial inputs (greetings, short emotional acks) don't need 4 DB round-trips —
        # the LLM will respond warmly without context anyway.
        system_prompt = JARVIS_CHAT_PROMPT
        memory_context = state.get("memory_context", "")
        is_trivial = state.get("trivial_input", False)
        if not memory_context and not is_trivial:
            user_model = state.get("user_model")
            if user_model and hasattr(user_model, "build_chat_context"):
                try:
                    memory_context = await user_model.build_chat_context()
                except Exception as e:
                    logger.warning(f"build_chat_context failed: {e}")
                    memory_context = ""
        if memory_context:
            system_prompt += f"\n\nWhat you know about this user:\n{memory_context}"

        # Inject relevant knowledge from ChromaDB if available
        try:
            from app.utils.chroma_client import query_knowledge
            user_model = state.get("user_model")
            if user_model:
                chunks = query_knowledge(user_model.user_id, user_message, n_results=3)
                if chunks:
                    system_prompt += "\n\nRelevant knowledge from user's documents:\n" + "\n---\n".join(chunks[:3])
        except Exception:
            pass  # ChromaDB unavailable — proceed without RAG

        progress_queue = state.get("progress_queue")
        if progress_queue is not None:
            # Live SSE turn: stream the reply token-by-token through the bridge
            # instead of making the endpoint re-emit it as one blob.
            message, thinking = await _stream_chat_reply(
                user_message,
                system_prompt,
                progress_queue,
                state.get("conversation_history"),
            )
            return {
                "response_message": message,
                "thinking_process": thinking,
                "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
            }

        result = await route_llm_call(
            task="voice_of_jarvis",
            prompt=user_message,
            system_prompt=system_prompt,
            response_schema=None,
            conversation_history=state.get("conversation_history"),
        )
        message = result if isinstance(result, str) else str(result)
        message = message.strip() if message else "Standing by, sir."

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
            "response_message": "A minor hiccup on my end, sir. Shall we try that again?",
            "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
        }


async def voice_of_jarvis_synthesis(state: dict) -> dict:
    """Orchestrator step — wrap module output in Voice of Jarvis personality.

    Runs after Planning, Research, Coach, Knowledge modules.
    NOT used for CHAT (conversation module handles its own synthesis).

    If the upstream module already produced a complete response_message
    (e.g., coach module with LLM-generated mastery coaching), pass it through
    instead of re-synthesizing — avoids overwriting with a generic message.
    """
    # Coach module already produces a complete, personality-rich response via LLM
    # with mastery data baked in. Don't re-synthesize — pass through.
    intent = str(state.get("intent", ""))
    # IntentType enum str() gives "IntentType.CHECK_PROGRESS" or just "CHECK_PROGRESS"
    if "CHECK_PROGRESS" in intent and state.get("response_message"):
        return {}  # keep coach's response_message as-is

    try:
        import app.services.analytical.voice_of_jarvis as _voj

        execution_summary = _voj.build_summary_from_state(state, intent)

        progress_queue = state.get("progress_queue")
        if progress_queue is not None:
            # Live SSE turn: stream synthesis through the bridge so the user
            # sees the sentence form instead of waiting for the whole blob.
            from app.orchestrator.graph import make_token_emitter

            emit = make_token_emitter(progress_queue)
            message_parts: list[str] = []
            thinking_parts: list[str] = []
            async for evt_type, tok in _voj.synthesize_jarvis_response_stream(
                execution_summary
            ):
                if evt_type == "thinking":
                    thinking_parts.append(tok)
                    emit(tok, event_type="thinking_token")
                else:
                    message_parts.append(tok)
                    emit(tok)
            return {
                "response_message": "".join(message_parts).strip() or "Standing by, sir.",
                "thinking_process": "".join(thinking_parts).strip() or None,
            }

        message, thinking = await _voj.synthesize_jarvis_response(
            execution_summary,
            conversation_history=state.get("conversation_history"),
        )
        return {"response_message": message, "thinking_process": thinking}
    except Exception as e:
        logger.error(f"Voice of Jarvis synthesis error: {e}")
        return {"response_message": state.get("response_message") or state.get("clarification_request", "All sorted, sir.")}
