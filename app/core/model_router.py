"""Task-based model routing: task → role → the provider chain.

``route_llm_call`` is the single call site for language models. It maps the task to a
role, decides whether this call may use the on-device model, and hands an
``LLMRequest`` to the process-wide ``FallbackChainProvider``
(``app/infrastructure/llm``). The chain owns the rest: local first, cloud fallback by
error type, timeouts, retries, the PreCloudLLM (PII) gate on every cloud send, and
usage metering.
"""

import contextvars
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, nullcontext
from enum import Enum
from types import ModuleType
from typing import Any, Optional

from pydantic import BaseModel

import app.core.config as _cfg
from app.domain.entities import LLMLocation
from app.domain.errors import LLMUnavailableError
from app.domain.events import EventBus, NullEventBus
from app.domain.ports import LLMRequest
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.decorators import (  # noqa: F401  (ServedModels: re-export)
    ServedModels,
    recording_served_models,
    redacting_with,
)
from app.infrastructure.llm.factory import LLMProviderFactory, current_chain, set_chain
from app.infrastructure.llm.transport import strip_fences  # noqa: F401  (re-export)
from app.orchestrator.hooks import ActionHooks, HookDecision, get_hooks

# Per-request override: set by chat.py when frontend model picker selects Gemini.
# ContextVar so concurrent requests don't stomp each other.
force_cloud_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "force_cloud_var", default=False
)

# Who the LLM calls made in this context are for, stamped on every LLMRequest so the
# usage ledger can attribute them. Set per turn by chat.py via attribute_llm_calls.
current_user_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_current_user", default=None
)
current_session_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_current_session", default=None
)


def attribute_llm_calls(user_id: str | None, session_id: str | None) -> Callable[[], None]:
    """Attribute every LLM call made from the current context to this user and session.

    Tasks started from the context afterwards copy the attribution. Returns the undo,
    which restores what was there and must be called once, from this same context
    (a ``ContextVar`` reset raises anywhere else).
    """
    user_token = current_user_var.set(user_id)
    session_token = current_session_var.set(session_id)

    def undo() -> None:
        current_session_var.reset(session_token)
        current_user_var.reset(user_token)

    return undo


class PreCloudLLMRedactor:
    """Adapts the PreCloudLLM hook (L8 PII gate) to the provider chain's ``Redactor`` port.

    Fails closed: a hook that denies the send, or asks first, stops the text from
    leaving the machine. ``RedactingProvider`` sits outside ``RetryingProvider``, so
    the refusal is never retried; the chain tries the next cloud provider, whose gate
    refuses the same way.
    """

    def __init__(self, hooks: ActionHooks) -> None:
        self._hooks = hooks

    async def redact(self, text: str) -> tuple[str, int]:
        result = await self._hooks.execute("PreCloudLLM", prompt=text)
        if result.decision in (HookDecision.DENY, HookDecision.ASK):
            raise LLMUnavailableError(
                "This request cannot be sent to a cloud model.",
                details={"hook_decision": result.decision.value},
            )
        if result.decision != HookDecision.MODIFY or not result.modified_input:
            return text, 0
        redacted = result.modified_input.get("prompt", text)
        # A handler that changed the text but did not count its spans still counts once.
        return redacted, result.redactions or int(redacted != text)


def build_chain(cfg: ModuleType, hooks: ActionHooks, bus: EventBus) -> FallbackChainProvider:
    """Build the provider chain: cloud sends gated by ``hooks``' PreCloudLLM, usage published on ``bus``.

    The one recipe for the chain. The composition root (``AppContainer.build``) and
    ``get_chain``'s lazy default both use it, so they cannot drift apart.
    """
    return LLMProviderFactory.build(cfg, PreCloudLLMRedactor(hooks), bus)


def get_chain() -> FallbackChainProvider:
    """Return the process-wide provider chain, building the default one on first use.

    The lazy default (no container: tests, scripts) gates cloud sends with the
    process-wide hooks and publishes usage nowhere. At startup the composition root
    installs its own chain, metered onto its event bus, with ``set_chain``.
    """
    chain = current_chain()
    if chain is None:
        chain = build_chain(_cfg, get_hooks(), NullEventBus())
        set_chain(chain)
    return chain


class ModelRole(str, Enum):
    PRIMARY = "primary"
    FAST = "fast"
    CLOUD = "cloud"


MODEL_ROUTING: dict[str, ModelRole] = {
    "socratic_chunker": ModelRole.PRIMARY,
    "habit_translation": ModelRole.PRIMARY,
    "document_understanding": ModelRole.PRIMARY,
    "research_summarization": ModelRole.PRIMARY,
    "practice_generation": ModelRole.PRIMARY,
    # Heavy structured generation (per-slot minute arithmetic, multi-day offsets),
    # and a wrong slot becomes a hard block in the scheduler.
    "calendar_parsing": ModelRole.PRIMARY,
    "intent_classification": ModelRole.FAST,
    "brain_dump_extraction": ModelRole.FAST,
    "memory_extraction": ModelRole.FAST,
    "voice_of_jarvis": ModelRole.FAST,
    "voice_of_jarvis_synthesis": ModelRole.FAST,
    "goal_validation": ModelRole.FAST,
    "task_edit_parsing": ModelRole.FAST,
    "action_item_extraction": ModelRole.FAST,
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

    ``hooks``, when given, gates this call's cloud sends instead of the chain's own
    PreCloudLLM gate (the process-wide ``get_hooks()``).

    The request carries the user and session ``attribute_llm_calls`` set for this
    context, so the usage ledger knows whom the call was for.

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
        user_id=current_user_var.get(),
        session_id=current_session_var.get(),
    )

    chain = get_chain()
    gate = nullcontext() if hooks is None else redacting_with(PreCloudLLMRedactor(hooks))
    try:
        with gate:
            if stream:
                return await _open_stream(chain, req, skip_local)
            return await chain.complete(req, skip_local=skip_local)
    except LLMUnavailableError as exc:
        if not chain.has_available(LLMLocation.CLOUD):
            raise RuntimeError(f"Local LLM failed for {task} and no GEMINI_API_KEY set") from exc
        raise
