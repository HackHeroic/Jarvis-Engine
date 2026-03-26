"""ChromaDB client factory. Uses CloudClient when credentials are set, else local Client."""

import chromadb
from chromadb.config import Settings

from app.core.config import CHROMA_API_KEY, CHROMA_DATABASE, CHROMA_TENANT


def get_chroma_client():
    """Return ChromaDB client. Cloud when CHROMA_API_KEY is set, else local."""
    if CHROMA_API_KEY and CHROMA_TENANT:
        return chromadb.CloudClient(
            api_key=CHROMA_API_KEY,
            tenant=CHROMA_TENANT,
            database=CHROMA_DATABASE,
        )
    return chromadb.Client(Settings(anonymized_telemetry=False))
