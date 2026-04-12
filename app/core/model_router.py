"""Task-based model routing. Replaces hybrid_route_query().

Local-first always. Gemini fallback only for web research or validation failure.
PII filter hook runs exactly once before any cloud call.
"""

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

import app.core.config as _cfg
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.orchestrator.hooks import ActionHooks, HookDecision


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


def _get_model_for_role(role: ModelRole) -> str:
    """Read config at call time so startup detection is respected."""
    if role == ModelRole.PRIMARY:
        return _cfg.GEMMA_PRIMARY_MODEL
    if role == ModelRole.FAST:
        return _cfg.GEMMA_FAST_MODEL
    return _cfg.GEMINI_MODEL

_FENCE_RE = re.compile(r"```json|```")


def strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


async def route_llm_call(
    task: str,
    prompt: str,
    system_prompt: str = "",
    response_schema: Optional[type[BaseModel]] = None,
    hooks: Optional[ActionHooks] = None,
    conversation_history: Optional[list[dict]] = None,
) -> str | BaseModel:
    """Route LLM call with fallback chain. Local-first always."""
    from app.models.brain.litellm_conf import hybrid_route_query

    if hooks is None:
        from app.orchestrator.hooks import get_hooks
        hooks = get_hooks()

    role = MODEL_ROUTING.get(task, ModelRole.FAST)
    model = _get_model_for_role(role)

    # Skip local attempt if GEMINI_PRIMARY is set (no local models available)
    if role in (ModelRole.PRIMARY, ModelRole.FAST) and not _cfg.GEMINI_PRIMARY:
        try:
            result = await hybrid_route_query(
                user_prompt=prompt,
                system_prompt=system_prompt,
                response_schema=response_schema,
                model_override=model,
                conversation_history=conversation_history,
            )
            if response_schema and isinstance(result, str):
                return response_schema.model_validate_json(strip_fences(result))
            return result
        except (ValidationError, Exception) as e:
            logger.warning(f"Local {model} failed for {task}: {e}")

    if hooks:
        pii_result = await hooks.execute("PreCloudLLM", prompt=prompt)
        if pii_result.decision == HookDecision.MODIFY:
            prompt = pii_result.modified_input["prompt"]

    if not _cfg.GEMINI_API_KEY:
        raise RuntimeError(f"Local LLM failed for {task} and no GEMINI_API_KEY set")

    from app.models.brain.litellm_conf import gemini_primary_route

    result = await gemini_primary_route(
        user_prompt=prompt,
        system_prompt=system_prompt,
        response_schema=response_schema,
        conversation_history=conversation_history,
    )
    if response_schema and isinstance(result, str):
        return response_schema.model_validate_json(strip_fences(result))
    return result
