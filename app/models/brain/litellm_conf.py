"""Hybrid routing logic for LiteLLM: Local Qwen (LM Studio) vs Cloud Gemini."""

import os

import litellm
from pydantic import BaseModel

from app.core.config import (
    LOCAL_LLM_URL,
    LOCAL_LLM_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LITELLM_VERBOSE,
)

# Keyword-based routing: queries containing these go to Cloud Gemini
CLOUD_KEYWORDS = [
    "latest news",
    "current events",
    "search the web",
    "real-time",
    "recent developments",
]

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
) -> str | dict:
    """
    Route the query to Local Qwen or Cloud Gemini based on keyword detection.
    Returns raw content string, or parsed dict when response_schema is provided.
    """
    # Keyword-based routing (case-insensitive)
    prompt_lower = user_prompt.lower()
    use_cloud = any(kw in prompt_lower for kw in CLOUD_KEYWORDS)
    target_model = GEMINI_MODEL if use_cloud else LOCAL_LLM_MODEL

    # Routing decision log
    if use_cloud:
        print("[LiteLLM Router] Routing to Cloud Gemini")
    else:
        print("[LiteLLM Router] Routing to Local Qwen")

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

    if use_cloud:
        if GEMINI_API_KEY:
            completion_kwargs["api_key"] = GEMINI_API_KEY
    else:
        completion_kwargs["api_base"] = LOCAL_LLM_URL
        completion_kwargs["api_key"] = "lm-studio"  # Dummy key; LM Studio doesn't validate it

    response = await litellm.acompletion(**completion_kwargs)
    content = response.choices[0].message.content

    if response_schema is not None and content:
        parsed = response_schema.model_validate_json(content)
        return parsed.model_dump()
    return content
