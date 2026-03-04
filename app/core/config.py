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
LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "openai/qwen-local")
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_raw_gemini = os.getenv("GEMINI_MODEL", "gemini/gemini-1.5-pro")
GEMINI_MODEL: str = _raw_gemini if _raw_gemini.startswith("gemini/") else f"gemini/{_raw_gemini}"

# LiteLLM observability
LITELLM_VERBOSE: bool = os.getenv("LITELLM_VERBOSE", "false").lower() in ("1", "true", "yes")
