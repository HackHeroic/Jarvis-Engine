"""Model status endpoint — exposes loaded LM Studio models to frontend."""

from fastapi import APIRouter

import app.core.config as _cfg

router = APIRouter()


@router.get(
    "/status",
    summary="Get loaded model info",
    description="Returns which models are loaded in LM Studio and the current routing config.",
)
async def model_status():
    """Return current model routing config for frontend display."""
    return {
        "primary_model": _cfg.LOCAL_LLM_MODEL,
        "fast_model": _cfg.SLM_ROUTER_MODEL,
        "cloud_model": _cfg.GEMINI_MODEL,
        "gemini_primary": _cfg.GEMINI_PRIMARY,
        "local_available": not _cfg.GEMINI_PRIMARY,
    }
