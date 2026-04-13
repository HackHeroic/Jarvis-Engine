"""ChromaDB client factory. Uses CloudClient when credentials are set, else local Client."""

import chromadb
from chromadb.config import Settings

from app.core.config import CHROMA_API_KEY, CHROMA_DATABASE, CHROMA_TENANT
from app.core.jarvis_logger import JARVIS_LOGGER


def get_chroma_client():
    """Return ChromaDB client. Cloud when CHROMA_API_KEY is set, else local."""
    if CHROMA_API_KEY and CHROMA_TENANT:
        client = chromadb.CloudClient(
            api_key=CHROMA_API_KEY,
            tenant=CHROMA_TENANT,
            database=CHROMA_DATABASE,
        )
        JARVIS_LOGGER.info("Successfully connected to ChromaDB Cloud (tenant=%s, db=%s)", CHROMA_TENANT, CHROMA_DATABASE)
        return client
    client = chromadb.Client(Settings(anonymized_telemetry=False))
    JARVIS_LOGGER.info("Successfully connected to ChromaDB (local mode)")
    return client


def query_knowledge(user_id: str, query: str, n_results: int = 5) -> list[str]:
    """Query ChromaDB for relevant knowledge chunks. Returns list of text chunks."""
    try:
        client = get_chroma_client()
        collection = client.get_or_create_collection("jarvis_knowledge", metadata={"hnsw:space": "cosine"})
        # Try with user_id filter first (data isolation)
        try:
            results = collection.query(
                query_texts=[query],
                n_results=n_results,
                where={"user_id": user_id},
            )
            if results and results.get("documents") and results["documents"][0]:
                return [doc for doc in results["documents"][0] if doc]
        except Exception:
            pass
        # Fallback: query without filter (for chunks ingested before user_id was added)
        results = collection.query(
            query_texts=[query],
            n_results=n_results,
        )
        if results and results.get("documents") and len(results["documents"]) > 0:
            return [doc for doc in results["documents"][0] if doc]
        return []
    except Exception:
        return []
