"""Hardware settings (MPS/MLX), DB secrets, model auto-detection."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root; try multiple paths for robustness (reload/subprocess edge cases)
_project_root = Path(__file__).resolve().parent.parent.parent
_env_candidates = [
    _project_root / ".env",
    Path.cwd() / ".env",
]
for _env_path in _env_candidates:
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)
        break

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

# LLM Routing (Level 9 - LiteLLM Hybrid Router)
# LM Studio: http://127.0.0.1:1234 — both models hit the same server; model name selects which
LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:1234/v1")

# Gemma 4 model defaults — these are env-overridable.
# At startup, detect_loaded_models() probes LM Studio and rewrites both PRIMARY+FAST
# to whichever Gemma is actually loaded. Single-model setups work transparently.
LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "openai/google/gemma-4-26b-a4b")
GEMMA_PRIMARY_MODEL: str = os.getenv("GEMMA_PRIMARY_MODEL", "openai/google/gemma-4-26b-a4b")
GEMMA_FAST_MODEL: str = os.getenv("GEMMA_FAST_MODEL", "openai/google/gemma-4-e4b")
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_raw_gemini = os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash")
GEMINI_MODEL: str = _raw_gemini if _raw_gemini.startswith("gemini/") else f"gemini/{_raw_gemini}"
GEMINI_PRIMARY: bool = os.getenv("GEMINI_PRIMARY", "false").lower() == "true"

# SLM for Semantic Router (Level 2 - fast intent detection)
# Uses Gemma fast model when available; falls back to primary at startup probe.
SLM_ROUTER_MODEL: str = os.getenv("SLM_ROUTER_MODEL", GEMMA_FAST_MODEL)
SLM_ROUTER_URL: str | None = os.getenv("SLM_ROUTER_URL")  # Optional; if unset, uses LOCAL_LLM_URL


def detect_loaded_models() -> dict:
    """Probe LM Studio /v1/models and pick best Gemma for primary + fast.

    Called at app startup. Mutates module globals GEMMA_PRIMARY_MODEL / GEMMA_FAST_MODEL
    / SLM_ROUTER_MODEL / LOCAL_LLM_MODEL to whatever Gemma model(s) are actually loaded.

    Strategy: prefer 26B for primary, prefer E4B for fast. If only one is loaded,
    point both at it. If LM Studio unreachable, leave defaults.
    """
    global GEMMA_PRIMARY_MODEL, GEMMA_FAST_MODEL, SLM_ROUTER_MODEL, LOCAL_LLM_MODEL
    try:
        import httpx
        url = LOCAL_LLM_URL.rstrip("/") + "/models"
        with httpx.Client(timeout=2.0) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json().get("data", [])
        ids = [m.get("id", "").lower() for m in data]
        # Match anything containing 'gemma'
        gemmas = [i for i in ids if "gemma" in i]
        if not gemmas:
            return {"loaded": ids, "warning": "No Gemma model loaded in LM Studio"}

        # Find heaviest (>= 20B in name) and fastest (e4b/e2b/4b)
        heavy = next((i for i in gemmas if any(s in i for s in ["27b", "26b", "22b"])), None)
        fast = next((i for i in gemmas if any(s in i for s in ["e4b", "e2b", "-4b", "1b"])), None)

        if heavy and fast:
            primary, fast_model = heavy, fast
        elif heavy:
            primary, fast_model = heavy, heavy
        elif fast:
            primary, fast_model = fast, fast
        else:
            primary, fast_model = gemmas[0], gemmas[0]

        # LM Studio returns plain ids (e.g. "google/gemma-4-26b-a4b"); LiteLLM needs "openai/" prefix
        def _normalize(model_id: str) -> str:
            return model_id if model_id.startswith("openai/") else f"openai/{model_id}"

        GEMMA_PRIMARY_MODEL = _normalize(primary)
        GEMMA_FAST_MODEL = _normalize(fast_model)
        SLM_ROUTER_MODEL = GEMMA_FAST_MODEL
        LOCAL_LLM_MODEL = GEMMA_PRIMARY_MODEL

        return {
            "loaded": ids,
            "primary": GEMMA_PRIMARY_MODEL,
            "fast": GEMMA_FAST_MODEL,
            "single_model": primary == fast_model,
        }
    except Exception as e:
        return {"loaded": [], "error": str(e), "warning": "LM Studio unreachable; using defaults"}

# LM Studio native API (non-OpenAI endpoint) for direct streaming chat
LM_STUDIO_NATIVE_URL: str = os.getenv("LM_STUDIO_NATIVE_URL", "http://127.0.0.1:1234")

# LiteLLM observability
LITELLM_VERBOSE: bool = os.getenv("LITELLM_VERBOSE", "false").lower() in ("1", "true", "yes")

DRAFT_TTL_SECONDS = 1800  # 30 minutes

# Horizon for scheduling (minutes)
DEFAULT_HORIZON_MINUTES: int = 2880  # 48 hours
MAX_HORIZON_MINUTES: int = 43200  # 30 days (per PDF: month-long planning)
DAY_START_HOUR: int = 8  # Planning day starts at 8 AM (habit translator convention)

# Mental-health safeguard: max scheduled deep-work minutes per day to avoid cramming
MAX_DEEP_WORK_MINUTES_PER_DAY: int = 360  # 6 hours (legacy; pacing module takes precedence)

# Adaptive pacing (research-based; used when user_override not set)
PACING_SUSTAINABLE_MIN_PER_DAY: int = 90  # slack >= 10
PACING_MODERATE_MIN_PER_DAY: int = 120  # slack >= 5
PACING_STANDARD_MIN_PER_DAY: int = 180  # slack >= 3
PACING_MAX_DEEP_WORK_PER_DAY: int = 240  # upper bound 4h
PACING_COGNITIVE_LOAD_HIGH: float = 0.8  # factor when intrinsic_load >= 0.8
PACING_COGNITIVE_LOAD_MED: float = 0.9  # factor when intrinsic_load >= 0.7

# ChromaDB (L4 Vector DB) - Cloud or local
CHROMA_API_KEY: str | None = os.getenv("CHROMA_API_KEY")
CHROMA_TENANT: str | None = os.getenv("CHROMA_TENANT")
CHROMA_DATABASE: str = os.getenv("CHROMA_DATABASE", "Jarvis-Vector-Db")

# AWS S3 (document storage)
AWS_ACCESS_KEY_ID: str | None = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY: str | None = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
AWS_BUCKET_NAME: str = os.getenv("AWS_BUCKET_NAME", "jarvis-dev-0-storage")


def is_feature_enabled(flag: str) -> bool:
    """Check if a feature flag is enabled. Runtime check via env vars.

    Convention: JARVIS_{FLAG_NAME} env var. Default: enabled ("1").
    Set to "0" to disable.
    """
    return os.environ.get(f"JARVIS_{flag}", "1") == "1"
