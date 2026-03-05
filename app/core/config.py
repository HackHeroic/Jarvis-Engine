"""Hardware settings (MPS/MLX), DB secrets."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_env_path)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

# LLM Routing (Level 9 - LiteLLM Hybrid Router)
# LM Studio: http://127.0.0.1:1234 — both models hit the same server; model name selects which
LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:1234/v1")
LOCAL_LLM_MODEL: str = os.getenv(
    "LOCAL_LLM_MODEL", "openai/mlx-community/qwen3.5-27b"
)  # Heavy lifting: decomposition, reasoning
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_raw_gemini = os.getenv("GEMINI_MODEL", "gemini/gemini-1.5-pro")
GEMINI_MODEL: str = _raw_gemini if _raw_gemini.startswith("gemini/") else f"gemini/{_raw_gemini}"

# SLM for Semantic Router (Level 2 - fast intent detection, ~100ms)
# Uses qwen3.5-4b for rapid classification; same LM Studio server as LOCAL_LLM
SLM_ROUTER_MODEL: str = os.getenv("SLM_ROUTER_MODEL", "openai/qwen3.5-4b")
SLM_ROUTER_URL: str | None = os.getenv("SLM_ROUTER_URL")  # Optional; if unset, uses LOCAL_LLM_URL

# LiteLLM observability
LITELLM_VERBOSE: bool = os.getenv("LITELLM_VERBOSE", "false").lower() in ("1", "true", "yes")

# Horizon for scheduling (minutes)
DEFAULT_HORIZON_MINUTES: int = 2880  # 48 hours
MAX_HORIZON_MINUTES: int = 43200  # 30 days (per PDF: month-long planning)
DAY_START_HOUR: int = 8  # Planning day starts at 8 AM (habit translator convention)
