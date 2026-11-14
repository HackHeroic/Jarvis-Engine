"""Model status endpoint — exposes loaded LM Studio models to frontend."""

from fastapi import APIRouter
from pydantic import BaseModel, Field

import app.core.config as _cfg

router = APIRouter()


class ModelStatus(BaseModel):
    """Which models answer, for the model picker."""

    primary_model: str = Field(description="The local PRIMARY model; '' when none is loaded.")
    fast_model: str = Field(description="The local FAST model; '' when none is loaded.")
    cloud_model: str = Field(description="The cloud model the picker names.")
    gemini_primary: bool = Field(description="Every call goes to the cloud providers.")
    local_available: bool = Field(description="A local model answers first.")


@router.get(
    "/status",
    response_model=ModelStatus,
    summary="Get loaded model info",
    description="Returns which models are loaded in LM Studio and the current routing config.",
)
async def model_status() -> ModelStatus:
    """Return current model routing config for frontend display."""
    local_available = not _cfg.GEMINI_PRIMARY
    return ModelStatus(
        primary_model=_cfg.LOCAL_LLM_MODEL if local_available else "",
        fast_model=_cfg.SLM_ROUTER_MODEL if local_available else "",
        cloud_model=_cfg.GEMINI_MODEL,
        gemini_primary=_cfg.GEMINI_PRIMARY,
        local_available=local_available,
    )
