"""Autonomous Extraction Orchestrator: routes by intent and runs pipelines."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.domain.entities import GoalDeadlineUpdate
from app.domain.ports import ConstraintRepository, GoalRepository, TaskRepository
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
    """Classify text on the FAST model."""
    from app.core.model_router import route_llm_call

    result = await route_llm_call(
        task="intent_classification",
        prompt=text,
        system_prompt=SEMANTIC_ROUTER_SYSTEM_PROMPT,
        response_schema=IntentClassification,
    )
    return IntentClassification.model_validate(result)


async def _goal_of_first_match(
    tasks: Optional[TaskRepository], user_id: str, matched: list[str]
) -> Optional[str]:
    """The goal of the first matched task the user has; None when none is found."""
    if tasks is None:
        return None
    for task_id in matched:
        try:
            task = await tasks.get(user_id, task_id)
        except Exception:  # a deadline without its goal is still worth keeping
            return None
        if task is not None:
            return task.goal_id
    return None


async def process_ingestion(
    payload: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    media_type: Optional[str] = None,
    user_id: Optional[str] = None,
    db_client=None,
    intent_override: Optional[IntentType] = None,
    file_name: Optional[str] = None,
    constraints: Optional[ConstraintRepository] = None,
    tasks: Optional[TaskRepository] = None,
    goals: Optional[GoalRepository] = None,
) -> IngestionPipelineResult:
    """Run the full autonomous extraction pipeline.

    Args:
        payload: Optional raw text (Slack, email, etc.).
        file_bytes: Optional raw PDF/image bytes.
        media_type: Optional "pdf" or "image" when file_bytes provided.
        user_id: Optional user identifier for behavioral context and storage.
        db_client: Optional Supabase client for the pending calendar, the document
            record and its task links (rows with no repository yet: Plan 3 cut Task 6).
        intent_override: When provided, skip SLM classification and use this intent (for Control Policy).
        constraints: The user's rules (the container's repository): read as calendar
            context, written for a stated preference. None (no database): neither.
        tasks: The user's tasks (the container's repository): a document is linked to
            the ones it matches. None: no links.
        goals: The goals' deadline updates (the container's repository): a deadline the
            document names is stored for the goal of the tasks it matched. None: not stored.

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
        user_context = (
            await get_behavioral_context_for_calendar(constraints, user_id)
            if constraints is not None and user_id
            else ""
        )
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
        source_id = f"ingestion_{uuid.uuid4().hex[:12]}"
        if provenance_items:
            kr = await ingest_knowledge(
                extracted_items=provenance_items,
                source="ingestion",
                intent=classification.intent.value,
                deadline_detected=None,
                source_id=source_id,
                user_id=user_id,
            )
        else:
            kr = await ingest_knowledge(
                extracted_text=text,
                source="ingestion",
                intent=classification.intent.value,
                deadline_detected=None,
                source_id=source_id,
                user_id=user_id,
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
        # Task–Material Linking via embedding similarity (same source_id as ChromaDB)
        matched: list[str] = []
        if user_id and kr.document_topics and tasks is not None:
            matched = await link_document_to_tasks(
                user_id=user_id,
                document_topics=kr.document_topics,
                source_id=source_id,
                tasks=tasks,
                source_type="chunk",
                supabase_client=supabase,
            )
            if matched:
                base_result.knowledge_result["linked_task_ids"] = matched

        # Record in ingested_documents for document management UI
        if user_id and supabase:
            # Upload original file to S3 for later preview (non-fatal)
            s3_key: str | None = None
            if file_bytes and user_id:
                try:
                    import boto3
                    from app.core.config import AWS_BUCKET_NAME, AWS_REGION

                    s3 = boto3.client(
                        "s3",
                        region_name=AWS_REGION,
                    )
                    s3_key = f"documents/{user_id}/{source_id}/{file_name or 'document'}"
                    s3.put_object(
                        Bucket=AWS_BUCKET_NAME,
                        Key=s3_key,
                        Body=file_bytes,
                        ContentType=media_type or "application/octet-stream",
                    )
                    print(f"[Orchestrator] Uploaded to S3: {s3_key}")
                except Exception as e:
                    print(f"[Orchestrator] S3 upload failed (non-fatal): {e}")

            try:
                upsert_row: dict = {
                    "user_id": user_id,
                    "source_id": source_id,
                    "file_name": file_name,
                    "media_type": media_type,
                    "document_topics": kr.document_topics or [],
                    "chunk_count": kr.stored_chunk_count or 0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                if s3_key is not None:
                    upsert_row["s3_key"] = s3_key
                supabase.table("ingested_documents").upsert(
                    upsert_row,
                    on_conflict="user_id,source_id",
                ).execute()
            except Exception as e:
                print(f"[Orchestrator] Insert ingested_documents failed: {e}")

        # Store deadlines as the goal's deadline updates (goal-level persistence)
        if user_id and kr.deadlines and goals is not None:
            from app.utils.deadline_parser import parse_deadline_to_date

            goal_id_val = await _goal_of_first_match(tasks, user_id, matched)
            from app.services.planning.local_time import local_today

            # "Today" is the user's date: in UTC, a deadline due today in India
            # counted as tomorrow's until 05:30.
            plan_date = local_today()
            context_snippet = (
                kr.action_items[0][:200] if kr.action_items
                else (", ".join(kr.document_topics[:3]) if kr.document_topics else None)
            )
            for raw_deadline in kr.deadlines:
                parsed = parse_deadline_to_date(raw_deadline, datetime.now(timezone.utc))
                if parsed and parsed.date() > plan_date:
                    try:
                        await goals.add_deadline_update(user_id, GoalDeadlineUpdate(
                            user_id=user_id,
                            goal_id=goal_id_val,
                            source="ingestion",
                            deadline_date=parsed.date(),
                            deadline_raw=raw_deadline,
                            context_snippet=context_snippet,
                        ))
                    except Exception as e:
                        print(f"[Orchestrator] Storing a deadline update failed: {type(e).__name__}")

    elif classification.intent == IntentType.BEHAVIORAL_CONSTRAINT:
        if constraints is not None and user_id:
            br = await store_behavioral_constraint(
                constraints, text, user_id=user_id, constraint_type="preference"
            )
        else:
            br = {"status": "skipped", "reason": "No database to keep it in."}
        base_result.behavioral_result = br

    elif classification.intent == IntentType.ACTION_ITEM:
        proposal = await propose_action_item(text)
        base_result.action_proposal = proposal

    return base_result
