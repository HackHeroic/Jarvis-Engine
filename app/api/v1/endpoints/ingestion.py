"""Semantic Router endpoint for intent classification and autonomous extraction pipeline."""

import base64
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.config import SLM_ROUTER_MODEL
from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.context import IngestionPipelineResult, IntentClassification, TimeSlot
from app.services.extraction.behavioral_store import delete_behavioral_constraint
from app.services.extraction.orchestrator import process_ingestion
from app.utils.docling_helper import extract_document

SEMANTIC_ROUTER_SYSTEM_PROMPT = (
    "You are the Jarvis Semantic Router. You process data for CEOs, students, "
    "and founders alike.\n\n"
    "Classify the incoming content into one of: CALENDAR_SYNC, KNOWLEDGE_INGESTION, "
    "BEHAVIORAL_CONSTRAINT, or ACTION_ITEM.\n\n"
    "Rules:\n"
    "- CALENDAR_SYNC: Timetables, meeting schedules, board meetings, class schedules, "
    "flight times, deep-work blocks.\n"
    "- KNOWLEDGE_INGESTION: Syllabi, DPP, sample papers, business plans, study materials, "
    "financial PDFs.\n"
    "- BEHAVIORAL_CONSTRAINT: 'I sit in back bench', 'no meetings before 10', "
    "work preferences, habits.\n"
    "- ACTION_ITEM: 'Apply for internship', 'prepare pitch', tasks with deadlines, "
    "direct goals to be scheduled.\n\n"
    "If multiple intents, choose the dominant one. Return strictly valid JSON."
)

router = APIRouter()


class IngestionRequest(BaseModel):
    """Request body for the ingest endpoint."""

    payload: str = Field(
        default="",
        description="Unstructured text to classify (required if no file)",
    )
    file_base64: Optional[str] = Field(
        default=None,
        description="Base64-encoded PDF or image (PNG, JPEG)",
    )
    media_type: Optional[str] = Field(
        default=None,
        description="One of 'pdf', 'image', 'png', 'jpeg'. Required when file_base64 is set.",
    )


def _get_text_for_classification(request: IngestionRequest) -> str:
    """Extract or combine text for SLM classification."""
    parts = []
    if request.payload:
        parts.append(request.payload.strip())
    if request.file_base64 and request.media_type:
        try:
            raw_bytes = base64.b64decode(request.file_base64)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid base64 in file_base64: {e!s}",
            ) from e
        extracted = extract_document(raw_bytes, request.media_type)
        parts.append(extracted)
    combined = "\n\n".join(p for p in parts if p)
    if not combined:
        raise HTTPException(
            status_code=400,
            detail="Provide either payload (text) or file_base64 with media_type.",
        )
    return combined


@router.post(
    "/ingest",
    response_model=IntentClassification,
    summary="Semantic Router: Classify unstructured data",
    description=(
        "Accepts text and/or file (PDF, image). Extracts text via Docling when file "
        "provided, then classifies into CALENDAR_SYNC, KNOWLEDGE_INGESTION, "
        "BEHAVIORAL_CONSTRAINT, or ACTION_ITEM using fast SLM."
    ),
)
async def ingest(request: IngestionRequest) -> IntentClassification:
    """Classify incoming unstructured data via the Semantic Router (SLM)."""
    if request.file_base64 and not request.media_type:
        raise HTTPException(
            status_code=400,
            detail="media_type is required when file_base64 is provided.",
        )
    text = _get_text_for_classification(request)
    try:
        result = await hybrid_route_query(
            user_prompt=text,
            system_prompt=SEMANTIC_ROUTER_SYSTEM_PROMPT,
            response_schema=IntentClassification,
            model_override=SLM_ROUTER_MODEL,
        )
        if isinstance(result, dict):
            return IntentClassification.model_validate(result)
        return IntentClassification.model_validate_json(result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Semantic Router failed to classify the payload. {exc!s}",
        ) from exc


class ProcessIngestionRequest(IngestionRequest):
    """Request for full autonomous pipeline. Extends IngestionRequest."""

    user_id: Optional[str] = Field(default=None, description="Optional user identifier")


@router.post(
    "/process",
    response_model=IngestionPipelineResult,
    summary="Autonomous extraction pipeline",
    description=(
        "Runs full pipeline: extract text, classify, route by intent, run extraction. "
        "Returns calendar slots, knowledge result, behavioral storage, or action proposal."
    ),
)
async def process_full_pipeline(
    request: ProcessIngestionRequest,
    http_request: Request,
) -> IngestionPipelineResult:
    """Run the autonomous extraction pipeline."""
    if request.file_base64 and not request.media_type:
        raise HTTPException(
            status_code=400,
            detail="media_type is required when file_base64 is provided.",
        )
    file_bytes = None
    if request.file_base64 and request.media_type:
        try:
            file_bytes = base64.b64decode(request.file_base64)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64: {e!s}") from e
    db_client = getattr(http_request.app.state, "db_client", None)
    return await process_ingestion(
        payload=request.payload or None,
        file_bytes=file_bytes,
        media_type=request.media_type,
        user_id=request.user_id,
        db_client=db_client,
    )


@router.get(
    "/pending-calendar",
    summary="List pending calendar updates",
)
async def list_pending_calendar(
    http_request: Request,
    user_id: Optional[str] = None,
) -> dict:
    """List pending calendar updates for approval."""
    db_client = getattr(http_request.app.state, "db_client", None) if http_request else None
    if not db_client or not hasattr(db_client, "supabase"):
        return {"pending": []}
    supabase = db_client.supabase
    try:
        query = supabase.table("pending_calendar_updates").select("*").eq("status", "pending")
        if user_id:
            query = query.eq("user_id", user_id)
        result = query.execute()
        return {"pending": result.data or []}
    except Exception:
        return {"pending": []}


@router.post(
    "/pending-calendar/{pending_id}/approve",
    summary="Approve pending calendar update",
)
async def approve_pending_calendar(
    pending_id: str,
    http_request: Request,
) -> dict:
    """Approve a pending calendar update. Returns daily_context for schedule."""
    db_client = getattr(http_request.app.state, "db_client", None)
    if not db_client or not hasattr(db_client, "supabase"):
        raise HTTPException(status_code=503, detail="Database not available")
    supabase = db_client.supabase
    try:
        result = (
            supabase.table("pending_calendar_updates")
            .update({"status": "approved"})
            .eq("id", pending_id)
            .eq("status", "pending")
            .select("*")
            .execute()
        )
        if not result.data or len(result.data) == 0:
            raise HTTPException(status_code=404, detail="Pending update not found or already processed")
        row = result.data[0]
        slots = row.get("extracted_slots", [])
        daily_context = [TimeSlot.model_validate(s) for s in slots]
        return {
            "status": "approved",
            "daily_context": [s.model_dump() for s in daily_context],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/pending-calendar/{pending_id}/reject",
    summary="Reject pending calendar update",
)
async def reject_pending_calendar(
    pending_id: str,
    http_request: Request,
) -> dict:
    """Reject a pending calendar update."""
    db_client = getattr(http_request.app.state, "db_client", None)
    if not db_client or not hasattr(db_client, "supabase"):
        raise HTTPException(status_code=503, detail="Database not available")
    supabase = db_client.supabase
    try:
        result = (
            supabase.table("pending_calendar_updates")
            .update({"status": "rejected"})
            .eq("id", pending_id)
            .eq("status", "pending")
            .select("*")
            .execute()
        )
        if not result.data or len(result.data) == 0:
            raise HTTPException(status_code=404, detail="Pending update not found or already processed")
        return {"status": "rejected"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete(
    "/behavioral-constraint/{constraint_id}",
    summary="Delete a behavioral constraint",
)
async def delete_behavioral_constraint_endpoint(
    constraint_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User ID (required for IDOR protection)"),
) -> dict:
    """Delete a behavioral constraint (habit/preference) by ID. Requires user_id for IDOR protection."""
    if not user_id:
        raise HTTPException(status_code=401, detail="user_id is required for deletion")
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")
    result = await delete_behavioral_constraint(
        constraint_id=constraint_id,
        user_id=user_id,
        supabase_client=supabase,
    )
    if result.get("status") == "error":
        reason = result.get("reason", "")
        if "not found" in reason.lower():
            raise HTTPException(status_code=404, detail="Constraint not found")
        if "not configured" in reason.lower():
            raise HTTPException(status_code=503, detail="Database not available")
        raise HTTPException(status_code=500, detail=reason or "Deletion failed")
    return result
