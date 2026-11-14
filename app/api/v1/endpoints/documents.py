"""Document management endpoints: list, delete, and view chunks for user-ingested documents.

The document and task-material rows have no repository yet (Plan 3 cut Task 6): these
routes read and write ``ingested_documents`` and ``task_materials`` through the app's
Supabase client, a documented leftover of the layering test's raw-table allow-list. No
database is 503 ``persistence_unavailable``; a document that is not the caller's is 404
``not_found``.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_supabase
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.core.jarvis_logger import log_step
from app.domain.errors import NotFoundError, PersistenceError

router = APIRouter()


class DocumentOut(BaseModel):
    """One ingested document, as the documents page shows it."""

    model_config = ConfigDict(extra="allow")  # the row's other columns pass through

    source_id: str = Field(description="The document's id (its chunks carry it too).")
    id: Optional[str] = Field(default=None, description="The row's id.")
    user_id: Optional[str] = Field(default=None, description="The document's owner.")
    file_name: Optional[str] = Field(default=None, description="The uploaded file's name.")
    media_type: Optional[str] = Field(default=None, description="'pdf', 'png', 'jpeg'...")
    document_topics: Optional[list[str]] = Field(
        default=None, description="The topics read from it."
    )
    chunk_count: Optional[int] = Field(default=None, description="How many chunks it became.")
    created_at: Optional[str] = Field(default=None, description="When it was ingested.")
    linked_task_ids: list[str] = Field(
        default_factory=list, description="The caller's tasks it was linked to."
    )


class DocumentListResponse(BaseModel):
    documents: list[DocumentOut] = Field(description="The caller's documents, newest first.")


class DocumentDeleteResponse(BaseModel):
    status: Literal["deleted"] = Field(description="Always 'deleted'.")
    source_id: str = Field(description="The document deleted, with its chunks and links.")


class DocumentChunksResponse(BaseModel):
    source_id: str = Field(description="The document.")
    chunks: list[str] = Field(description="Its extracted text chunks (the caller's only).")
    count: int = Field(ge=0, description="How many chunks.")


@router.get(
    "/",
    response_model=DocumentListResponse,
    summary="List user documents",
    description="Returns all documents ingested by the user, with linked task IDs.",
)
async def list_documents(
    http_request: Request,
    user_id: str = Query(..., description="User identifier"),
) -> DocumentListResponse:
    """List all ingested documents for a user."""
    supabase = require_supabase(http_request)

    try:
        docs_result = (
            supabase.table("ingested_documents")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        logger.error(f"Listing documents failed: {exc!r}")
        raise PersistenceError("Could not load your documents.") from exc

    documents = docs_result.data or []

    # Fetch linked tasks for all documents in one query
    source_ids = [d["source_id"] for d in documents]
    link_map: dict[str, list[str]] = {}
    if source_ids:
        try:
            links_result = (
                supabase.table("task_materials")
                .select("source_id, task_id")
                .eq("user_id", user_id)
                .in_("source_id", source_ids)
                .execute()
            )
            for link in links_result.data or []:
                link_map.setdefault(link["source_id"], []).append(link["task_id"])
        except Exception:
            pass  # Non-fatal: just skip linked tasks

    for doc in documents:
        doc["linked_task_ids"] = link_map.get(doc["source_id"], [])

    return DocumentListResponse(documents=documents)


@router.delete(
    "/{source_id}",
    response_model=DocumentDeleteResponse,
    summary="Delete a document",
    description="Cascade delete: ChromaDB chunks, task_materials links, ingested_documents row.",
)
async def delete_document(
    source_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User identifier"),
) -> DocumentDeleteResponse:
    """Delete a document and all associated data."""
    supabase = require_supabase(http_request)

    log_step("DOC_DELETE", "Deleting document", {"user_id": user_id, "source_id": source_id})

    # 1. Delete chunks from ChromaDB
    try:
        from app.utils.chroma_client import get_chroma_client

        client = get_chroma_client()
        collection = client.get_or_create_collection(
            "jarvis_knowledge", metadata={"hnsw:space": "cosine"}
        )
        # Only the caller's chunks: a source id alone would delete another user's.
        results = collection.get(where={"$and": [{"source_id": source_id}, {"user_id": user_id}]})
        if results["ids"]:
            collection.delete(ids=results["ids"])
            log_step("DOC_DELETE", f"Removed {len(results['ids'])} chunks from ChromaDB")
    except Exception as exc:
        print(f"[Documents] ChromaDB delete failed (non-fatal): {exc}")

    # 2. Delete task-material links
    try:
        supabase.table("task_materials").delete().eq("user_id", user_id).eq("source_id", source_id).execute()
    except Exception as exc:
        print(f"[Documents] task_materials delete failed (non-fatal): {exc}")

    # 3. Delete from ingested_documents
    try:
        result = (
            supabase.table("ingested_documents")
            .delete()
            .eq("user_id", user_id)
            .eq("source_id", source_id)
            .execute()
        )
    except Exception as exc:
        logger.error(f"Deleting the document record failed: {exc!r}")
        raise PersistenceError("Could not delete the document.") from exc
    if not result.data:
        raise NotFoundError("Document not found.")

    return DocumentDeleteResponse(status="deleted", source_id=source_id)


@router.get(
    "/{source_id}/chunks",
    response_model=DocumentChunksResponse,
    summary="Get document chunks",
    description="Returns extracted text chunks for a document from ChromaDB.",
)
async def get_document_chunks(
    source_id: str,
    http_request: Request,
    user_id: str = Query(...),
) -> DocumentChunksResponse:
    """Fetch text chunks for a document from ChromaDB."""
    try:
        from app.utils.chroma_client import get_chroma_client

        client = get_chroma_client()
        collection = client.get_or_create_collection(
            "jarvis_knowledge", metadata={"hnsw:space": "cosine"}
        )
        # Only the caller's chunks: a source id alone would read another user's.
        results = collection.get(where={"$and": [{"source_id": source_id}, {"user_id": user_id}]})
        chunks = results.get("documents", []) if results else []
        return DocumentChunksResponse(source_id=source_id, chunks=chunks, count=len(chunks))
    except Exception as exc:
        logger.error(f"Fetching document chunks failed: {exc!r}")
        raise PersistenceError("Could not load the document's chunks.") from exc
