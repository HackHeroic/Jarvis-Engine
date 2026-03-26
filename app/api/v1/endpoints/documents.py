"""Document management endpoints: list and delete user-ingested documents."""

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.jarvis_logger import log_step

router = APIRouter()


@router.get(
    "/",
    summary="List user documents",
    description="Returns all documents ingested by the user, with linked task IDs.",
)
async def list_documents(
    http_request: Request,
    user_id: str = Query(..., description="User identifier"),
) -> dict:
    """List all ingested documents for a user."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        raise HTTPException(503, "Database not available")

    try:
        docs_result = (
            supabase.table("ingested_documents")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(500, f"Failed to query documents: {exc}") from exc

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

    return {"documents": documents}


@router.delete(
    "/{source_id}",
    summary="Delete a document",
    description="Cascade delete: ChromaDB chunks, task_materials links, ingested_documents row.",
)
async def delete_document(
    source_id: str,
    http_request: Request,
    user_id: str = Query(..., description="User identifier"),
) -> dict:
    """Delete a document and all associated data."""
    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        raise HTTPException(503, "Database not available")

    log_step("DOC_DELETE", "Deleting document", {"user_id": user_id, "source_id": source_id})

    # 1. Delete chunks from ChromaDB
    try:
        from app.utils.chroma_client import get_chroma_client

        client = get_chroma_client()
        collection = client.get_or_create_collection("jarvis_knowledge")
        results = collection.get(where={"source_id": source_id})
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
        if not result.data:
            raise HTTPException(404, "Document not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to delete document record: {exc}") from exc

    return {"status": "deleted", "source_id": source_id}
