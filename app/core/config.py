"""Hardware settings (MPS/MLX), DB secrets."""

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
LOCAL_LLM_MODEL: str = os.getenv(
    "LOCAL_LLM_MODEL", "openai/mlx-community/qwen3.5-27b"
)  # Heavy lifting: decomposition, reasoning
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_raw_gemini = os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash")
GEMINI_MODEL: str = _raw_gemini if _raw_gemini.startswith("gemini/") else f"gemini/{_raw_gemini}"
GEMINI_PRIMARY: bool = os.getenv("GEMINI_PRIMARY", "true").lower() == "true"

# SLM for Semantic Router (Level 2 - fast intent detection, ~100ms)
# Uses qwen3.5-4b for rapid classification; same LM Studio server as LOCAL_LLM
SLM_ROUTER_MODEL: str = os.getenv("SLM_ROUTER_MODEL", "openai/qwen3.5-4b")
SLM_ROUTER_URL: str | None = os.getenv("SLM_ROUTER_URL")  # Optional; if unset, uses LOCAL_LLM_URL

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
