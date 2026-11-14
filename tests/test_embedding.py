"""The TextEmbedder adapters: the local sentence model, and the test stand-in."""

import pytest

from app.domain.errors import InfrastructureError
from app.infrastructure.embedding import MiniLMEmbedder
from app.infrastructure.persistence._mapping import MEMORY_EMBEDDING_DIMENSIONS
from tests.fakes import BagOfWordsEmbedder


def test_minilm_embedder__model_unavailable__raises_infrastructure_error(monkeypatch):
    monkeypatch.setattr(
        "app.infrastructure.embedding.embed_texts", lambda texts: [None] * len(texts)
    )

    with pytest.raises(InfrastructureError) as info:
        MiniLMEmbedder().embed(["Likes tea"])

    assert info.value.message == "Text embeddings are unavailable."


def test_minilm_embedder__model_raises__infrastructure_error_chained(monkeypatch):
    def boom(texts):
        raise OSError("model download failed: secret-host")

    monkeypatch.setattr("app.infrastructure.embedding.embed_texts", boom)

    with pytest.raises(InfrastructureError) as info:
        MiniLMEmbedder().embed(["Likes tea"])

    assert isinstance(info.value.__cause__, OSError)
    assert "secret-host" not in info.value.message


def test_minilm_embedder__model_available__one_float_vector_per_text(monkeypatch):
    monkeypatch.setattr(
        "app.infrastructure.embedding.embed_texts", lambda texts: [[1, 0.5]] * len(texts)
    )

    monkeypatch.setattr(MiniLMEmbedder, "dimensions", 2)

    assert MiniLMEmbedder().embed(["a", "b"]) == [[1.0, 0.5], [1.0, 0.5]]
    assert MiniLMEmbedder().embed([]) == []


def test_minilm_embedder__dimensions__match_the_stored_embedding_width():
    assert MiniLMEmbedder.dimensions == MEMORY_EMBEDDING_DIMENSIONS == 384


def test_minilm_embedder__vector_of_the_wrong_length__raises_infrastructure_error(monkeypatch):
    monkeypatch.setattr(
        "app.infrastructure.embedding.embed_texts", lambda texts: [[0.1] * 383] * len(texts)
    )

    with pytest.raises(InfrastructureError) as info:
        MiniLMEmbedder().embed(["Likes tea"])

    assert info.value.message == "Text embeddings are unavailable."


def test_bag_of_words_embedder__shared_words__closer_than_unrelated():
    from app.utils.embedding import cosine_similarity

    tea, more_tea, maths = BagOfWordsEmbedder().embed(
        ["likes green tea", "likes jasmine tea", "studies linear algebra"]
    )

    assert len(tea) == BagOfWordsEmbedder().dimensions == 384
    assert cosine_similarity(tea, more_tea) > cosine_similarity(tea, maths)
