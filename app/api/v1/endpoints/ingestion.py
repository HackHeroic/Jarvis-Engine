"""Semantic Router endpoint for intent classification and autonomous extraction pipeline.

The pending-calendar rows have no repository yet (Plan 3 cut Task 6): those routes use
the app's Supabase client, a documented leftover of the layering test's raw-table
allow-list. A malformed request is 422 ``validation_error``; a classifier that cannot
answer is 503 ``llm_unavailable``.
"""

import base64
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import CurrentUser, container_of, require_supabase
from app.domain import errors
from app.schemas.context import IngestionPipelineResult, IntentClassification, TimeSlot
from app.schemas.upload import UploadMediaType, within_upload_limit
from app.services.extraction.orchestrator import process_ingestion
from app.utils.docling_helper import read_document

logger = logging.getLogger(__name__)

# Fixed texts: an exception's own message never goes on the wire (it is logged).
_BAD_BASE64 = "file_base64 is not valid base64."
_MEDIA_TYPE_REQUIRED = "media_type is required when file_base64 is provided."
_NOTHING_TO_READ = "Provide either payload (text) or file_base64 with media_type."
_CLASSIFIER_DOWN = "Could not classify the payload. Try again shortly."

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
    """Request body for the ingest endpoint. A file has the chat upload's bounds: one of
    the kinds the document reader takes, at most ``MAX_CHAT_UPLOAD_BYTES`` decoded."""

    payload: str = Field(
        default="",
        description="Unstructured text to classify (required if no file)",
    )
    file_base64: Optional[str] = Field(
        default=None,
        description="Base64-encoded PDF or image (PNG, JPEG)",
    )
    media_type: Optional[UploadMediaType] = Field(
        default=None,
        description="One of 'pdf', 'image', 'png', 'jpeg'. Required when file_base64 is set.",
    )
    file_name: Optional[str] = Field(
        default=None,
        max_length=255,
        description="The uploaded file's own name, shown in the document list.",
    )

    _file_within_the_limit = field_validator("file_base64")(within_upload_limit)


class PendingCalendarOut(BaseModel):
    """One timetable extraction awaiting the user's approval, as stored."""

    model_config = ConfigDict(extra="allow")  # the row's other columns pass through

    id: str = Field(description="The pending update's id.")
    user_id: Optional[str] = Field(default=None, description="Its owner.")
    extracted_slots: list[dict[str, Any]] = Field(
        default_factory=list, description="The time slots read from the timetable."
    )
    source_summary: Optional[str] = Field(default=None, description="What was read.")
    status: Optional[str] = Field(default=None, description="'pending'.")
    created_at: Optional[str] = Field(default=None, description="When it was extracted.")
    valid_until: Optional[str] = Field(default=None, description="When the timetable ends.")


class PendingCalendarListResponse(BaseModel):
    pending: list[PendingCalendarOut] = Field(description="The caller's pending updates.")


class PendingCalendarApproved(BaseModel):
    status: Literal["approved"] = Field(description="Always 'approved'.")
    daily_context: list[TimeSlot] = Field(
        description="The approved slots, as a schedule's daily context."
    )


class PendingCalendarRejected(BaseModel):
    status: Literal["rejected"] = Field(description="Always 'rejected'.")


async def _get_text_for_classification(request: IngestionRequest) -> str:
    """Extract or combine text for SLM classification."""
    parts = []
    if request.payload:
        parts.append(request.payload.strip())
    if request.file_base64 and request.media_type:
        raw_bytes = _decoded(request.file_base64)
        extracted = await read_document(raw_bytes, request.media_type)
        parts.append(extracted)
    combined = "\n\n".join(p for p in parts if p)
    if not combined:
        raise errors.ValidationError(_NOTHING_TO_READ)
    return combined


def _decoded(file_base64: str) -> bytes:
    """The file's bytes; ValidationError (422, a fixed text) when it is not base64."""
    try:
        return base64.b64decode(file_base64)
    except Exception as exc:
        raise errors.ValidationError(_BAD_BASE64) from exc


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
        raise errors.ValidationError(_MEDIA_TYPE_REQUIRED)
    text = await _get_text_for_classification(request)
    try:
        from app.core.model_router import route_llm_call

        result = await route_llm_call(
            task="intent_classification",
            prompt=text,
            system_prompt=SEMANTIC_ROUTER_SYSTEM_PROMPT,
            response_schema=IntentClassification,
        )
        return IntentClassification.model_validate(result)
    except Exception as exc:
        logger.warning("Semantic Router could not classify the payload", exc_info=exc)
        raise errors.LLMUnavailableError(_CLASSIFIER_DOWN) from exc


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
        raise errors.ValidationError(_MEDIA_TYPE_REQUIRED)
    file_bytes = None
    if request.file_base64 and request.media_type:
        file_bytes = _decoded(request.file_base64)
    db_client = getattr(http_request.app.state, "db_client", None)
    # Rules, tasks, goals and documents are read and stored through the container, and
    # only with a database: without one the container is in-memory and nothing would last.
    container = container_of(http_request)
    has_db = getattr(db_client, "supabase", None) is not None
    repos = container.repos if container is not None and has_db else None
    return await process_ingestion(
        payload=request.payload or None,
        file_bytes=file_bytes,
        media_type=request.media_type,
        user_id=request.user_id,
        db_client=db_client,
        file_name=request.file_name,
        constraints=repos.constraints if repos is not None else None,
        tasks=repos.tasks if repos is not None else None,
        goals=repos.goals if repos is not None else None,
        documents=container.documents if repos is not None else None,
    )


@router.get(
    "/pending-calendar",
    response_model=PendingCalendarListResponse,
    summary="List pending calendar updates",
)
async def list_pending_calendar(
    http_request: Request, user: CurrentUser
) -> PendingCalendarListResponse:
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
    return PendingCalendarListResponse(pending=result.data or [])


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
    response_model=PendingCalendarApproved,
    summary="Approve pending calendar update",
)
async def approve_pending_calendar(
    pending_id: str,
    http_request: Request,
    user: CurrentUser,
) -> PendingCalendarApproved:
    """Approve one of the caller's pending calendar updates. Returns daily_context for schedule."""
    supabase = require_supabase(http_request)
    row = _resolve_pending_calendar(supabase, pending_id, user, "approved")
    slots = row.get("extracted_slots") or []
    daily_context = [TimeSlot.model_validate(s) for s in slots]
    return PendingCalendarApproved(status="approved", daily_context=daily_context)


@router.post(
    "/pending-calendar/{pending_id}/reject",
    response_model=PendingCalendarRejected,
    summary="Reject pending calendar update",
)
async def reject_pending_calendar(
    pending_id: str,
    http_request: Request,
    user: CurrentUser,
) -> PendingCalendarRejected:
    """Reject one of the caller's pending calendar updates."""
    supabase = require_supabase(http_request)
    _resolve_pending_calendar(supabase, pending_id, user, "rejected")
    return PendingCalendarRejected(status="rejected")
