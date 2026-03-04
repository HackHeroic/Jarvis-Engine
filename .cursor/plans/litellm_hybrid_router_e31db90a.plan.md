---
name: LiteLLM Hybrid Router
overview: Implement the Level 9 LiteLLM Hybrid Router with keyword-based routing between local Qwen (LM Studio) and cloud Gemini, config updates, structured output support, and LiteLLM-native observability (callbacks for cost, latency, and success/failure logging).
todos: []
isProject: false
---

# LiteLLM Hybrid Router Implementation Plan (Rev 2)

## Context

Per [docs/POLICY_ENGINE_ARCHITECTURE.md](docs/POLICY_ENGINE_ARCHITECTURE.md), the LiteLLM Router ensures sensitive data is processed locally and offloads high-level research to the cloud. LM Studio provides an OpenAI-compatible API at `http://localhost:1234/v1` (POST /v1/chat/completions).

**Architecture note:** The POLICY_ENGINE shows cloud-bound queries should ideally flow through **L8 PII Filter** before Gemini. This plan implements the routing logic first; L8 is a future enhancement.

---

## 1. Config Updates ([app/core/config.py](app/core/config.py))

Add LLM and LiteLLM observability variables:

```python
# LLM Routing (Level 9 - LiteLLM Hybrid Router)
LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "openai/qwen-local")
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_raw_gemini = os.getenv("GEMINI_MODEL", "gemini/gemini-1.5-pro")
GEMINI_MODEL: str = _raw_gemini if _raw_gemini.startswith("gemini/") else f"gemini/{_raw_gemini}"

# LiteLLM observability
LITELLM_VERBOSE: bool = os.getenv("LITELLM_VERBOSE", "false").lower() in ("1", "true", "yes")
```

---

## 2. Router Implementation ([app/models/brain/litellm_conf.py](app/models/brain/litellm_conf.py))

### Imports

```python
import litellm
from pydantic import BaseModel

from app.core.config import (
    LOCAL_LLM_URL, LOCAL_LLM_MODEL, GEMINI_API_KEY, GEMINI_MODEL,
    LITELLM_VERBOSE,
)
```

### Keyword-Based Routing

```python
CLOUD_KEYWORDS = [
    "latest news", "current events", "search the web",
    "real-time", "recent developments"
]
```

- Case-insensitive: `any(kw in user_prompt.lower() for kw in CLOUD_KEYWORDS)`.
- If matched: `target_model = GEMINI_MODEL`, else: `target_model = LOCAL_LLM_MODEL`.

### Observability (per [LiteLLM Logging docs](https://docs.litellm.ai/docs/#logging-observability---log-llm-inputoutput-docs))

**2a. Verbose mode (module init)**  
Set `litellm.set_verbose = LITELLM_VERBOSE` at import or in a setup function so requests/responses are logged when enabled.

**2b. Async success callback**  
Because we use `acompletion()`, use an async callback registered with `litellm.success_callback`:

```python
async def _hybrid_route_success_callback(kwargs, completion_response, start_time, end_time):
    model = kwargs.get("model", "unknown")
    cost = kwargs.get("response_cost", 0)
    duration_s = (end_time - start_time).total_seconds() if start_time and end_time else 0
    print(f"[LiteLLM] Success | model={model} | cost=${cost:.6f} | latency={duration_s:.2f}s")
```

Register once at module load: `litellm.success_callback = [_hybrid_route_success_callback]`.

**2c. Failure callback**  
Use `litellm.failure_callback` to log errors:

```python
def _hybrid_route_failure_callback(kwargs, completion_response, start_time, end_time):
    model = kwargs.get("model", "unknown")
    print(f"[LiteLLM] Failure | model={model} | error={completion_response}")
```

Set: `litellm.failure_callback = [_hybrid_route_failure_callback]`.

**2d. Routing decision (print)**  
Keep explicit routing logs before the API call:

```python
print("[LiteLLM Router] Routing to Local Qwen")   # or "Cloud Gemini"
```

**Note:** Avoid blocking or heavy I/O in callbacks. For production, consider switching to LiteLLM’s `CustomLogger` and sending to Lunary/MLflow/Langfuse (see [custom callbacks](https://docs.litellm.ai/docs/observability/custom_callback)).

### Async Function

```python
async def hybrid_route_query(
    user_prompt: str,
    system_prompt: str,
    response_schema: type[BaseModel] | None = None
) -> str | dict:
```

### Messages and `acompletion` Call


| Parameter     | Local                  | Cloud            |
| ------------- | ---------------------- | ---------------- |
| `model`       | `LOCAL_LLM_MODEL`      | `GEMINI_MODEL`   |
| `messages`    | `[system, user]`       | Same             |
| `temperature` | `0.1`                  | `0.1`            |
| `api_base`    | `LOCAL_LLM_URL`        | Omit             |
| `api_key`     | Omit or `"not-needed"` | `GEMINI_API_KEY` |


- If `response_schema` is given: `response_format=response_schema`.
- Return: raw content string, or parsed `model_dump()` when `response_schema` is provided.

---

## 3. Module Init Flow

1. Apply `litellm.set_verbose = LITELLM_VERBOSE`.
2. Register `success_callback` and `failure_callback`.
3. Define `hybrid_route_query`.

---

## 4. `.env` Additions (User Action)

```
LOCAL_LLM_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=openai/qwen-local
GEMINI_API_KEY=<key>   # or use GOOGLE_API_KEY
LITELLM_VERBOSE=true   # optional, for debugging
```

---

## 5. Observability Summary


| Source                | Purpose                                   |
| --------------------- | ----------------------------------------- |
| Print (routing)       | Which target (Local Qwen vs Cloud Gemini) |
| `litellm.set_verbose` | Raw request/response for debugging        |
| `success_callback`    | Model, cost, latency on success           |
| `failure_callback`    | Model and error on failure                |


Future: plug in Lunary, MLflow, or Langfuse via `litellm.success_callback = ["lunary", "mlflow", ...]` per [LiteLLM docs](https://docs.litellm.ai/docs/#logging-observability---log-llm-inputoutput-docs).

---

## 6. Files to Modify


| File                                                                 | Changes                                                                                          |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| [app/core/config.py](app/core/config.py)                             | Add `LOCAL_LLM_URL`, `LOCAL_LLM_MODEL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `LITELLM_VERBOSE`      |
| [app/models/brain/litellm_conf.py](app/models/brain/litellm_conf.py) | Implement `hybrid_route_query`, routing, `acompletion`, `response_format`, and LiteLLM callbacks |


