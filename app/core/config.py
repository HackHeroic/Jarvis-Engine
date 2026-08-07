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

# Gemma 4 model defaults. At startup, detect_loaded_models() probes LM Studio and
# rewrites PRIMARY+FAST to whichever model is actually loaded (Gemma preferred, qwen
# as last resort). Single-model setups work transparently. Setting either env var
# pins that slot — detection will not overwrite it.
LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "openai/google/gemma-4-26b-a4b")
GEMMA_PRIMARY_MODEL: str = os.getenv("GEMMA_PRIMARY_MODEL", "openai/google/gemma-4-26b-a4b")
GEMMA_FAST_MODEL: str = os.getenv("GEMMA_FAST_MODEL", "openai/google/gemma-4-e4b")
# An explicitly pinned model name outranks startup detection (design D7): the user
# chose it deliberately, so detect_loaded_models() must leave that slot alone.
# A non-empty value is the pin — captured here, the single env-var reader. An empty
# env var (GEMMA_PRIMARY_MODEL=) reads as unset, so detection still fills the slot.
_PRIMARY_PINNED: bool = bool(os.getenv("GEMMA_PRIMARY_MODEL"))
_FAST_PINNED: bool = bool(os.getenv("GEMMA_FAST_MODEL"))
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_raw_gemini = os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash")
GEMINI_MODEL: str = _raw_gemini if _raw_gemini.startswith("gemini/") else f"gemini/{_raw_gemini}"
GEMINI_PRIMARY: bool = os.getenv("GEMINI_PRIMARY", "false").lower() == "true"

# SLM for Semantic Router (Level 2 - fast intent detection)
# Uses Gemma fast model when available; falls back to primary at startup probe.
SLM_ROUTER_MODEL: str = os.getenv("SLM_ROUTER_MODEL", GEMMA_FAST_MODEL)
SLM_ROUTER_URL: str | None = os.getenv("SLM_ROUTER_URL")  # Optional; if unset, uses LOCAL_LLM_URL


# Capability-class markers, not pinned model ids — local model churn is ~quarterly
# (Gemma 4 12B beats the older 27B-class at ~8 GB), so match by size class instead.
_HEAVY_MARKERS = ("27b", "26b", "22b", "14b", "12b")
_FAST_MARKERS = ("e4b", "e2b", "-4b", "1b", "3b")


def _select_models(ids: list[str]) -> tuple[str | None, str | None]:
    """Pick (primary, fast) from loaded model ids.

    Gemma family preferred; non-Gemma (qwen) accepted as last resort.
    A lone model serves both roles.
    """
    def _pick(pool: list[str]) -> tuple[str | None, str | None]:
        heavy = next((i for i in pool if any(m in i for m in _HEAVY_MARKERS)), None)
        fast = next((i for i in pool if any(m in i for m in _FAST_MARKERS)), None)
        if heavy and fast:
            return heavy, fast
        if heavy:
            return heavy, heavy
        if fast:
            return fast, fast
        return (pool[0], pool[0]) if pool else (None, None)

    gemmas = [i for i in ids if "gemma" in i.lower()]
    if gemmas:
        return _pick(gemmas)
    others = [i for i in ids if "qwen" in i.lower()]
    return _pick(others)


def _normalize_model_id(model_id: str) -> str:
    """LM Studio returns plain ids (e.g. "google/gemma-4-26b-a4b"); LiteLLM needs "openai/"."""
    return model_id if model_id.startswith("openai/") else f"openai/{model_id}"


def _resolve_models(primary: str, fast: str) -> tuple[str, str]:
    """Apply env pins over the detected (primary, fast) ids.

    Precedence: env pin > detection. Each slot is pinned independently, so pinning
    only the primary still lets detection choose the fast model. Pinned values are
    normalized too — users pin the id LM Studio displays ("google/gemma-4-12b"),
    which LiteLLM cannot route without the "openai/" prefix.
    """
    return (
        _normalize_model_id(GEMMA_PRIMARY_MODEL if _PRIMARY_PINNED else primary),
        _normalize_model_id(GEMMA_FAST_MODEL if _FAST_PINNED else fast),
    )


def detect_loaded_models() -> dict:
    """Probe LM Studio /v1/models and pick the best loaded model for primary + fast.

    Called at app startup. Mutates module globals GEMMA_PRIMARY_MODEL / GEMMA_FAST_MODEL
    / SLM_ROUTER_MODEL / LOCAL_LLM_MODEL to whatever model(s) are actually loaded.

    Selection is delegated to _select_models (pure, testable) and env pins are applied
    by _resolve_models — a slot pinned via env keeps its value. If only one model is
    loaded, both roles point at it. If LM Studio is unreachable, leave defaults.
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
        primary, fast_model = _select_models(ids)
        if primary is None:
            return {"loaded": ids, "warning": "No usable model loaded in LM Studio"}
        GEMMA_PRIMARY_MODEL, GEMMA_FAST_MODEL = _resolve_models(primary, fast_model)
        SLM_ROUTER_MODEL = GEMMA_FAST_MODEL
        LOCAL_LLM_MODEL = GEMMA_PRIMARY_MODEL

        print(f"[Model Detect] primary={primary} fast={fast_model} (from {len(ids)} loaded)")
        if _PRIMARY_PINNED or _FAST_PINNED:
            print(
                f"[Model Detect] env pins override detection → "
                f"primary={GEMMA_PRIMARY_MODEL} fast={GEMMA_FAST_MODEL}"
            )

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
