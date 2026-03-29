# Architecture Reset Phase 1B: Memory & Context

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Jarvis persistent memory — extract facts/preferences/constraints from conversations, score them with SM-2 decay, detect contradictions, and bridge memories into OR-Tools scheduling constraints. After Phase 1B, Jarvis remembers the user across sessions and adapts its scheduling without being told.

**Architecture:** Three-tier memory inspired by MemGPT. Tier 1 (Working Memory) = current session messages (already exists in `chat_history.py`). Tier 2 (Recall Memory) = session summaries (extend existing `chat_sessions`). Tier 3 (Archival Memory) = new `user_memories` table with SM-2 decay scoring. Memory extraction runs as a fire-and-forget background task after each `/chat` response. Memory retrieval happens at the START of each request, injecting relevant memories into the LLM system prompt. The Memory→Constraint Bridge converts memories of type `constraint` and `behavioral_pattern` into `TimeSlot` objects that feed directly into OR-Tools.

**Tech Stack:** FastAPI, Pydantic v2, Supabase (PostgreSQL), ChromaDB embedding (all-MiniLM-L6-v2 for cosine similarity), LiteLLM (Qwen-4B for extraction)

**Spec:** `docs/superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md` (sections: Memory & Context Architecture, Database Schema, Memory Extraction Pipeline, Memory Scoring & Retrieval, Memory Reinforcement & Contradiction, Memory → Scheduler Constraint Bridge, Session Management, Memory Embedding Strategy, Memory Extraction Error Handling)

**Prerequisite:** Phase 1A complete (BaseRegistry, Supabase patterns, `prefer_local` parameter on `hybrid_route_query`)

**Produces:** Working memory system where memories are extracted from conversations, scored with SM-2 decay, injected into LLM prompts, and bridged into OR-Tools constraints. All covered by unit and integration tests.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/utils/embedding.py` | Shared embedding + cosine similarity (extracted from task_material_linker) |
| Create | `app/services/memory/__init__.py` | Memory package init |
| Create | `app/services/memory/store.py` | CRUD for `user_memories` table — store, get, update, archive |
| Create | `app/services/memory/retriever.py` | SM-2 scoring, Top-K retrieval, memory context formatting |
| Create | `app/services/memory/extractor.py` | LLM-based memory extraction from conversation turns |
| Create | `app/services/memory/lifecycle.py` | Reinforcement, contradiction/supersede, decay |
| Create | `app/services/memory/constraint_bridge.py` | Convert memories → TimeSlot constraints for OR-Tools |
| Create | `app/schemas/memory.py` | Pydantic schemas for memory extraction and storage |
| Create | `tests/test_memory_store.py` | Unit tests for memory CRUD |
| Create | `tests/test_memory_retriever.py` | Unit tests for SM-2 scoring and retrieval |
| Create | `tests/test_memory_extractor.py` | Unit tests for extraction pipeline (mocked LLM) |
| Create | `tests/test_memory_lifecycle.py` | Unit tests for reinforcement, contradiction, decay |
| Create | `tests/test_memory_constraint_bridge.py` | Unit tests for memory → TimeSlot conversion |
| Modify | `app/services/extraction/task_material_linker.py` | Remove embedding/cosine funcs (moved to shared utils) |

---

### Task 1: Shared Embedding Utilities + Memory Schemas

**Files:**
- Create: `app/utils/embedding.py`
- Create: `app/schemas/memory.py`
- Create: `tests/test_memory_store.py` (just the import test initially)

- [ ] **Step 1: Write the failing test for embedding utilities**

```python
# tests/test_memory_store.py
"""Tests for memory store and supporting utilities."""

import math
from app.utils.embedding import cosine_similarity


def test_cosine_similarity_identical_vectors():
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert abs(cosine_similarity(a, b) - 1.0) < 0.001


def test_cosine_similarity_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(cosine_similarity(a, b)) < 0.001


def test_cosine_similarity_opposite_vectors():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(cosine_similarity(a, b) - (-1.0)) < 0.001


def test_cosine_similarity_zero_vector():
    a = [0.0, 0.0]
    b = [1.0, 1.0]
    assert cosine_similarity(a, b) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.utils.embedding'`

- [ ] **Step 3: Create shared embedding utilities**

```python
# app/utils/embedding.py
"""Shared embedding and similarity utilities.

Used by both the memory system and the task-material linker.
Embedding model: all-MiniLM-L6-v2 (384 dimensions) via ChromaDB.
Runs locally — no API calls, no cost, ~5ms per embedding.
"""

import math
from typing import Optional


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns 0.0 if either is zero."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def get_embedding_function():
    """Get the ChromaDB default embedding function (all-MiniLM-L6-v2).

    Returns None if chromadb is not installed.
    """
    try:
        from chromadb.utils import embedding_functions
        return embedding_functions.DefaultEmbeddingFunction()
    except ImportError:
        return None


def embed_text(text: str) -> Optional[list[float]]:
    """Embed a single text string. Returns None if embedding unavailable."""
    ef = get_embedding_function()
    if ef is None:
        return None
    results = ef([text])
    if results and len(results) > 0:
        return list(results[0])
    return None


def embed_texts(texts: list[str]) -> list[Optional[list[float]]]:
    """Embed multiple texts in a batch. Returns list of embeddings."""
    ef = get_embedding_function()
    if ef is None:
        return [None] * len(texts)
    results = ef(texts)
    return [list(r) if r is not None else None for r in results]
```

- [ ] **Step 4: Create memory schemas**

```python
# app/schemas/memory.py
"""Pydantic schemas for the memory system."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Valid memory types — matches the CHECK constraint on user_memories table
MemoryType = Literal[
    "fact", "preference", "behavioral_pattern",
    "temporal_event", "goal", "feedback", "constraint",
]


class MemoryRecord(BaseModel):
    """A single memory from the user_memories table."""

    id: str
    user_id: str
    memory_type: MemoryType
    content: str
    source: str = "conversation"
    source_id: str | None = None
    confidence: float = 0.5
    strength: float = 1.0
    stability: float = 1.0
    last_accessed: datetime | None = None
    last_reinforced: datetime | None = None
    superseded_by: str | None = None
    expires_at: datetime | None = None
    observation_count: int = 1
    applied_as: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    embedding: list[float] | None = None


class ExtractedMemory(BaseModel):
    """A memory extracted from a conversation turn by the LLM."""

    type: MemoryType
    content: str = Field(description="Concise statement of what was learned")
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    contradicts: str | None = Field(
        default=None,
        description="ID of existing memory this contradicts, or null",
    )
    expires_at: str | None = Field(
        default=None,
        description="ISO date if temporal event, or null",
    )


class MemoryExtractionResponse(BaseModel):
    """Response schema for the memory extraction LLM call."""

    memories: list[ExtractedMemory] = Field(default_factory=list)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_store.py -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/utils/embedding.py app/schemas/memory.py tests/test_memory_store.py
git commit -m "feat(memory): add shared embedding utilities and memory schemas"
```

---

### Task 2: Memory Store — CRUD for user_memories

**Files:**
- Create: `app/services/memory/__init__.py`
- Create: `app/services/memory/store.py`
- Modify: `tests/test_memory_store.py`

- [ ] **Step 1: Write failing tests for memory store**

Append to `tests/test_memory_store.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from app.services.memory.store import MemoryStore


@pytest.fixture
def memory_store(mock_supabase):
    return MemoryStore(supabase_client=mock_supabase)


def test_store_memory(memory_store, mock_supabase):
    mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "mem-1", "user_id": "u1", "content": "User is a CS student"}
    ]

    with patch("app.services.memory.store.embed_text", return_value=[0.1] * 384):
        result = memory_store.store_memory("u1", {
            "type": "fact",
            "content": "User is a CS student",
            "confidence": 0.8,
        })

    assert result is not None
    assert result["id"] == "mem-1"
    mock_supabase.table.assert_called_with("user_memories")


def test_get_active_memories(memory_store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.is_.return_value.gt.return_value.execute.return_value.data = [
        {"id": "m1", "user_id": "u1", "memory_type": "fact", "content": "CS student", "confidence": 0.8, "strength": 1.0, "stability": 1.0},
        {"id": "m2", "user_id": "u1", "memory_type": "preference", "content": "Hates mornings", "confidence": 0.7, "strength": 0.9, "stability": 2.0},
    ]
    result = memory_store.get_active_memories("u1")
    assert len(result) == 2
    assert result[0]["memory_type"] == "fact"


def test_get_memories_by_type(memory_store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.is_.return_value.gt.return_value.execute.return_value.data = [
        {"id": "m1", "memory_type": "constraint", "content": "No work after 6 PM"},
    ]
    result = memory_store.get_memories_by_type("u1", "constraint")
    assert len(result) == 1
    assert result[0]["memory_type"] == "constraint"


def test_update_memory(memory_store, mock_supabase):
    mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
        {"id": "m1", "strength": 1.0, "stability": 2.0}
    ]
    result = memory_store.update_memory("m1", {"strength": 1.0, "stability": 2.0})
    assert result is True


def test_supersede_memory(memory_store, mock_supabase):
    # Setup: get old memory
    mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "old-1", "user_id": "u1", "memory_type": "preference", "content": "Likes mornings"}
    ]
    # Setup: insert new memory
    mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "new-1", "user_id": "u1", "memory_type": "preference", "content": "Prefers evenings"}
    ]

    with patch("app.services.memory.store.embed_text", return_value=[0.1] * 384):
        new_mem = memory_store.supersede_memory("old-1", "u1", "Prefers evenings")

    assert new_mem is not None


def test_find_similar_memory(memory_store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.is_.return_value.gt.return_value.execute.return_value.data = [
        {"id": "m1", "content": "User is a CS student", "embedding": [0.1] * 384},
    ]

    with patch("app.services.memory.store.embed_text", return_value=[0.1] * 384):
        result = memory_store.find_similar_memory("u1", "CS student at VIT", threshold=0.5)

    assert result is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.memory'`

- [ ] **Step 3: Create memory package init**

```python
# app/services/memory/__init__.py
"""Jarvis Memory System — 3-tier memory with SM-2 decay."""
```

- [ ] **Step 4: Implement memory store**

```python
# app/services/memory/store.py
"""CRUD operations for the user_memories table (Tier 3: Archival Memory).

All queries filter by user_id for IDOR protection.
Embeddings are pre-computed at storage time using all-MiniLM-L6-v2.
"""

import uuid
from datetime import datetime, timezone

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.utils.embedding import cosine_similarity, embed_text


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# Stability cap — prevents memories from becoming permanently undecayable
MAX_STABILITY = 20.0


class MemoryStore:
    """CRUD for user_memories in Supabase."""

    def __init__(self, supabase_client=None):
        self._supabase = supabase_client or _get_supabase()

    def store_memory(self, user_id: str, memory: dict) -> dict | None:
        """Store a new memory with pre-computed embedding.

        Args:
            user_id: Owner of the memory.
            memory: Dict with keys: type, content, confidence, source (optional),
                    expires_at (optional), applied_as (optional), observation_count (optional).

        Returns:
            The inserted row dict, or None on failure.
        """
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
        """Get all active (non-superseded, strength > 0.1) memories for a user."""
        if not self._supabase:
            return []

        result = (
            self._supabase.table("user_memories")
            .select("*")
            .eq("user_id", user_id)
            .is_("superseded_by", "null")
            .gt("strength", 0.1)
            .execute()
        )
        return result.data or []

    def get_memories_by_type(
        self, user_id: str, memory_type: str, min_confidence: float = 0.0
    ) -> list[dict]:
        """Get active memories of a specific type."""
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

    def get_memory(self, memory_id: str) -> dict | None:
        """Get a single memory by ID."""
        if not self._supabase:
            return None

        result = (
            self._supabase.table("user_memories")
            .select("*")
            .eq("id", memory_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def update_memory(self, memory_id: str, updates: dict) -> bool:
        """Update fields on a memory."""
        if not self._supabase:
            return False

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = (
            self._supabase.table("user_memories")
            .update(updates)
            .eq("id", memory_id)
            .execute()
        )
        return bool(result.data)

    def supersede_memory(
        self, old_memory_id: str, user_id: str, new_content: str
    ) -> dict | None:
        """Replace an old memory with a new one (contradiction handling).

        The old memory is NOT deleted — it's marked with superseded_by.
        The new memory starts with moderate confidence (0.6).
        """
        old_mem = self.get_memory(old_memory_id)
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

        self.update_memory(old_memory_id, {"superseded_by": new_mem["id"]})
        return new_mem

    def reinforce_memory(self, memory_id: str) -> bool:
        """Reinforce a memory — increase stability and confidence.

        SM-2 analogy: successful review → next interval gets longer.
        Stability is capped at MAX_STABILITY (20) to prevent infinite half-life.
        """
        mem = self.get_memory(memory_id)
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
        })

    def find_similar_memory(
        self,
        user_id: str,
        content: str,
        threshold: float = 0.85,
        memory_type: str | None = None,
    ) -> dict | None:
        """Find a memory similar to the given content (for deduplication).

        Uses pre-computed embeddings and cosine similarity.
        Returns the most similar memory above threshold, or None.
        """
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

    def archive_memory(self, memory_id: str) -> bool:
        """Archive a decayed memory (strength dropped below 0.1)."""
        return self.update_memory(memory_id, {"strength": 0.0})
```

- [ ] **Step 5: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_store.py -v`
Expected: All 10 tests PASS (4 embedding + 6 store)

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/utils/embedding.py app/schemas/memory.py app/services/memory/__init__.py app/services/memory/store.py tests/test_memory_store.py
git commit -m "feat(memory): add MemoryStore with CRUD, embedding, supersede, and reinforcement"
```

---

### Task 3: Memory Retriever — SM-2 Scoring and Context Injection

**Files:**
- Create: `app/services/memory/retriever.py`
- Create: `tests/test_memory_retriever.py`

- [ ] **Step 1: Write failing tests for memory retriever**

```python
# tests/test_memory_retriever.py
"""Tests for memory scoring, retrieval, and context formatting."""

import math
from datetime import datetime, timezone, timedelta

from app.services.memory.retriever import (
    compute_memory_strength,
    score_memory,
    format_memory_block,
    IMPORTANCE_WEIGHTS,
)


def _make_memory(
    memory_type="fact",
    content="Test memory",
    confidence=0.8,
    strength=1.0,
    stability=1.0,
    last_reinforced=None,
    superseded_by=None,
    embedding=None,
):
    """Helper to create a memory-like dict for testing."""
    return {
        "id": "test-id",
        "user_id": "u1",
        "memory_type": memory_type,
        "content": content,
        "confidence": confidence,
        "strength": strength,
        "stability": stability,
        "last_reinforced": (last_reinforced or datetime.now(timezone.utc)).isoformat(),
        "superseded_by": superseded_by,
        "embedding": embedding or [0.1] * 10,
    }


class TestComputeMemoryStrength:
    def test_fresh_memory_has_full_strength(self):
        """A memory just reinforced should have strength close to 1.0."""
        mem = _make_memory(last_reinforced=datetime.now(timezone.utc))
        strength = compute_memory_strength(mem, datetime.now(timezone.utc))
        assert strength > 0.99

    def test_memory_decays_over_time(self):
        """A memory from a week ago with stability=1 should be significantly decayed."""
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        mem = _make_memory(last_reinforced=one_week_ago, stability=1.0)
        strength = compute_memory_strength(mem, datetime.now(timezone.utc))
        # e^(-1) ≈ 0.368
        assert 0.3 < strength < 0.4

    def test_higher_stability_slows_decay(self):
        """A memory with stability=5 should decay much slower than stability=1."""
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        mem_low = _make_memory(last_reinforced=one_week_ago, stability=1.0)
        mem_high = _make_memory(last_reinforced=one_week_ago, stability=5.0)
        now = datetime.now(timezone.utc)
        strength_low = compute_memory_strength(mem_low, now)
        strength_high = compute_memory_strength(mem_high, now)
        assert strength_high > strength_low
        # stability=5, 1 week: e^(-1/5) ≈ 0.819
        assert strength_high > 0.8

    def test_very_old_memory_approaches_zero(self):
        """A memory from 3 months ago with stability=1 should be near zero."""
        three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)
        mem = _make_memory(last_reinforced=three_months_ago, stability=1.0)
        strength = compute_memory_strength(mem, datetime.now(timezone.utc))
        assert strength < 0.01


class TestScoreMemory:
    def test_score_combines_all_factors(self):
        """Score should be relevance × recency × importance × confidence."""
        mem = _make_memory(
            memory_type="constraint",
            confidence=0.9,
            last_reinforced=datetime.now(timezone.utc),
        )
        query_emb = [0.1] * 10
        mem_emb = [0.1] * 10  # Identical = relevance 1.0
        score = score_memory(mem, query_emb, mem_emb, datetime.now(timezone.utc))
        # relevance=1.0, recency≈1.0, importance=1.0 (constraint), confidence=0.9
        assert score > 0.85

    def test_constraint_scores_higher_than_fact(self):
        """Constraints should score higher than facts, all else equal."""
        now = datetime.now(timezone.utc)
        query_emb = [0.1] * 10
        mem_emb = [0.1] * 10

        constraint = _make_memory(memory_type="constraint", last_reinforced=now)
        fact = _make_memory(memory_type="fact", last_reinforced=now)

        score_c = score_memory(constraint, query_emb, mem_emb, now)
        score_f = score_memory(fact, query_emb, mem_emb, now)
        assert score_c > score_f


class TestFormatMemoryBlock:
    def test_empty_memories_returns_empty_string(self):
        assert format_memory_block([]) == ""

    def test_formats_by_type(self):
        memories = [
            {"memory_type": "constraint", "content": "No work after 6 PM"},
            {"memory_type": "fact", "content": "CS student at VIT"},
            {"memory_type": "goal", "content": "Finish DSA by April"},
        ]
        result = format_memory_block(memories)
        assert "Scheduling Constraints" in result
        assert "No work after 6 PM" in result
        assert "Facts" in result
        assert "CS student at VIT" in result
        assert "Active Goals" in result

    def test_groups_same_type(self):
        memories = [
            {"memory_type": "fact", "content": "Fact 1"},
            {"memory_type": "fact", "content": "Fact 2"},
        ]
        result = format_memory_block(memories)
        assert result.count("Facts") == 1  # Only one header
        assert "Fact 1" in result
        assert "Fact 2" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_retriever.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement memory retriever**

```python
# app/services/memory/retriever.py
"""Memory scoring, retrieval, and LLM context injection.

Implements SM-2 inspired decay scoring and Top-K retrieval.
Called at the START of every /chat request to inject relevant
memories into the LLM system prompt.
"""

import math
from datetime import datetime, timezone

from app.utils.embedding import cosine_similarity

# Type-based importance weights (higher = more important to surface)
IMPORTANCE_WEIGHTS = {
    "constraint": 1.0,
    "behavioral_pattern": 0.9,
    "preference": 0.8,
    "temporal_event": 0.8,
    "goal": 0.7,
    "fact": 0.6,
    "feedback": 0.5,
}

# Types that are ALWAYS injected regardless of relevance score
ALWAYS_INCLUDE_TYPES = {"constraint", "goal", "behavioral_pattern"}
ALWAYS_INCLUDE_MIN_CONFIDENCE = 0.6

# Base half-life in hours (1 week)
BASE_HALFLIFE_HOURS = 7 * 24


def compute_memory_strength(memory: dict, current_time: datetime) -> float:
    """SM-2 inspired decay function.

    Memory_Strength(t) = strength × e^(-t / (stability × base_halflife))

    - stability starts at 1.0, increases on each reinforcement (capped at 20)
    - base_halflife = 168 hours (1 week)
    - At stability=1: half-life = 1 week
    - At stability=5: half-life = 5 weeks
    - At stability=20 (cap): half-life = 140 days
    """
    last_reinforced_str = memory.get("last_reinforced")
    if not last_reinforced_str:
        return memory.get("strength", 1.0)

    if isinstance(last_reinforced_str, str):
        last_reinforced = datetime.fromisoformat(last_reinforced_str)
    else:
        last_reinforced = last_reinforced_str

    # Ensure timezone-aware
    if last_reinforced.tzinfo is None:
        last_reinforced = last_reinforced.replace(tzinfo=timezone.utc)

    hours_since = (current_time - last_reinforced).total_seconds() / 3600
    stability = memory.get("stability", 1.0)
    effective_halflife = stability * BASE_HALFLIFE_HOURS
    strength = memory.get("strength", 1.0)

    return strength * math.exp(-hours_since / effective_halflife)


def score_memory(
    memory: dict,
    query_embedding: list[float],
    memory_embedding: list[float],
    current_time: datetime,
) -> float:
    """Score = Relevance × Recency × Importance × Confidence."""
    relevance = cosine_similarity(query_embedding, memory_embedding)
    recency = compute_memory_strength(memory, current_time)
    importance = IMPORTANCE_WEIGHTS.get(memory.get("memory_type", "fact"), 0.5)
    confidence = memory.get("confidence", 0.5)

    return relevance * recency * importance * confidence


def format_memory_block(memories: list[dict]) -> str:
    """Format memories as a structured block for the LLM system prompt."""
    if not memories:
        return ""

    sections: dict[str, list[str]] = {}
    for mem in memories:
        mem_type = mem.get("memory_type", "fact")
        sections.setdefault(mem_type, []).append(mem.get("content", ""))

    type_labels = {
        "constraint": "Scheduling Constraints",
        "goal": "Active Goals",
        "behavioral_pattern": "Observed Patterns",
        "preference": "Preferences",
        "temporal_event": "Upcoming Events",
        "fact": "Facts",
        "feedback": "User Feedback on Jarvis",
    }

    lines = ["## What you know about this user:\n"]
    for mem_type, label in type_labels.items():
        if mem_type in sections:
            lines.append(f"### {label}")
            for content in sections[mem_type]:
                lines.append(f"- {content}")
            lines.append("")

    return "\n".join(lines)


def deduplicate_memories(memories: list[dict]) -> list[dict]:
    """Remove duplicate memories by ID."""
    seen: set[str] = set()
    result = []
    for mem in memories:
        mem_id = mem.get("id", "")
        if mem_id and mem_id not in seen:
            seen.add(mem_id)
            result.append(mem)
    return result
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_retriever.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/memory/retriever.py tests/test_memory_retriever.py
git commit -m "feat(memory): add SM-2 decay scoring, Top-K retrieval, and context formatting"
```

---

### Task 4: Memory Extractor — LLM-based Extraction Pipeline

**Files:**
- Create: `app/services/memory/extractor.py`
- Create: `tests/test_memory_extractor.py`

- [ ] **Step 1: Write failing tests for memory extractor**

```python
# tests/test_memory_extractor.py
"""Tests for LLM-based memory extraction from conversation turns."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.memory.extractor import (
    extract_memories_from_turn,
    safe_extract_memories,
    EXTRACTION_PROMPT,
)


def test_extraction_prompt_has_placeholders():
    """The extraction prompt should have the right format placeholders."""
    assert "{existing_memories}" in EXTRACTION_PROMPT
    assert "{user_message}" in EXTRACTION_PROMPT
    assert "{assistant_response}" in EXTRACTION_PROMPT


@pytest.mark.asyncio
async def test_extract_memories_returns_list():
    """Extraction should return a list of extracted memories."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []
    mock_store.find_similar_memory.return_value = None
    mock_store.store_memory.return_value = {"id": "new-1"}

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": [
            {"type": "fact", "content": "User is a CS student", "confidence": 0.8, "contradicts": None, "expires_at": None}
        ]},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="I'm a CS student at VIT",
            assistant_response="Great! I'll keep that in mind.",
            memory_store=mock_store,
        )

    assert len(result) == 1
    assert result[0]["type"] == "fact"
    mock_store.store_memory.assert_called_once()


@pytest.mark.asyncio
async def test_extract_handles_contradiction():
    """When LLM marks a contradiction, supersede_memory should be called."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = [
        {"id": "old-1", "memory_type": "preference", "content": "User likes mornings"}
    ]
    mock_store.supersede_memory.return_value = {"id": "new-1"}

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": [
            {"type": "preference", "content": "User prefers evenings", "confidence": 0.7, "contradicts": "old-1", "expires_at": None}
        ]},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="Actually I work best in the evenings",
            assistant_response="Noted — I'll adjust your schedule.",
            memory_store=mock_store,
        )

    assert len(result) == 1
    mock_store.supersede_memory.assert_called_once_with("old-1", "u1", "User prefers evenings")


@pytest.mark.asyncio
async def test_extract_handles_duplicate():
    """When a similar memory already exists, reinforce instead of creating new."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []
    mock_store.find_similar_memory.return_value = {"id": "existing-1"}
    mock_store.reinforce_memory.return_value = True

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": [
            {"type": "fact", "content": "User is a CS student", "confidence": 0.8, "contradicts": None, "expires_at": None}
        ]},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="As a CS student, I need...",
            assistant_response="Sure!",
            memory_store=mock_store,
        )

    mock_store.reinforce_memory.assert_called_once_with("existing-1")
    mock_store.store_memory.assert_not_called()


@pytest.mark.asyncio
async def test_extract_returns_empty_when_nothing_new():
    """When LLM finds nothing new, return empty list."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []

    with patch(
        "app.services.memory.extractor.hybrid_route_query",
        new_callable=AsyncMock,
        return_value={"memories": []},
    ):
        result = await extract_memories_from_turn(
            user_id="u1",
            user_message="Hello",
            assistant_response="Hi there!",
            memory_store=mock_store,
        )

    assert result == []
    mock_store.store_memory.assert_not_called()


@pytest.mark.asyncio
async def test_safe_extract_catches_errors():
    """safe_extract_memories should never raise — fire and forget."""
    mock_store = MagicMock()
    mock_store.get_active_memories.side_effect = Exception("DB down")

    # Should NOT raise
    await safe_extract_memories(
        user_id="u1",
        user_message="test",
        assistant_response="test",
        memory_store=mock_store,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement memory extractor**

```python
# app/services/memory/extractor.py
"""LLM-based memory extraction from conversation turns.

Runs as a fire-and-forget background task after each /chat response.
Uses Qwen-4B (prefer_local=True) since this is a background task
that doesn't need to be perfect — a missed extraction is fine.
"""

import logging

from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.memory import MemoryExtractionResponse

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Analyze this conversation exchange and extract new information about the user.

EXISTING MEMORIES (what we already know):
{existing_memories}

CURRENT EXCHANGE:
User: {user_message}
Assistant: {assistant_response}

Extract ONLY genuinely new information. Do not repeat what we already know.
If the user contradicts an existing memory, mark it as a contradiction.

Return JSON object with "memories" array:
{{"memories": [
  {{
    "type": "fact|preference|behavioral_pattern|temporal_event|goal|feedback|constraint",
    "content": "concise statement of what was learned",
    "confidence": 0.5-1.0,
    "contradicts": "id of existing memory this contradicts, or null",
    "expires_at": "ISO date if temporal, or null"
  }}
]}}

Return {{"memories": []}} if nothing new was learned."""

EXTRACTION_SYSTEM_PROMPT = (
    "You are a memory extraction agent. Extract structured facts, "
    "preferences, and constraints from conversations. Return valid JSON only."
)


async def extract_memories_from_turn(
    user_id: str,
    user_message: str,
    assistant_response: str,
    memory_store,
) -> list[dict]:
    """Extract structured memories from a conversation turn.

    Args:
        user_id: User who sent the message.
        user_message: What the user said.
        assistant_response: What Jarvis replied.
        memory_store: MemoryStore instance for CRUD operations.

    Returns:
        List of extracted memory dicts.
    """
    existing = memory_store.get_active_memories(user_id)

    formatted_existing = "\n".join([
        f"[{m.get('id', '?')}] ({m.get('memory_type', '?')}) {m.get('content', '')}"
        for m in existing[:20]
    ])

    prompt = EXTRACTION_PROMPT.format(
        existing_memories=formatted_existing or "None yet.",
        user_message=user_message[:500],
        assistant_response=assistant_response[:500],
    )

    raw = await hybrid_route_query(
        user_prompt=prompt,
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
        response_schema=MemoryExtractionResponse,
        prefer_local=True,
    )

    if isinstance(raw, dict):
        memories_data = raw.get("memories", [])
    elif hasattr(raw, "memories"):
        memories_data = [m.model_dump() if hasattr(m, "model_dump") else m for m in raw.memories]
    else:
        memories_data = []

    results = []
    for mem in memories_data:
        contradicts = mem.get("contradicts")
        if contradicts:
            memory_store.supersede_memory(contradicts, user_id, mem.get("content", ""))
        else:
            similar = memory_store.find_similar_memory(
                user_id, mem.get("content", ""), threshold=0.85
            )
            if similar:
                memory_store.reinforce_memory(similar["id"])
            else:
                memory_store.store_memory(user_id, mem)

        results.append(mem)

    return results


async def safe_extract_memories(
    user_id: str,
    user_message: str,
    assistant_response: str,
    memory_store,
) -> None:
    """Fire-and-forget wrapper. Catches ALL errors.

    A failed extraction means we miss one memory — not that the user's
    request fails. Called via asyncio.create_task() after the response is sent.
    """
    try:
        await extract_memories_from_turn(
            user_id=user_id,
            user_message=user_message,
            assistant_response=assistant_response,
            memory_store=memory_store,
        )
    except Exception as e:
        logger.debug("Memory extraction failed (non-blocking): %s", e)
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_extractor.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/memory/extractor.py tests/test_memory_extractor.py
git commit -m "feat(memory): add LLM-based memory extraction with contradiction and dedup handling"
```

---

### Task 5: Memory → Constraint Bridge

**Files:**
- Create: `app/services/memory/constraint_bridge.py`
- Create: `tests/test_memory_constraint_bridge.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_memory_constraint_bridge.py
"""Tests for converting memories into OR-Tools TimeSlot constraints."""

import pytest
from unittest.mock import MagicMock
from app.services.memory.constraint_bridge import memories_to_constraints


def test_no_memories_returns_empty():
    mock_store = MagicMock()
    mock_store.get_memories_by_type.return_value = []
    result = memories_to_constraints("u1", mock_store)
    assert result == []


def test_constraint_memory_becomes_timeslot():
    """A 'constraint' memory like 'No work after 6 PM' should produce a TimeSlot."""
    mock_store = MagicMock()
    mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: (
        [{"id": "m1", "content": "No tasks between 14:00 and 15:00 (has class)", "confidence": 0.9}]
        if mtype == "constraint" else []
    )

    result = memories_to_constraints("u1", mock_store)
    # Should produce at least one TimeSlot
    assert len(result) >= 1
    slot = result[0]
    assert slot.start_min == 840  # 14:00 = 14*60
    assert slot.end_min == 900    # 15:00 = 15*60
    assert slot.availability == "blocked"
    assert slot.source == "user"


def test_pattern_memory_becomes_soft_block():
    """A 'behavioral_pattern' memory should produce a soft (minimal_work) constraint."""
    mock_store = MagicMock()
    mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: (
        [{"id": "m2", "content": "User avoids tasks during hour 8 (skipped 85%)", "confidence": 0.85}]
        if mtype == "behavioral_pattern" else []
    )

    result = memories_to_constraints("u1", mock_store)
    assert len(result) >= 1
    slot = result[0]
    assert slot.start_min == 480  # 8:00 = 8*60
    assert slot.end_min == 540    # 9:00
    assert slot.availability == "minimal_work"
    assert slot.source == "pearl_inferred"


def test_mixed_constraints_and_patterns():
    """Both explicit constraints and inferred patterns should be returned."""
    mock_store = MagicMock()
    mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: {
        "constraint": [{"id": "c1", "content": "No tasks between 14:00 and 15:00 (has class)", "confidence": 0.9}],
        "behavioral_pattern": [{"id": "p1", "content": "User avoids tasks during hour 8 (skipped 85%)", "confidence": 0.85}],
    }.get(mtype, [])

    result = memories_to_constraints("u1", mock_store)
    assert len(result) == 2
    sources = {s.source for s in result}
    assert "user" in sources
    assert "pearl_inferred" in sources
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_constraint_bridge.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement constraint bridge**

```python
# app/services/memory/constraint_bridge.py
"""Bridge between archival memory and the deterministic scheduler.

This is what makes Jarvis different from ChatGPT's memory:
- ChatGPT memory: affects what the LLM says
- Jarvis memory: affects the MATHEMATICAL CONSTRAINTS in OR-Tools

A behavioral pattern like "user skips morning tasks" doesn't just make
Jarvis say "I notice you prefer afternoons" — it makes the scheduler
STOP SCHEDULING deep work before 10 AM.
"""

import re

from app.schemas.context import TimeSlot


def _parse_time_range(text: str) -> tuple[int, int] | None:
    """Extract a time range from natural language constraint text.

    Handles patterns like:
    - "between 14:00 and 15:00"
    - "from 2 PM to 3 PM"
    - "No tasks between 14:00 and 15:00"

    Returns (start_min, end_min) or None if no time range found.
    """
    # Pattern: HH:MM and HH:MM
    match_24h = re.search(r"(\d{1,2}):(\d{2})\s*(?:and|to|-)\s*(\d{1,2}):(\d{2})", text)
    if match_24h:
        start_h, start_m = int(match_24h.group(1)), int(match_24h.group(2))
        end_h, end_m = int(match_24h.group(3)), int(match_24h.group(4))
        return start_h * 60 + start_m, end_h * 60 + end_m

    # Pattern: N AM/PM to N AM/PM
    match_12h = re.search(
        r"(\d{1,2})\s*(AM|PM|am|pm)\s*(?:to|and|-)\s*(\d{1,2})\s*(AM|PM|am|pm)", text
    )
    if match_12h:
        start_h = int(match_12h.group(1))
        start_period = match_12h.group(2).upper()
        end_h = int(match_12h.group(3))
        end_period = match_12h.group(4).upper()

        if start_period == "PM" and start_h != 12:
            start_h += 12
        if start_period == "AM" and start_h == 12:
            start_h = 0
        if end_period == "PM" and end_h != 12:
            end_h += 12
        if end_period == "AM" and end_h == 12:
            end_h = 0

        return start_h * 60, end_h * 60

    # Pattern: "after N PM" → N PM to midnight
    match_after = re.search(r"after\s+(\d{1,2})\s*(AM|PM|am|pm)?", text)
    if match_after:
        h = int(match_after.group(1))
        period = (match_after.group(2) or "").upper()
        if period == "PM" and h != 12:
            h += 12
        return h * 60, 24 * 60

    # Pattern: "before N AM/PM" → midnight to N
    match_before = re.search(r"before\s+(\d{1,2})\s*(AM|PM|am|pm)?", text)
    if match_before:
        h = int(match_before.group(1))
        period = (match_before.group(2) or "").upper()
        if period == "PM" and h != 12:
            h += 12
        return 0, h * 60

    return None


def _parse_hour_from_pattern(text: str) -> int | None:
    """Extract hour from a PEARL pattern like 'avoids tasks during hour 8'."""
    match = re.search(r"hour\s+(\d{1,2})", text)
    if match:
        return int(match.group(1))
    return None


def memories_to_constraints(user_id: str, memory_store) -> list[TimeSlot]:
    """Convert relevant memories into TimeSlot constraints for OR-Tools.

    Called during _run_plan_day_flow, BEFORE run_schedule.
    Queries both explicit constraints and PEARL-inferred behavioral patterns.
    """
    constraints: list[TimeSlot] = []

    # 1. Explicit constraints (user stated)
    explicit = memory_store.get_memories_by_type(user_id, "constraint")
    for mem in explicit:
        time_range = _parse_time_range(mem.get("content", ""))
        if time_range:
            start_min, end_min = time_range
            constraints.append(TimeSlot(
                name=f"memory_constraint_{mem.get('id', '')}",
                start_min=start_min,
                end_min=end_min,
                availability="blocked",
                source="user",
            ))

    # 2. PEARL behavioral patterns (system inferred)
    patterns = memory_store.get_memories_by_type(
        user_id, "behavioral_pattern", min_confidence=0.6
    )
    for pattern in patterns:
        content = pattern.get("content", "")

        # Pattern: "avoids tasks during hour X"
        hour = _parse_hour_from_pattern(content)
        if hour is not None:
            constraints.append(TimeSlot(
                name=f"pearl_pattern_{pattern.get('id', '')}",
                start_min=hour * 60,
                end_min=(hour + 1) * 60,
                availability="minimal_work",
                source="pearl_inferred",
            ))

        # Pattern: time range in pattern text
        time_range = _parse_time_range(content)
        if time_range and hour is None:  # Don't duplicate if hour already matched
            start_min, end_min = time_range
            constraints.append(TimeSlot(
                name=f"pearl_pattern_{pattern.get('id', '')}",
                start_min=start_min,
                end_min=end_min,
                availability="minimal_work",
                source="pearl_inferred",
            ))

    return constraints
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_constraint_bridge.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/memory/constraint_bridge.py tests/test_memory_constraint_bridge.py
git commit -m "feat(memory): add memory→constraint bridge — memories change OR-Tools math"
```

---

### Task 6: Integration Test — Full Memory Pipeline

**Files:**
- Create: `tests/test_memory_integration.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/test_memory_integration.py
"""Integration tests for the full memory pipeline: extract → store → score → retrieve → bridge."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

from app.services.memory.store import MemoryStore
from app.services.memory.retriever import (
    compute_memory_strength,
    score_memory,
    format_memory_block,
    deduplicate_memories,
    IMPORTANCE_WEIGHTS,
)
from app.services.memory.constraint_bridge import memories_to_constraints, _parse_time_range
from app.schemas.memory import MemoryRecord, ExtractedMemory, MemoryExtractionResponse


class TestMemorySchemas:
    def test_memory_record_from_dict(self):
        record = MemoryRecord(
            id="m1", user_id="u1", memory_type="fact",
            content="CS student", confidence=0.8,
        )
        assert record.memory_type == "fact"
        assert record.confidence == 0.8
        assert record.stability == 1.0  # default

    def test_extracted_memory_validates_type(self):
        mem = ExtractedMemory(type="preference", content="Likes mornings")
        assert mem.type == "preference"

    def test_extraction_response_schema(self):
        resp = MemoryExtractionResponse(memories=[
            ExtractedMemory(type="fact", content="CS student"),
            ExtractedMemory(type="goal", content="Finish DSA by April"),
        ])
        assert len(resp.memories) == 2


class TestTimeRangeParsing:
    def test_24h_format(self):
        result = _parse_time_range("No tasks between 14:00 and 15:00")
        assert result == (840, 900)

    def test_12h_format(self):
        result = _parse_time_range("Meeting from 2 PM to 3 PM")
        assert result == (840, 900)

    def test_after_pattern(self):
        result = _parse_time_range("No work after 6 PM")
        assert result == (1080, 1440)

    def test_before_pattern(self):
        result = _parse_time_range("No tasks before 10 AM")
        assert result == (0, 600)

    def test_no_time_returns_none(self):
        result = _parse_time_range("User prefers quiet study")
        assert result is None


class TestDeduplication:
    def test_removes_duplicates_by_id(self):
        memories = [
            {"id": "m1", "content": "A"},
            {"id": "m2", "content": "B"},
            {"id": "m1", "content": "A duplicate"},
        ]
        result = deduplicate_memories(memories)
        assert len(result) == 2

    def test_preserves_order(self):
        memories = [
            {"id": "m1", "content": "First"},
            {"id": "m2", "content": "Second"},
        ]
        result = deduplicate_memories(memories)
        assert result[0]["content"] == "First"


class TestStabilityCapIntegration:
    def test_stability_cap_at_20(self):
        """Reinforcing a memory 25 times should cap stability at 20."""
        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"id": "m1", "stability": 19.5, "confidence": 0.9}
        ]
        mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [{"id": "m1"}]

        store = MemoryStore(supabase_client=mock_supabase)
        store.reinforce_memory("m1")

        # Check the update was called with capped stability
        update_call = mock_supabase.table.return_value.update.call_args
        updates = update_call[0][0] if update_call[0] else update_call[1]
        assert updates["stability"] == 20.0  # Capped, not 20.5


class TestConstraintBridgeIntegration:
    def test_constraint_and_pattern_both_produce_slots(self):
        """End-to-end: memories of different types produce different TimeSlot types."""
        mock_store = MagicMock()
        mock_store.get_memories_by_type.side_effect = lambda uid, mtype, **kw: {
            "constraint": [{"id": "c1", "content": "Meeting from 2 PM to 3 PM", "confidence": 0.9}],
            "behavioral_pattern": [{"id": "p1", "content": "User avoids tasks during hour 9 (skipped 80%)", "confidence": 0.8}],
        }.get(mtype, [])

        slots = memories_to_constraints("u1", mock_store)
        assert len(slots) == 2

        blocked = [s for s in slots if s.availability == "blocked"]
        soft = [s for s in slots if s.availability == "minimal_work"]
        assert len(blocked) == 1  # Explicit constraint
        assert len(soft) == 1     # PEARL pattern
        assert blocked[0].source == "user"
        assert soft[0].source == "pearl_inferred"
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full Phase 1B test suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_memory_store.py tests/test_memory_retriever.py tests/test_memory_extractor.py tests/test_memory_constraint_bridge.py tests/test_memory_integration.py -v`
Expected: All tests PASS

- [ ] **Step 4: Run combined Phase 1A + 1B suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_registry.py tests/test_intent_routing.py tests/test_draft_store.py tests/test_core_pipeline.py tests/test_memory_store.py tests/test_memory_retriever.py tests/test_memory_extractor.py tests/test_memory_constraint_bridge.py tests/test_memory_integration.py -v`
Expected: All tests PASS (32 from Phase 1A + new from Phase 1B)

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add tests/test_memory_integration.py
git commit -m "test: add memory pipeline integration tests — schemas, parsing, dedup, bridge"
```

---

## Phase 1B Complete Checklist

After completing all 6 tasks, verify:

- [ ] Shared embedding utilities extracted to `app/utils/embedding.py`
- [ ] Memory schemas defined in `app/schemas/memory.py` (MemoryRecord, ExtractedMemory, MemoryExtractionResponse)
- [ ] `MemoryStore` handles CRUD, reinforcement, supersede, similarity search
- [ ] SM-2 decay scoring works (compute_memory_strength, score_memory)
- [ ] Memory context formatted for LLM injection (format_memory_block)
- [ ] Memory extraction pipeline extracts from conversation turns (fire-and-forget)
- [ ] Contradiction detection supersedes old memories
- [ ] Duplicate detection reinforces existing memories
- [ ] Memory → Constraint Bridge converts memories to TimeSlots
- [ ] Time range parsing handles 24h, 12h, "after X", "before X" formats
- [ ] PEARL patterns become soft blocks (minimal_work) in OR-Tools
- [ ] All tests pass: Phase 1A (32) + Phase 1B (new)

**Next phase:** Phase 1C (Document Intelligence) — depends on the MemoryStore and embedding utilities built here.
