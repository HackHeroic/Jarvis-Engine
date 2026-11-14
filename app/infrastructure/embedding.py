"""Text embedders: the ``TextEmbedder`` port over a local sentence model (an Adapter)."""

from app.domain.errors import InfrastructureError
from app.domain.ports import TextEmbedder
from app.utils.embedding import embed_texts

_UNAVAILABLE = "Text embeddings are unavailable."


class MiniLMEmbedder(TextEmbedder):
    """all-MiniLM-L6-v2 sentence embeddings, computed on this machine's CPU.

    384 dimensions, the width of the user_memories.embedding column. Raises
    InfrastructureError when the model cannot run (chromadb missing, model not
    downloaded) or answers with a vector of any other length; the driver's text is
    chained, never repeated.
    """

    dimensions = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors = embed_texts(texts)
        except Exception as exc:  # any model load or inference failure
            raise InfrastructureError(_UNAVAILABLE) from exc
        if len(vectors) != len(texts) or any(
            v is None or len(v) != self.dimensions for v in vectors
        ):
            raise InfrastructureError(_UNAVAILABLE)
        return [[float(x) for x in v] for v in vectors]
