"""Autonomous Extraction Orchestrator: routes by intent and runs pipelines."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core.config import SLM_ROUTER_MODEL
from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.context import (
    ActionItemProposal,
    ExtractedTimeSlots,
    IngestionPipelineResult,
    IntentClassification,
    IntentType,
    PendingCalendarUpdate,
)
from app.services.extraction.action_item_handler import propose_action_item
from app.services.extraction.behavioral_store import get_behavioral_context_for_calendar, store_behavioral_constraint
from app.services.extraction.calendar_extractor import extract_calendar_slots
from app.services.extraction.knowledge_ingester import ingest_knowledge
from app.services.extraction.task_material_linker import link_document_to_tasks
from app.utils.docling_helper import extract_document, extract_document_with_provenance

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


def _extract_text(
    payload: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    media_type: Optional[str] = None,
) -> tuple[str, Optional[list[dict]]]:
    """Extract or combine text for classification and pipelines.
    Returns (text, provenance_items). provenance_items is None when payload-only."""
    parts: list[str] = []
    provenance_items: Optional[list[dict]] = None
    if payload and payload.strip():
        parts.append(payload.strip())
    if file_bytes and media_type:
        items = extract_document_with_provenance(file_bytes, media_type)
        text_from_file = "\n\n".join(i.get("text", "") or "" for i in items)
        parts.append(text_from_file)
        provenance_items = items
        if payload and payload.strip():
            provenance_items = [
                {"text": payload.strip(), "metadata": {"page_no": 0, "bbox": []}},
            ] + provenance_items
    text = "\n\n".join(parts) if parts else ""
    return (text, provenance_items)


async def _classify(text: str) -> IntentClassification:
    """Classify text via SLM."""
    result = await hybrid_route_query(
        user_prompt=text,
        system_prompt=SEMANTIC_ROUTER_SYSTEM_PROMPT,
        response_schema=IntentClassification,
        model_override=SLM_ROUTER_MODEL,
    )
    if isinstance(result, dict):
        return IntentClassification.model_validate(result)
    return IntentClassification.model_validate_json(result)


async def process_ingestion(
    payload: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    media_type: Optional[str] = None,
    user_id: Optional[str] = None,
    db_client=None,
    intent_override: Optional[IntentType] = None,
) -> IngestionPipelineResult:
    """Run the full autonomous extraction pipeline.

    Args:
        payload: Optional raw text (Slack, email, etc.).
        file_bytes: Optional raw PDF/image bytes.
        media_type: Optional "pdf" or "image" when file_bytes provided.
        user_id: Optional user identifier for behavioral context and storage.
        db_client: Optional Supabase client for pending_calendar and behavioral_constraints.
        intent_override: When provided, skip SLM classification and use this intent (for Control Policy).

    Returns:
        IngestionPipelineResult with intent and pipeline-specific results.
    """
    text, provenance_items = _extract_text(payload, file_bytes, media_type)
    if not text:
        return IngestionPipelineResult(
            intent=IntentType.BEHAVIORAL_CONSTRAINT,
            classification_summary="No content to process",
        )

    if intent_override is not None:
        classification = IntentClassification(
            intent=intent_override,
            confidence=1.0,
            summary="",
        )
    else:
        classification = await _classify(text)
    base_result = IngestionPipelineResult(
        intent=classification.intent,
        classification_summary=classification.summary,
    )

    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    if classification.intent == IntentType.CALENDAR_SYNC:
        user_context = await get_behavioral_context_for_calendar(user_id, supabase)
        extracted = await extract_calendar_slots(text, user_context=user_context or None)
        base_result.calendar_result = extracted
        if db_client and supabase:
            try:
                pending_id = str(uuid.uuid4())
                row = {
                    "id": pending_id,
                    "user_id": user_id or "",
                    "extracted_slots": [s.model_dump() for s in extracted.slots],
                    "source_summary": extracted.source_summary,
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                if extracted.valid_until:
                    row["valid_until"] = extracted.valid_until
                supabase.table("pending_calendar_updates").insert(row).execute()
                base_result.pending_calendar_id = pending_id
            except Exception:
                base_result.pending_calendar_id = str(uuid.uuid4())  # Return ID for client to use
        else:
            base_result.pending_calendar_id = str(uuid.uuid4())

    elif classification.intent == IntentType.KNOWLEDGE_INGESTION:
        if provenance_items:
            kr = await ingest_knowledge(
                extracted_items=provenance_items,
                source="ingestion",
                intent=classification.intent.value,
                deadline_detected=None,
            )
        else:
            kr = await ingest_knowledge(
                extracted_text=text,
                source="ingestion",
                intent=classification.intent.value,
                deadline_detected=None,
            )
        base_result.knowledge_result = {
            "stored_chunk_count": kr.stored_chunk_count,
            "suggested_actions": kr.suggested_actions,
            "metadata": kr.metadata,
            "action_items": kr.action_items,
            "document_topics": kr.document_topics,
        }
        # Proactive ActionItemProposal from DPP/syllabus
        if kr.action_items:
            primary = kr.action_items[0]
            base_result.action_proposal = ActionItemProposal(
                id=str(uuid.uuid4()),
                title=primary[:80] + ("..." if len(primary) > 80 else ""),
                summary=primary,
                suggested_actions=kr.suggested_actions,
                deadline_mentioned=bool(kr.deadlines),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        # Task–Material Linking via embedding similarity
        if user_id and kr.document_topics:
            source_id = f"ingestion_{uuid.uuid4().hex[:12]}"
            matched = await link_document_to_tasks(
                user_id=user_id,
                document_topics=kr.document_topics,
                source_id=source_id,
                source_type="chunk",
                supabase_client=supabase,
            )
            if matched:
                base_result.knowledge_result["linked_task_ids"] = matched

    elif classification.intent == IntentType.BEHAVIORAL_CONSTRAINT:
        br = await store_behavioral_constraint(
            raw_text=text,
            constraint_type="preference",
            user_id=user_id,
            supabase_client=supabase,
        )
        base_result.behavioral_result = br

    elif classification.intent == IntentType.ACTION_ITEM:
        proposal = await propose_action_item(text)
        base_result.action_proposal = proposal

    return base_result
