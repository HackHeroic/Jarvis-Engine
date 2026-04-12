"""LM Studio native streaming proxy — transparent SSE relay for direct LLM chat."""

import json
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import app.core.config as _cfg
from app.core.config import LM_STUDIO_NATIVE_URL
from app.core.jarvis_logger import log_step

router = APIRouter()


class LMStudioMessage(BaseModel):
    role: str = Field(..., description="Message role: system, user, or assistant")
    content: str = Field(..., description="Message content")


class LMStudioChatRequest(BaseModel):
    """Request body for direct LM Studio streaming chat.

    Frontend sends 'messages' array (OpenAI-style). The proxy converts to
    LM Studio's native format: 'input' (string or structured items) +
    optional 'system_prompt'.
    """

    messages: list[LMStudioMessage] = Field(..., description="Conversation messages")
    model: Optional[str] = Field(default=None, description="LM Studio model identifier")
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    reasoning: Optional[str] = Field(
        default=None,
        description="Reasoning effort: off, low, medium, high, on",
    )


def _convert_messages_to_native(messages: list[LMStudioMessage]) -> tuple[str | list[dict], str | None]:
    """Convert OpenAI-style messages array to LM Studio native 'input' + 'system_prompt'.

    LM Studio native API uses:
      - 'input': a string for single-turn, or list of {type:"message", content:...} for multi-turn
      - 'system_prompt': extracted from system role messages

    For multi-turn, we use previous_response_id chaining ideally, but since we
    don't track that, we concatenate conversation context into a single input.
    """
    system_prompt: str | None = None
    conversation_parts: list[str] = []

    for msg in messages:
        if msg.role == "system":
            system_prompt = msg.content
        elif msg.role == "user":
            conversation_parts.append(msg.content)
        elif msg.role == "assistant":
            conversation_parts.append(f"[Assistant]: {msg.content}")

    # If only one user message (no conversation history), send as plain string
    if len(conversation_parts) == 1 and not any(m.role == "assistant" for m in messages):
        return conversation_parts[0], system_prompt

    # Multi-turn: combine into single input with context
    combined = "\n\n".join(conversation_parts)
    return combined, system_prompt


@router.post(
    "/stream",
    summary="LM Studio native streaming proxy (SSE)",
    description=(
        "Proxies requests to LM Studio's native /api/v1/chat endpoint with stream=true. "
        "Re-streams all SSE events (chat.start, reasoning.delta, message.delta, etc.) unchanged."
    ),
)
async def lmstudio_stream(request: LMStudioChatRequest, http_request: Request) -> StreamingResponse:
    """Proxy LM Studio native SSE stream to the frontend."""
    log_step("LMSTUDIO_STREAM", "Proxying to LM Studio native API", {
        "model": request.model,
        "message_count": len(request.messages),
    })

    upstream_url = f"{LM_STUDIO_NATIVE_URL}/api/v1/chat"

    # Resolve model ID — default to configured local model, strip LiteLLM "openai/" prefix
    model_id = request.model or _cfg.LOCAL_LLM_MODEL
    if model_id.startswith("openai/"):
        model_id = model_id[len("openai/"):]
    # Also strip publisher prefix — LM Studio uses short names (e.g. "gemma-4-9b-it")
    if "/" in model_id:
        model_id = model_id.rsplit("/", 1)[-1]

    # Convert messages to LM Studio native format
    native_input, system_prompt = _convert_messages_to_native(request.messages)

    body: dict = {
        "model": model_id,
        "input": native_input,
        "stream": True,
    }
    if system_prompt:
        body["system_prompt"] = system_prompt
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.max_tokens is not None:
        body["max_output_tokens"] = request.max_tokens
    if request.reasoning:
        body["reasoning"] = request.reasoning

    async def event_stream():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10)) as client:
                async with client.stream("POST", upstream_url, json=body) as upstream:
                    if upstream.status_code != 200:
                        error_body = ""
                        async for chunk in upstream.aiter_text():
                            error_body += chunk
                        yield f"event: error\ndata: {json.dumps({'type': 'upstream_error', 'message': f'LM Studio returned {upstream.status_code}: {error_body[:500]}'})}\n\n"
                        return

                    # Re-stream SSE lines transparently
                    async for line in upstream.aiter_lines():
                        if await http_request.is_disconnected():
                            break
                        yield line + "\n"

        except httpx.ConnectError:
            yield f"event: error\ndata: {json.dumps({'type': 'connection_error', 'message': 'Cannot reach LM Studio. Is it running at ' + LM_STUDIO_NATIVE_URL + '?'})}\n\n"
        except httpx.ReadError as e:
            yield f"event: error\ndata: {json.dumps({'type': 'read_error', 'message': f'Stream interrupted: {e}'})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'type': 'proxy_error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
