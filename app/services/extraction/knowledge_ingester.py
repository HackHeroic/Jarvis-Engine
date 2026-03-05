"""Knowledge pipeline: Chunk, embed, store in Vector DB (L4)."""

from dataclasses import dataclass
from typing import Any, Optional

from app.core.config import SLM_ROUTER_MODEL
from app.models.brain.litellm_conf import hybrid_route_query
from pydantic import BaseModel, Field


class ProactiveExtractionSchema(BaseModel):
    """4B SLM output: actionable content from DPP, syllabus, assignments."""

    action_items: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    document_topics: list[str] = Field(
        default_factory=list,
        description="1-5 granular topic tags e.g. Probability, Statistics, Calculus limits",
    )


PROACTIVE_EXTRACTION_PROMPT = """Analyze this document. Is it actionable? (e.g., a Daily Practice Problem set, an assignment, a syllabus, a sample paper).

If yes, extract:
- action_items: list of actionable items (e.g. "Solve Math DPP Chapter 4", "Complete Assignment 2")
- deadlines: any mentioned dates/deadlines
- document_topics: 1-5 granular topic tags that describe the document content (e.g. "Probability", "Statistics", "Calculus limits"). These will be used to match the document to the user's existing study tasks.

If not actionable, return empty lists. Return valid JSON only."""


@dataclass
class KnowledgeIngestionResult:
    """Result of knowledge ingestion."""

    stored_chunk_count: int
    suggested_actions: list[str]
    metadata: dict[str, Any]
    action_items: list[str] = ()
    document_topics: list[str] = ()
    deadlines: list[str] = ()


def _sanitize_chroma_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Mandatory before collection.add(). Chroma crashes on None or invalid types.
    Omit empty arrays (Chroma rejects them)."""
    sanitized: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            v = "" if k in ("source", "intent", "deadline") else 0
        if isinstance(v, list):
            v = [x for x in v if x is not None]
            if not v:
                continue
            v = v[:10]
        sanitized[k] = v
    return sanitized


def _simple_chunk(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Simple token-approximate chunking (chars / 4 ~ tokens)."""
    words = text.split()
    chunks = []
    current = []
    current_len = 0
    for w in words:
        current.append(w)
        current_len += len(w) // 4 + 1
        if current_len >= chunk_size:
            chunks.append(" ".join(current))
            overlap_words = max(0, len(current) - overlap)
            current = current[-overlap:] if overlap > 0 else []
            current_len = sum(len(x) // 4 + 1 for x in current)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _chunk_with_provenance(
    extracted_items: list[dict],
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[tuple[str, dict[str, Any]]]:
    """Chunk items preserving provenance. Each chunk gets Anchor Bbox (first item's bbox)."""
    chunks: list[tuple[str, dict[str, Any]]] = []
    current_text: list[str] = []
    current_len = 0
    first_page_no: int = 0
    first_bbox: list[float] = []

    for item in extracted_items:
        text = item.get("text", "") or ""
        meta = item.get("metadata", {}) or {}
        page_no = meta.get("page_no", 0) or 0
        bbox = meta.get("bbox", []) or []
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            bbox = [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
        else:
            bbox = []

        words = text.split()
        for w in words:
            if not current_text and not first_bbox and bbox:
                first_bbox = bbox
                first_page_no = page_no
            current_text.append(w)
            current_len += len(w) // 4 + 1
            if current_len >= chunk_size:
                chunk_text = " ".join(current_text)
                chunk_meta: dict[str, Any] = {"page_no": first_page_no}
                if first_bbox:
                    chunk_meta["bbox"] = first_bbox
                chunks.append((chunk_text, chunk_meta))
                current_text = current_text[-overlap:] if overlap > 0 else []
                current_len = sum(len(x) // 4 + 1 for x in current_text)
                first_bbox = []
                first_page_no = 0

    if current_text:
        chunk_meta = {"page_no": first_page_no}
        if first_bbox:
            chunk_meta["bbox"] = first_bbox
        chunks.append((" ".join(current_text), chunk_meta))

    return chunks


async def ingest_knowledge(
    extracted_text: Optional[str] = None,
    extracted_items: Optional[list[dict]] = None,
    source: str = "unknown",
    intent: str = "KNOWLEDGE_INGESTION",
    deadline_detected: Optional[str] = None,
) -> KnowledgeIngestionResult:
    """Chunk text and store in Vector DB. Falls back to metadata-only when Chroma unavailable.

    Args:
        extracted_text: Raw text (when no provenance). Use when payload-only.
        extracted_items: List of {text, metadata: {page_no, bbox}} from extract_document_with_provenance.
        source: Source identifier (e.g. "slack", "pdf").
        intent: Intent type.
        deadline_detected: Optional deadline string if detected by LLM.

    Returns:
        KnowledgeIngestionResult with stored_chunk_count, suggested_actions.
    """
    if extracted_items:
        chunk_tuples = _chunk_with_provenance(extracted_items)
        chunks = [c[0] for c in chunk_tuples]
        chunk_metas = [c[1] for c in chunk_tuples]
    else:
        text = extracted_text or ""
        chunks = _simple_chunk(text)
        chunk_metas = [{}] * len(chunks)

    suggested = []

    try:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.Client(Settings(anonymized_telemetry=False))
        collection = client.get_or_create_collection("jarvis_knowledge", metadata={"hnsw:space": "cosine"})

        for i, chunk in enumerate(chunks):
            meta: dict[str, Any] = {
                "source": source,
                "intent": intent,
                "deadline": deadline_detected or "",
            }
            meta.update(chunk_metas[i] if i < len(chunk_metas) else {})
            meta = _sanitize_chroma_metadata(meta)
            collection.add(
                ids=[f"{source}_{i}"],
                documents=[chunk],
                metadatas=[meta],
            )

        suggested.append("generate_quiz")
        if deadline_detected:
            suggested.append("remind_before_deadline")
        suggested.append("use_for_revision")

        # Proactive 4B extraction: action items, document_topics
        full_text = "\n\n".join(chunks)
        proactive = await _run_proactive_extraction(full_text, deadline_detected)
        if proactive.action_items:
            suggested.append("schedule_dpp_review")
        return KnowledgeIngestionResult(
            stored_chunk_count=len(chunks),
            suggested_actions=suggested,
            metadata={"collection": "jarvis_knowledge"},
            action_items=proactive.action_items,
            document_topics=proactive.document_topics,
            deadlines=proactive.deadlines,
        )
    except ImportError:
        proactive = await _run_proactive_extraction(
            "\n\n".join(chunks),
            deadline_detected,
        )
        suggested = ["generate_quiz"]
        if deadline_detected:
            suggested.append("remind_before_deadline")
        if proactive.action_items:
            suggested.append("schedule_dpp_review")
        return KnowledgeIngestionResult(
            stored_chunk_count=len(chunks),
            suggested_actions=suggested,
            metadata={"status": "chunked_only", "chromadb_not_installed": True},
            action_items=proactive.action_items,
            document_topics=proactive.document_topics,
            deadlines=proactive.deadlines,
        )
    except Exception as e:
        return KnowledgeIngestionResult(
            stored_chunk_count=0,
            suggested_actions=[],
            metadata={"error": str(e)},
        )


async def _run_proactive_extraction(
    text: str,
    deadline_detected: Optional[str] = None,
) -> ProactiveExtractionSchema:
    """Run 4B SLM to extract action items, deadlines, document_topics."""
    if not text or len(text.strip()) < 50:
        return ProactiveExtractionSchema()
    try:
        prompt = text.strip()[:16000]  # Cap token count
        if deadline_detected:
            prompt = f"Detected deadline: {deadline_detected}\n\n{prompt}"
        result = await hybrid_route_query(
            user_prompt=prompt,
            system_prompt=PROACTIVE_EXTRACTION_PROMPT,
            response_schema=ProactiveExtractionSchema,
            model_override=SLM_ROUTER_MODEL,
        )
        if isinstance(result, dict):
            return ProactiveExtractionSchema.model_validate(result)
        return ProactiveExtractionSchema.model_validate_json(result)
    except Exception:
        return ProactiveExtractionSchema()
