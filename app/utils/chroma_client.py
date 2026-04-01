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
