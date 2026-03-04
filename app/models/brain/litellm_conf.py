"""Hybrid routing logic for LiteLLM: Local Qwen (LM Studio) vs Cloud Gemini."""

import json
import os
import re

import litellm
from pydantic import BaseModel

from app.core.config import (
    LOCAL_LLM_URL,
    LOCAL_LLM_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LITELLM_VERBOSE,
)

# Cloud keywords: Real-Time Research (L9) only. Local-First: all other requests
# (including decomposition, academic topics like SARIMAX) go to local Qwen.
# Cloud Gemini is reserved exclusively for real-time/research queries.
CLOUD_KEYWORDS = [
    "latest news",
    "current events",
    "search the web",
    "real-time",
    "recent developments",
]

# Strip markdown code fences from LLM output (local models often wrap JSON)
_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL,
)


def _sanitize_llm_json(raw: str) -> str:
    """Strip markdown code fences that local models wrap around JSON."""
    stripped = raw.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped

# Use the new environment variable method for LiteLLM logging
if LITELLM_VERBOSE:
    os.environ["LITELLM_LOG"] = "DEBUG"


async def _hybrid_route_success_callback(kwargs, completion_response, start_time, end_time):
    """Log success: model, cost, latency."""
    model = kwargs.get("model", "unknown")

    # Safely handle the cost if LiteLLM returns None for local models
    raw_cost = kwargs.get("response_cost")
    cost = float(raw_cost) if raw_cost is not None else 0.0

    duration_s = (end_time - start_time).total_seconds() if start_time and end_time else 0.0
    print(f"✅ [LiteLLM] Success | model={model} | cost=${cost:.6f} | latency={duration_s:.2f}s")


def _hybrid_route_failure_callback(kwargs, completion_response, start_time, end_time):
    """Log failure: model and error."""
    model = kwargs.get("model", "unknown")
    print(f"[LiteLLM] Failure | model={model} | error={completion_response}")


# Register LiteLLM callbacks
litellm.success_callback = [_hybrid_route_success_callback]
litellm.failure_callback = [_hybrid_route_failure_callback]


async def hybrid_route_query(
    user_prompt: str,
    system_prompt: str,
    response_schema: type[BaseModel] | None = None,
    force_cloud: bool = False,
    lenient_validation: bool = False,
) -> str | dict:
    """
    Route the query to Local Qwen or Cloud Gemini. Local-First: all requests
    go to local Qwen unless (a) CLOUD_KEYWORDS match (Real-Time Research L9),
    or (b) force_cloud=True (last-resort fallback when local fails validation).
    """
    prompt_lower = user_prompt.lower()
    use_cloud = force_cloud or any(kw in prompt_lower for kw in CLOUD_KEYWORDS)
    target_model = GEMINI_MODEL if use_cloud else LOCAL_LLM_MODEL

    if use_cloud:
        reason = "force_cloud fallback" if force_cloud else "Real-Time Research (L9)"
        print(f"[LiteLLM Router] Routing to Cloud Gemini ({reason})")
    else:
        print("[LiteLLM Router] Routing to Local Qwen (Local-First)")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Build completion kwargs
    completion_kwargs: dict = {
        "model": target_model,
        "messages": messages,
        "temperature": 0.1,
    }
    if response_schema is not None:
        completion_kwargs["response_format"] = response_schema
        completion_kwargs["max_tokens"] = 4096

    if use_cloud:
        if GEMINI_API_KEY:
            completion_kwargs["api_key"] = GEMINI_API_KEY
    else:
        completion_kwargs["api_base"] = LOCAL_LLM_URL
        completion_kwargs["api_key"] = "lm-studio"  # Dummy key; LM Studio doesn't validate it

    response = await litellm.acompletion(**completion_kwargs)
    content = response.choices[0].message.content

    if response_schema is not None and content:
        try:
            parsed = response_schema.model_validate_json(content)
            return parsed.model_dump()
        except Exception as e:
            # Retry with sanitized content (local models often wrap JSON in fences)
            content_sanitized = _sanitize_llm_json(content)
            try:
                parsed = response_schema.model_validate_json(content_sanitized)
                return parsed.model_dump()
            except Exception:
                # If lenient, return raw dict for caller to inspect/retry (e.g. undersized decomposition)
                if lenient_validation:
                    return json.loads(content_sanitized)
                raise e
    return content
