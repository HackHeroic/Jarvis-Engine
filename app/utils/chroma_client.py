"""ChromaDB client and the user-scoped document search.

One client per process (Cloud when credentials are set, else local), built on
first use. ``query_knowledge`` and ``search_knowledge`` are synchronous — they talk
to Chroma over the network — so async callers run them with ``asyncio.to_thread``.
"""

import threading
from dataclasses import dataclass

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


def _max_distance(collection, max_cosine_distance: float | None = None) -> float:
    """The cut-off in the collection's own distance unit.

    ``max_cosine_distance`` is the cut-off as cosine distance (1 - cosine similarity);
    None means the general one, ``RAG_MAX_COSINE_DISTANCE``. Every writer creates the
    collection with cosine space; one created without it is Chroma's default, squared
    L2, which for the normalised default embeddings is exactly twice the cosine distance.
    """
    cosine = RAG_MAX_COSINE_DISTANCE if max_cosine_distance is None else max_cosine_distance
    space = (getattr(collection, "metadata", None) or {}).get("hnsw:space", "l2")
    return cosine * 2 if space == "l2" else cosine


@dataclass(frozen=True)
class KnowledgeHit:
    """One chunk a search found: its text, the document it came from, and its distance
    from the query (in the collection's own unit: smaller is closer)."""

    text: str
    source_id: str | None
    distance: float


def search_knowledge(
    user_id: str,
    query: str,
    n_results: int = 5,
    *,
    max_cosine_distance: float | None = None,
    source_id: str | None = None,
) -> list[KnowledgeHit]:
    """``user_id``'s chunks closest to ``query``, closest first, with their documents.

    Only chunks within ``max_cosine_distance`` (cosine distance; None: the general
    ``RAG_MAX_COSINE_DISTANCE``) come back. With ``source_id`` the search ranks that one
    document's chunks only. Always filtered by ``user_id``: a missing one is a programming
    error and raises ``ValueError``. Chroma being unreachable yields ``[]`` (logged by the
    exception's type): the caller proceeds without document context.
    """
    if not user_id:
        raise ValueError("search_knowledge needs a user_id: the search is scoped to one user")
    where: dict = {"user_id": user_id}
    if source_id is not None:
        where = {"$and": [{"source_id": source_id}, {"user_id": user_id}]}
    try:
        collection = get_chroma_client().get_or_create_collection(
            KNOWLEDGE_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        results = collection.query(query_texts=[query], n_results=n_results, where=where)
    except Exception as exc:
        JARVIS_LOGGER.warning("Document search failed (%s)", type(exc).__name__)
        return []
    documents = (results.get("documents") or [[]])[0] if results else []
    distances = (results.get("distances") or [[]])[0] if results else []
    metadatas = ((results.get("metadatas") or [[]])[0] if results else None) or []
    cutoff = _max_distance(collection, max_cosine_distance)
    hits = [
        KnowledgeHit(
            text=doc,
            source_id=((metadatas[i] if i < len(metadatas) else None) or {}).get("source_id"),
            distance=distance,
        )
        for i, (doc, distance) in enumerate(zip(documents, distances, strict=False))
        if doc and distance is not None and distance <= cutoff
    ]
    return sorted(hits, key=lambda hit: hit.distance)


def query_knowledge(user_id: str, query: str, n_results: int = 5) -> list[str]:
    """``user_id``'s document chunks closest to ``query``; only matches within the cut-off.

    Always filtered by ``user_id`` — a user with no matching chunks gets ``[]``,
    never anyone else's. A missing ``user_id`` is a programming error and raises
    ``ValueError``; callers without one skip the search. Chroma being unreachable
    yields ``[]`` (logged): the caller proceeds without document context.
    """
    if not user_id:
        raise ValueError("query_knowledge needs a user_id: the search is scoped to one user")
    return [hit.text for hit in search_knowledge(user_id, query, n_results)]
