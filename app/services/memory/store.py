"""CRUD operations for the user_memories table (Tier 3: Archival Memory).

All queries filter by user_id for IDOR protection.
Embeddings are pre-computed at storage time using all-MiniLM-L6-v2.

NOTE: All methods are synchronous (using sync Supabase client).
When calling from async code, wrap in asyncio.to_thread().
The memory extractor already does this via safe_extract_memories (fire-and-forget).
Memory retrieval on the /chat hot path will need asyncio.to_thread() wrappers.
"""

import uuid
from datetime import datetime, timezone

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.services.memory.retriever import compute_memory_strength
from app.utils.embedding import cosine_similarity, embed_text


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


MAX_STABILITY = 20.0


class MemoryStore:
    """CRUD for user_memories in Supabase."""

    def __init__(self, supabase_client=None):
        self._supabase = supabase_client or _get_supabase()

    def store_memory(self, user_id: str, memory: dict) -> dict | None:
        if not self._supabase:
            return None
        content = memory.get("content", "")
        embedding = embed_text(content)
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "memory_type": memory.get("type", "fact"),
            "content": content,
            "source": memory.get("source", "conversation"),
            "source_id": memory.get("source_id"),
            "confidence": memory.get("confidence", 0.5),
            "strength": 1.0,
            "stability": 1.0,
            "last_accessed": now,
            "last_reinforced": now,
            "expires_at": memory.get("expires_at"),
            "observation_count": memory.get("observation_count", 1),
            "applied_as": memory.get("applied_as"),
            "created_at": now,
            "updated_at": now,
        }
        if embedding:
            row["embedding"] = embedding
        result = self._supabase.table("user_memories").insert(row).execute()
        return result.data[0] if result.data else None

    def get_active_memories(self, user_id: str) -> list[dict]:
        if not self._supabase:
            return []
        result = (
            self._supabase.table("user_memories")
            .select("*")
            .eq("user_id", user_id)
            .is_("superseded_by", "null")
            .execute()
        )
        rows = result.data or []
        # Post-fetch: filter out memories whose time-decayed strength < 0.1
        now = datetime.now(timezone.utc)
        return [m for m in rows if compute_memory_strength(m, now) >= 0.1]

    def get_memories_by_type(self, user_id: str, memory_type: str, min_confidence: float = 0.0) -> list[dict]:
        if not self._supabase:
            return []
        query = (
            self._supabase.table("user_memories")
            .select("*")
            .eq("user_id", user_id)
            .eq("memory_type", memory_type)
            .is_("superseded_by", "null")
            .gt("strength", 0.1)
        )
        if min_confidence > 0:
            query = query.gt("confidence", min_confidence)
        result = query.execute()
        return result.data or []

    def get_memory(self, memory_id: str, user_id: str = None) -> dict | None:
        """Get a single memory. Always filters by user_id when provided.
        Returns None if user_id is not provided (fail-safe IDOR protection)."""
        if not self._supabase or not user_id:
            return None
        result = (
            self._supabase.table("user_memories")
            .select("*")
            .eq("id", memory_id)
            .eq("user_id", user_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def update_memory(self, memory_id: str, updates: dict, user_id: str = None) -> bool:
        """Update fields on a memory. Always filters by user_id when provided.
        Returns False if user_id is not provided (fail-safe IDOR protection)."""
        if not self._supabase or not user_id:
            return False
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = (
            self._supabase.table("user_memories")
            .update(updates)
            .eq("id", memory_id)
            .eq("user_id", user_id)
            .execute()
        )
        return bool(result.data)

    def supersede_memory(self, old_memory_id: str, user_id: str, new_content: str) -> dict | None:
        old_mem = self.get_memory(old_memory_id, user_id=user_id)
        if not old_mem:
            return None
        new_mem = self.store_memory(user_id, {
            "type": old_mem.get("memory_type", "fact"),
            "content": new_content,
            "confidence": 0.6,
            "source": "contradiction",
        })
        if not new_mem:
            return None
        self.update_memory(old_memory_id, {"superseded_by": new_mem["id"]}, user_id=user_id)
        return new_mem

    def reinforce_memory(self, memory_id: str, user_id: str = None) -> bool:
        """Reinforce a memory. Requires user_id for IDOR protection."""
        if not user_id:
            return False
        mem = self.get_memory(memory_id, user_id=user_id)
        if not mem:
            return False
        new_stability = min(MAX_STABILITY, (mem.get("stability", 1.0) + 1.0))
        new_confidence = min(1.0, (mem.get("confidence", 0.5) + 0.1))
        now = datetime.now(timezone.utc).isoformat()
        return self.update_memory(memory_id, {
            "stability": new_stability,
            "confidence": new_confidence,
            "strength": 1.0,
            "last_reinforced": now,
        }, user_id=user_id)

    def find_similar_memory(self, user_id: str, content: str, threshold: float = 0.85, memory_type: str | None = None) -> dict | None:
        query_embedding = embed_text(content)
        if not query_embedding:
            return None
        memories = self.get_active_memories(user_id)
        if memory_type:
            memories = [m for m in memories if m.get("memory_type") == memory_type]
        best_match = None
        best_score = 0.0
        for mem in memories:
            mem_embedding = mem.get("embedding")
            if not mem_embedding:
                continue
            score = cosine_similarity(query_embedding, mem_embedding)
            if score > best_score and score >= threshold:
                best_score = score
                best_match = mem
        return best_match

    def archive_memory(self, memory_id: str, user_id: str = None) -> bool:
        return self.update_memory(memory_id, {"strength": 0.0}, user_id=user_id)

    def weaken_memory(self, memory_id: str, user_id: str = None) -> bool:
        """Reduce confidence by 0.3, cap stability at 0.5. Used when user dismisses a pattern."""
        if not user_id:
            return False
        mem = self.get_memory(memory_id, user_id=user_id)
        if not mem:
            return False
        new_conf = max(0.0, mem.get("confidence", 0.5) - 0.3)
        new_stab = min(0.5, mem.get("stability", 1.0))
        return self.update_memory(memory_id, {
            "confidence": new_conf,
            "stability": new_stab,
        }, user_id=user_id)
