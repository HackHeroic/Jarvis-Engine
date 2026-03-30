"""Hybrid routing logic for LiteLLM: Local Qwen (LM Studio) vs Cloud Gemini."""

import json
import logging
import os
import re
from collections.abc import AsyncGenerator

import litellm
from pydantic import BaseModel

from app.core.config import (
    LOCAL_LLM_URL,
    LOCAL_LLM_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_PRIMARY,
    LITELLM_VERBOSE,
    SLM_ROUTER_MODEL,
    SLM_ROUTER_URL,
)

logger = logging.getLogger(__name__)

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
    model_override: str | None = None,
    stream: bool = False,
    conversation_history: list[dict] | None = None,
    prefer_local: bool = False,
) -> str | dict | AsyncGenerator[str, None]:
    """
    Route the query to Local Qwen or Cloud Gemini. Local-First: all requests
    go to local Qwen unless (a) CLOUD_KEYWORDS match (Real-Time Research L9),
    or (b) force_cloud=True (last-resort fallback when local fails validation).
    If model_override is set, bypass routing and use that model (e.g. SLM for Semantic Router).
    """
    if model_override is not None:
        target_model = model_override
        use_cloud = False
        print(f"[LiteLLM Router] Using model_override: {model_override}")
    else:
        # Gemini-primary routing: use cloud for schema-critical tasks
        if (
            model_override is None
            and GEMINI_PRIMARY
            and not prefer_local
            and not force_cloud
            and GEMINI_API_KEY
            and response_schema is not None
        ):
            force_cloud = True

        prompt_lower = user_prompt.lower()
        use_cloud = force_cloud or any(kw in prompt_lower for kw in CLOUD_KEYWORDS)
        target_model = GEMINI_MODEL if use_cloud else LOCAL_LLM_MODEL

        if use_cloud:
            reason = "force_cloud fallback" if force_cloud else "Real-Time Research (L9)"
            print(f"[LiteLLM Router] Routing to Cloud Gemini ({reason})")
        else:
            print("[LiteLLM Router] Routing to Local Qwen (Local-First)")

    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_prompt})

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
        if model_override is not None and SLM_ROUTER_URL:
            completion_kwargs["api_base"] = SLM_ROUTER_URL
        elif model_override is not None and "ollama/" not in model_override:
            completion_kwargs["api_base"] = LOCAL_LLM_URL  # LM Studio or custom
        elif model_override is None:
            completion_kwargs["api_base"] = LOCAL_LLM_URL
        # else: model_override with ollama/ and no SLM_ROUTER_URL — LiteLLM uses Ollama default
        completion_kwargs["api_key"] = "lm-studio"  # Dummy key; LM Studio/Ollama don't validate it

    # Streaming path: only for unstructured calls (no response_schema)
    # Yields (event_type, token) tuples where event_type is "reasoning" or "content"
    if stream and response_schema is None:
        completion_kwargs["stream"] = True
        stream_response = await litellm.acompletion(**completion_kwargs)

        async def _token_gen() -> AsyncGenerator[tuple[str, str], None]:
            in_think = False   # are we currently inside a <think> block?
            tag_buf = ""       # incomplete tag fragment buffered across chunk boundary

            async for chunk in stream_response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Primary: LiteLLM standardized reasoning_content (LM Studio native separation)
                reasoning = getattr(delta, "reasoning_content", None) or ""
                raw_content = delta.content or ""

                if reasoning:
                    in_think = False
                    tag_buf = ""
                    yield ("reasoning", reasoning)
                    continue

                if not raw_content:
                    continue

                # Fallback: detect <think>…</think> embedded in content stream.
                # Qwen models emit the thinking block as content when the OpenAI-compatible
                # endpoint doesn't separate reasoning_content.
                text = tag_buf + raw_content
                tag_buf = ""

                while text:
                    if in_think:
                        end = text.lower().find("</think>")
                        if end == -1:
                            # Buffer a potential partial closing tag at the chunk boundary
                            possible = ""
                            for n in range(min(8, len(text)), 0, -1):
                                if "</think>"[:n].lower() == text[-n:].lower():
                                    possible = text[-n:]
                                    break
                            if possible:
                                tag_buf = possible
                                text = text[:-len(possible)]
                            if text:
                                yield ("reasoning", text)
                            text = ""
                        else:
                            if end > 0:
                                yield ("reasoning", text[:end])
                            in_think = False
                            text = text[end + 8:]   # skip </think>
                    else:
                        start = text.lower().find("<think>")
                        if start == -1:
                            # Buffer a potential partial opening tag at the chunk boundary
                            possible = ""
                            for n in range(min(7, len(text)), 0, -1):
                                if "<think>"[:n].lower() == text[-n:].lower():
                                    possible = text[-n:]
                                    break
                            if possible:
                                tag_buf = possible
                                text = text[:-len(possible)]
                            if text:
                                yield ("content", text)
                            text = ""
                        else:
                            if start > 0:
                                yield ("content", text[:start])
                            in_think = True
                            text = text[start + 7:]   # skip <think>

        return _token_gen()

    response = await litellm.acompletion(**completion_kwargs)
    content = response.choices[0].message.content

    # Guard: empty LLM response when structured output expected
    if response_schema is not None and not content:
        if not force_cloud and model_override is None and GEMINI_API_KEY:
            print(f"[LiteLLM Router] Empty response from local model for {response_schema.__name__}, retrying with cloud")
            return await hybrid_route_query(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                response_schema=response_schema,
                force_cloud=True,
                lenient_validation=lenient_validation,
            )
        raise ValueError(f"LLM returned empty response for {response_schema.__name__}")

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


async def gemini_primary_route(
    user_prompt: str,
    system_prompt: str,
    response_schema: type[BaseModel] | None = None,
    fallback_model: str | None = None,
    stream: bool = False,
    conversation_history: list[dict] | None = None,
) -> str | dict:
    """Route to Gemini 2.5 Flash primary. Fall back to local on failure."""
    if GEMINI_API_KEY:
        try:
            return await hybrid_route_query(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                response_schema=response_schema,
                force_cloud=True,
                stream=stream,
                conversation_history=conversation_history,
            )
        except Exception as e:
            logger.warning("Gemini primary failed, falling back to local: %s", e)
    return await hybrid_route_query(
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        response_schema=response_schema,
        model_override=fallback_model or SLM_ROUTER_MODEL,
        stream=stream,
        conversation_history=conversation_history,
    )


async def local_primary_route(
    user_prompt: str,
    system_prompt: str,
    response_schema: type[BaseModel] | None = None,
    model_override: str | None = None,
    stream: bool = False,
    conversation_history: list[dict] | None = None,
) -> str | dict:
    """Route to local Qwen-4B SLM primary. Fall back to Gemini on failure."""
    target = model_override or SLM_ROUTER_MODEL
    try:
        return await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            response_schema=response_schema,
            model_override=target,
            stream=stream,
            conversation_history=conversation_history,
        )
    except Exception as e:
        logger.warning("Local primary (%s) failed, falling back to Gemini: %s", target, e)
        if GEMINI_API_KEY:
            return await hybrid_route_query(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                response_schema=response_schema,
                force_cloud=True,
                stream=stream,
                conversation_history=conversation_history,
            )
        raise


async def run_deep_research(queries: list[str]) -> dict:
    """Run Deep Research (L9) for each query via Gemini. Returns dict with queries and summaries.

    Used when BrainDumpExtraction identifies search_queries. Runs with force_cloud=True.
    """
    if not queries:
        return {"queries": [], "summaries": []}
    if not GEMINI_API_KEY:
        return {"queries": queries, "summaries": ["(Gemini not configured for search)"] * len(queries)}

    summaries: list[str] = []
    for q in queries:
        try:
            prompt = (
                f"Provide a concise 2-3 sentence summary of the latest information on: {q}. "
                "Focus on recent developments, current status, or key facts. Be factual and brief."
            )
            result = await hybrid_route_query(
                user_prompt=prompt,
                system_prompt="You are a research assistant. Summarize the requested topic concisely.",
                response_schema=None,
                force_cloud=True,
            )
            summary = result if isinstance(result, str) else str(result)
            summaries.append(summary.strip() if summary else "(No summary)")
        except Exception as e:
            print(f"[Deep Research] Failed for query '{q}': {e}")
            summaries.append(f"(Search failed: {e})")

    return {"queries": queries, "summaries": summaries}


async def gemini_web_search(
    user_prompt: str,
    system_prompt: str = "You are a helpful research assistant. Return factual information with real URLs when relevant.",
) -> str:
    """Run Gemini with Google Search grounding for real-time web results (L9).

    Used by Proactive Task Workspace for YouTube/article/LeetCode link surfacing.
    Requires GEMINI_API_KEY. Falls back to empty string if unavailable.
    """
    if not GEMINI_API_KEY:
        return ""
    try:
        completion_kwargs = {
            "model": GEMINI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "api_key": GEMINI_API_KEY,
            "temperature": 0.1,
            "web_search_options": {"search_context_size": "medium"},
        }
        response = await litellm.acompletion(**completion_kwargs)
        content = response.choices[0].message.content
        return content.strip() if content else ""
    except Exception as e:
        print(f"[Gemini Web Search] Failed: {e}")
        return ""
