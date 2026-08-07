"""Task–Material Linking: Match documents to user tasks via embedding similarity."""

from typing import Any, Optional

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.utils.embedding import cosine_similarity as _cosine_similarity, get_embedding_function as _get_embedding_function
from supabase import create_client

SIMILARITY_THRESHOLD = 0.65  # 0.6–0.8 recommended to avoid noisy links


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


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

        # Invalidate workspace cache for every newly-linked task so the next
        # workspace fetch rebuilds with the new document.
        if matched:
            try:
                from app.services.analytical.workspace_builder import invalidate_workspace_cache
                import asyncio as _asyncio
                for tid in matched:
                    try:
                        await invalidate_workspace_cache(user_id, tid)
                    except Exception:
                        pass
            except Exception:
                pass

        return matched
    except Exception:
        return []
