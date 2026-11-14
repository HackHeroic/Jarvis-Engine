"""Semantic Router endpoint for intent classification and autonomous extraction pipeline."""

import base64
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, require_supabase
from app.domain import errors
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
        from app.core.model_router import route_llm_call

        result = await route_llm_call(
            task="intent_classification",
            prompt=text,
            system_prompt=SEMANTIC_ROUTER_SYSTEM_PROMPT,
            response_schema=IntentClassification,
        )
        return IntentClassification.model_validate(result)
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
async def list_pending_calendar(http_request: Request, user: CurrentUser) -> dict:
    """List the caller's pending calendar updates awaiting approval."""
    supabase = require_supabase(http_request)
    try:
        result = (
            supabase.table("pending_calendar_updates")
            .select("*")
            .eq("user_id", user)
            .eq("status", "pending")
            .execute()
        )
    except Exception as exc:
        raise errors.PersistenceError("Could not reach the database.") from exc
    return {"pending": result.data or []}


def _resolve_pending_calendar(
    supabase: Any, pending_id: str, user: str, status: str
) -> dict:
    """Flip one of the caller's pending rows to ``status``; NotFoundError if none matched.

    Another user's row matches nothing, so it is indistinguishable from a missing one.
    """
    try:
        result = (
            supabase.table("pending_calendar_updates")
            .update({"status": status})
            .eq("id", pending_id)
            .eq("user_id", user)
            .eq("status", "pending")
            .execute()
        )
    except Exception as exc:
        raise errors.PersistenceError("Could not reach the database.") from exc
    if not result.data:
        raise errors.NotFoundError("Pending update not found or already processed.")
    return result.data[0]


@router.post(
    "/pending-calendar/{pending_id}/approve",
    summary="Approve pending calendar update",
)
async def approve_pending_calendar(
    pending_id: str,
    http_request: Request,
    user: CurrentUser,
) -> dict:
    """Approve one of the caller's pending calendar updates. Returns daily_context for schedule."""
    supabase = require_supabase(http_request)
    row = _resolve_pending_calendar(supabase, pending_id, user, "approved")
    slots = row.get("extracted_slots") or []
    daily_context = [TimeSlot.model_validate(s) for s in slots]
    return {
        "status": "approved",
        "daily_context": [s.model_dump() for s in daily_context],
    }


@router.post(
    "/pending-calendar/{pending_id}/reject",
    summary="Reject pending calendar update",
)
async def reject_pending_calendar(
    pending_id: str,
    http_request: Request,
    user: CurrentUser,
) -> dict:
    """Reject one of the caller's pending calendar updates."""
    supabase = require_supabase(http_request)
    _resolve_pending_calendar(supabase, pending_id, user, "rejected")
    return {"status": "rejected"}


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
