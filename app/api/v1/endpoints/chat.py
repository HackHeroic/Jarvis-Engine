"""Unified chat endpoint: single entry point for Control Policy orchestration."""

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.schemas.context import ChatResponse
from app.services.analytical.control_policy import execute_agentic_flow

router = APIRouter()


class ChatRequest(BaseModel):
    """Request body for POST /chat."""

    user_prompt: str = Field(..., description="The user's message or goal")
    user_id: str = Field(..., description="User identifier for habits and storage")
    day_start_hour: Optional[int] = Field(
        default=None,
        ge=0,
        le=23,
        description="Override planning day start hour (0-23). Default from config.",
    )


@router.post(
    "/",
    response_model=ChatResponse,
    summary="Unified chat endpoint",
    description=(
        "Single entry point: routes to ingestion or plan-day pipeline via SLM classification. "
        "Returns ChatResponse with intent, message, and pipeline-specific results (schedule, "
        "execution_graph, or ingestion_result)."
    ),
)
async def chat(request: ChatRequest, http_request: Request) -> ChatResponse:
    """Execute the agentic flow: classify intent, then run ingestion or plan-day pipeline."""
    db_client = getattr(http_request.app.state, "db_client", None)
    return await execute_agentic_flow(
        user_prompt=request.user_prompt,
        user_id=request.user_id,
        db_client=db_client,
        day_start_hour_override=request.day_start_hour,
    )
