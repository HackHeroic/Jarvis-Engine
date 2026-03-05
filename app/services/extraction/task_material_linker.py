"""Task–Material Linking: Match documents to user tasks via embedding similarity."""

from typing import Any, Optional

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from supabase import create_client

SIMILARITY_THRESHOLD = 0.65  # 0.6–0.8 recommended to avoid noisy links


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _get_embedding_function():
    """Use same embedding as ChromaDB jarvis_knowledge collection (all-MiniLM-L6-v2)."""
    try:
        from chromadb.utils import embedding_functions

        return embedding_functions.DefaultEmbeddingFunction()
    except ImportError:
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def link_document_to_tasks(
    user_id: str,
    document_topics: list[str],
    source_id: str,
    source_type: str = "chunk",
    threshold: float = SIMILARITY_THRESHOLD,
    supabase_client: Any = None,
) -> list[str]:
    """Match document to user tasks via embedding similarity.

    Returns matched task_ids. Empty list if no match above threshold.
    """
    if not document_topics:
        return []

    supabase = supabase_client or _get_supabase()
    if not supabase:
        return []

    ef = _get_embedding_function()
    if not ef:
        return []

    try:
        # Fetch user tasks (most recent per task_id or all)
        result = (
            supabase.table("user_tasks")
            .select("id, task_id, title")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        tasks = result.data or []
        if not tasks:
            return []

        # Dedupe by task_id (keep latest)
        seen: set[str] = set()
        unique_tasks: list[dict] = []
        for t in tasks:
            tid = t.get("task_id") or t.get("id", "")
            if tid and tid not in seen:
                seen.add(tid)
                unique_tasks.append(t)

        doc_text = " ".join(document_topics).strip()
        if not doc_text:
            return []

        titles = [t.get("title") or "" for t in unique_tasks]
        texts_to_embed = [doc_text] + titles
        embeddings = ef(texts_to_embed)
        doc_emb = embeddings[0]
        task_embs = embeddings[1:]

        matched: list[str] = []
        for t, emb in zip(unique_tasks, task_embs):
            sim = _cosine_similarity(doc_emb, emb)
            if sim >= threshold:
                task_id = t.get("task_id") or t.get("id", "")
                if task_id:
                    matched.append(task_id)

        # Persist links for matched tasks
        for task_id in matched:
            try:
                supabase.table("task_materials").upsert(
                    {
                        "user_id": user_id,
                        "task_id": task_id,
                        "source_type": source_type,
                        "source_id": source_id,
                        "document_topics": document_topics,
                    },
                    on_conflict="user_id,task_id,source_id",
                ).execute()
            except Exception:
                pass  # Upsert may fail if table/constraint differs

        return matched
    except Exception:
        return []
