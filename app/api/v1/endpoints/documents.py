"""Document management endpoints: list, open, delete, and view the chunks of a document.

Every route but the chunks one goes through the composition root's ``DocumentService``
and first requires the database (``require_supabase``): without it the container runs on
empty in-memory repositories, and a document page would show nothing as if that were
the truth. No database is 503 ``persistence_unavailable``; a document that is not the
caller's is 404 ``not_found``.
"""

from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.api.deps import get_container, require_supabase
from app.api.error_handlers import json_error_responses
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.core.jarvis_logger import log_step
from app.domain.errors import PersistenceError
from app.schemas.documents import (
    DocumentChunksResponse,
    DocumentDeleteResponse,
    DocumentListResponse,
)
from app.services.documents.service import DocumentService
from app.services.documents.uploads import ascii_name

router = APIRouter()


def _documents(request: Request) -> DocumentService:
    require_supabase(request)
    return get_container(request).documents


@router.get(
    "/",
    response_model=DocumentListResponse,
    summary="List user documents",
    description=(
        "The caller's documents, newest first, each with whether its file is kept"
        " (`has_file`) and the tasks it is linked to (`linked_tasks`, with titles)."
    ),
)
async def list_documents(
    http_request: Request,
    user_id: str = Query(..., description="User identifier"),
) -> DocumentListResponse:
    """List all ingested documents for a user."""
    documents = await _documents(http_request).list_documents(user_id)
    return DocumentListResponse(documents=documents)


@router.get(
    "/{source_id}/file",
    summary="Open a document's file",
    description=(
        "The uploaded file itself, as it was uploaded, shown inline (`Content-Disposition:"
        " inline`). 404 `not_found` when the caller has no such document or its file was"
        " not kept (pasted text); 503 `persistence_unavailable` when the file store cannot"
        " be reached."
    ),
    response_class=Response,
    responses={
        200: {
            "description": "The file's bytes, with the content type it was uploaded as.",
            "content": {
                "application/pdf": {"schema": {"type": "string", "format": "binary"}},
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        **json_error_responses(),
    },
)
async def get_document_file(
    source_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User identifier"),
) -> Response:
    """Send the document's kept file, inline."""
    kept = await _documents(http_request).open_file(user_id, source_id)
    return Response(
        content=kept.content,
        media_type=kept.content_type,
        headers={
            "Content-Disposition": _inline_disposition(kept.file_name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


def _inline_disposition(file_name: str) -> str:
    """``inline; filename="..."`` (ASCII), plus RFC 6266's ``filename*`` when the name is
    not plain ASCII. ``file_name`` is already safe (``download_name``)."""
    plain = ascii_name(file_name)
    header = f'inline; filename="{plain}"'
    if plain != file_name:
        header += f"; filename*=UTF-8''{quote(file_name, safe='')}"
    return header


@router.delete(
    "/{source_id}",
    response_model=DocumentDeleteResponse,
    summary="Delete a document",
    description="Deletes its chunks, its task links, its record and its file.",
)
async def delete_document(
    source_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User identifier"),
) -> DocumentDeleteResponse:
    """Delete a document and all associated data."""
    documents = _documents(http_request)
    log_step("DOC_DELETE", "Deleting document", {"user_id": user_id, "source_id": source_id})
    await documents.delete(user_id, source_id)
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
