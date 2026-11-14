"""ChromaDB client and the user-scoped document search.

One client per process (Cloud when credentials are set, else local), built on
first use. ``query_knowledge`` is synchronous — it talks to Chroma over the
network — so async callers run it with ``asyncio.to_thread``.
"""

import threading

import chromadb
from chromadb.config import Settings

from app.core.config import CHROMA_API_KEY, CHROMA_DATABASE, CHROMA_TENANT, RAG_MAX_COSINE_DISTANCE
from app.core.jarvis_logger import JARVIS_LOGGER

KNOWLEDGE_COLLECTION = "jarvis_knowledge"

_client = None
_client_lock = threading.Lock()


def get_chroma_client():
    """The process's ChromaDB client: Cloud when CHROMA_API_KEY is set, else local. Built once."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:  # callers run on worker threads; build it exactly once
        if _client is None:
            if CHROMA_API_KEY and CHROMA_TENANT:
                _client = chromadb.CloudClient(
                    api_key=CHROMA_API_KEY,
                    tenant=CHROMA_TENANT,
                    database=CHROMA_DATABASE,
                )
                JARVIS_LOGGER.info(
                    "Connected to ChromaDB Cloud (tenant=%s, db=%s)", CHROMA_TENANT, CHROMA_DATABASE
                )
            else:
                _client = chromadb.Client(Settings(anonymized_telemetry=False))
                JARVIS_LOGGER.info("Connected to ChromaDB (local mode)")
    return _client


def _max_distance(collection) -> float:
    """The cut-off in the collection's own distance unit.

    Every writer creates the collection with cosine space; one created without it
    is Chroma's default, squared L2, which for the normalised default embeddings
    is exactly twice the cosine distance.
    """
    space = (getattr(collection, "metadata", None) or {}).get("hnsw:space", "l2")
    return RAG_MAX_COSINE_DISTANCE * 2 if space == "l2" else RAG_MAX_COSINE_DISTANCE


def query_knowledge(user_id: str, query: str, n_results: int = 5) -> list[str]:
    """``user_id``'s document chunks closest to ``query``; only matches within the cut-off.

    Always filtered by ``user_id`` — a user with no matching chunks gets ``[]``,
    never anyone else's. A missing ``user_id`` is a programming error and raises
    ``ValueError``; callers without one skip the search. Chroma being unreachable
    yields ``[]`` (logged): the caller proceeds without document context.
    """
    if not user_id:
        raise ValueError("query_knowledge needs a user_id: the search is scoped to one user")
    try:
        collection = get_chroma_client().get_or_create_collection(
            KNOWLEDGE_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        results = collection.query(
            query_texts=[query],
            n_results=n_results,
            where={"user_id": user_id},
        )
    except Exception as exc:
        JARVIS_LOGGER.warning(f"Document search failed: {exc!r}")
        return []
    documents = (results.get("documents") or [[]])[0] if results else []
    distances = (results.get("distances") or [[]])[0] if results else []
    cutoff = _max_distance(collection)
    return [
        doc
        for doc, distance in zip(documents, distances, strict=False)
        if doc and distance is not None and distance <= cutoff
    ]
