"""Task-based model routing: task → role → the provider chain.

``route_llm_call`` is the single call site for language models. It maps the task to a
role, decides whether this call may use the on-device model, and hands an
``LLMRequest`` to the process-wide ``FallbackChainProvider``
(``app/infrastructure/llm``). The chain owns the rest: local first, cloud fallback by
error type, timeouts, retries, the PreCloudLLM (PII) gate on every cloud send, and
usage metering.
"""

import contextvars
from collections.abc import AsyncIterator
from contextlib import aclosing
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel

import app.core.config as _cfg
from app.domain.entities import LLMLocation
from app.domain.errors import LLMUnavailableError
from app.domain.ports import LLMRequest
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import get_chain
from app.infrastructure.llm.transport import strip_fences  # noqa: F401  (re-export)
from app.orchestrator.hooks import ActionHooks

# Per-request override: set by chat.py when frontend model picker selects Gemini.
# ContextVar so concurrent requests don't stomp each other.
force_cloud_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "force_cloud_var", default=False
)


class ModelRole(str, Enum):
    PRIMARY = "primary"
    FAST = "fast"
    CLOUD = "cloud"


MODEL_ROUTING: dict[str, ModelRole] = {
    "socratic_chunker": ModelRole.PRIMARY,
    "habit_translation": ModelRole.PRIMARY,
    "document_understanding": ModelRole.PRIMARY,
    "research_summarization": ModelRole.PRIMARY,
    "intent_classification": ModelRole.FAST,
    "brain_dump_extraction": ModelRole.FAST,
    "memory_extraction": ModelRole.FAST,
    "voice_of_jarvis": ModelRole.FAST,
    "calendar_parsing": ModelRole.FAST,
    "goal_validation": ModelRole.FAST,
    "web_search": ModelRole.CLOUD,
    "real_time_research": ModelRole.CLOUD,
}


async def _replay(
    head: tuple[tuple[str, str], ...], rest: AsyncIterator[tuple[str, str]]
) -> AsyncIterator[tuple[str, str]]:
    async with aclosing(rest):
        for item in head:
            yield item
        async for item in rest:
            yield item


async def _open_stream(
    chain: FallbackChainProvider, req: LLMRequest, skip_local: bool
) -> AsyncIterator[tuple[str, str]]:
    """Start the chain's stream and wait for its first token.

    A stream that cannot start raises here, at the caller's ``await`` — where
    ``hybrid_route_query(stream=True)`` raised when the model call never began — so
    callers' "failed before any text" handling still applies.
    """
    tokens = chain.stream(req, skip_local=skip_local)
    try:
        first = await anext(tokens)
    except StopAsyncIteration:
        return _replay((), tokens)
    return _replay((first,), tokens)


async def route_llm_call(
    task: str,
    prompt: str,
    system_prompt: str = "",
    response_schema: Optional[type[BaseModel]] = None,
    hooks: Optional[ActionHooks] = None,
    conversation_history: Optional[list[dict]] = None,
    force_cloud: Optional[bool] = None,
    stream: bool = False,
) -> str | BaseModel | Any:
    """Route one LLM call through the provider chain.

    The on-device model is skipped for this call when any of these hold:
      1. explicit ``force_cloud=True`` kwarg
      2. ``force_cloud_var`` ContextVar (set by chat endpoint when user picks Gemini)
      3. ``_cfg.GEMINI_PRIMARY`` or ``_cfg.CLAUDE_PRIMARY``
      4. the task's role is CLOUD

    Structured calls (``response_schema``) return an instance of the schema.
    ``stream=True`` returns an async generator of ``(event_type, token)`` pairs
    instead of a string, and is incompatible with ``response_schema`` (partial JSON
    cannot be validated). It takes the same route as a plain call — same local-first
    attempt, same cloud fallback, same PreCloudLLM gate.

    ``hooks`` is accepted for compatibility and no longer consulted: the
    PreCloudLLM gate belongs to the chain's ``RedactingProvider``, which uses the
    process-wide hooks (``get_hooks()``).

    Raises ``RuntimeError`` when every reachable provider failed and no cloud
    provider is configured, and ``LLMUnavailableError`` when cloud ones failed too.
    """
    if stream and response_schema is not None:
        raise ValueError("route_llm_call: stream=True cannot be combined with response_schema")

    role = MODEL_ROUTING.get(task, ModelRole.FAST)
    skip_local = (
        role == ModelRole.CLOUD
        or bool(force_cloud)
        or force_cloud_var.get()
        or _cfg.GEMINI_PRIMARY
        or _cfg.CLAUDE_PRIMARY
    )
    req = LLMRequest(
        task=task,
        role=role.value,
        prompt=prompt,
        system_prompt=system_prompt,
        conversation_history=conversation_history,
        response_schema=response_schema,
        timeout_s=_cfg.LLM_STREAM_TIMEOUT_SECONDS if stream else _cfg.LLM_TIMEOUT_SECONDS,
    )

    chain = get_chain()
    try:
        if stream:
            return await _open_stream(chain, req, skip_local)
        return await chain.complete(req, skip_local=skip_local)
    except LLMUnavailableError as exc:
        if not chain.has_available(LLMLocation.CLOUD):
            raise RuntimeError(f"Local LLM failed for {task} and no GEMINI_API_KEY set") from exc
        raise
