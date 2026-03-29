"""Shared embedding and similarity utilities.

Used by both the memory system and the task-material linker.
Embedding model: all-MiniLM-L6-v2 (384 dimensions) via ChromaDB.
Runs locally — no API calls, no cost, ~5ms per embedding.
"""

import functools
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


@functools.lru_cache(maxsize=1)
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
