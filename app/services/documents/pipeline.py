# app/services/documents/pipeline.py
"""Document Intelligence Pipeline — classify → dispatch → memory.

Universal entry point for all document processing. Uses the document
type registry for extensible classification and handling.

Adding a new document type requires ZERO changes to this function.
"""

import logging

from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.document import DocumentClassification
from app.services.documents.registry import document_registry

logger = logging.getLogger(__name__)

CLASSIFICATION_SYSTEM_PROMPT = (
    "You are a document classifier. Classify the document into one of the given types. "
    "Return valid JSON only."
)


async def classify_document(
    extracted_text: str,
    registry_prompt: str,
) -> DocumentClassification:
    """Classify a document using the LLM and registry-generated prompt.

    Args:
        extracted_text: First ~8000 chars of the document text.
        registry_prompt: Classification prompt from document_registry.classification_prompt().

    Returns:
        DocumentClassification with document_type, confidence, topics, etc.
    """
    user_prompt = f"""{registry_prompt}

Document text (first 8000 chars):
{extracted_text[:8000]}

Classify this document. Return JSON with: document_type, confidence (0-1), topics_covered (list), problem_count (int or null), deadline_detected (ISO date or null), difficulty_estimate (0-1 or null)."""

    result = await hybrid_route_query(
        user_prompt=user_prompt,
        system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
        response_schema=DocumentClassification,
        prefer_local=False,  # Classification needs reliability — use Gemini
    )

    if isinstance(result, dict):
        return DocumentClassification.model_validate(result)
    if isinstance(result, DocumentClassification):
        return result
    return DocumentClassification.model_validate_json(str(result))


async def document_intelligence_pipeline(
    user_id: str,
    extracted_text: str,
    source: str,
    source_id: str,
    memory_store=None,
) -> DocumentClassification | None:
    """Universal document processing pipeline.

    Uses the registry framework — no hardcoded type checks.
    Adding a new document type requires ZERO changes to this function.

    Steps:
    1. Classify using registry-generated prompt
    2. Look up handler from registry
    3. Execute handler
    4. Trigger replan if handler metadata says so
    5. Store ingestion event as memory

    Args:
        user_id: Owner of the document.
        extracted_text: Text extracted from the document (via Docling or raw).
        source: Where the document came from ("direct_upload", "slack", "email", "api").
        source_id: Unique ID for this document.
        memory_store: Optional MemoryStore instance for storing ingestion memory.

    Returns:
        DocumentClassification result, or None if classification failed.
    """
    try:
        # 1. Classify using registry-generated prompt
        registry_prompt = document_registry.classification_prompt()
        classification = await classify_document(extracted_text, registry_prompt)

        # 2. Look up handler from registry (falls back to "reference")
        entry = document_registry.get_or_fallback(classification.document_type)

        # 3. Execute handler
        extraction = {"classification": classification}
        await entry.handler(user_id, extraction, source_id)

        # 4. Store ingestion event as memory
        if memory_store:
            topics_str = ", ".join(classification.topics_covered[:5])
            memory_store.store_memory(user_id, {
                "type": "fact",
                "content": f"Uploaded {classification.document_type}: topics {topics_str}",
                "source": "ingestion",
                "source_id": source_id,
            })

        return classification

    except Exception as e:
        logger.warning("Document intelligence pipeline failed: %s", e)
        return None
