# tests/test_memory_extractor.py
"""Tests for LLM-based memory extraction from conversation turns."""

import pytest
from unittest.mock import MagicMock

from app.domain.entities import LLMLocation
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.schemas.memory import MemoryExtractionResponse
from app.services.memory.extractor import (
    extract_memories_from_turn,
    safe_extract_memories,
    EXTRACTION_PROMPT,
)


class _ExtractionModel(LLMProvider):
    """The only model in the provider chain: answers every request with ``reply``."""

    name = "extraction"
    location = LLMLocation.CLOUD

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls: list[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    async def complete(self, req: LLMRequest) -> MemoryExtractionResponse:
        self.calls.append(req)
        return MemoryExtractionResponse.model_validate(self.reply)

    async def stream(self, req: LLMRequest):  # pragma: no cover - extraction never streams
        raise NotImplementedError
        yield


def _extraction_returns(reply: dict) -> _ExtractionModel:
    model = _ExtractionModel(reply)
    set_chain(FallbackChainProvider([model]))  # conftest's _fresh_llm_chain resets it
    return model


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

    model = _extraction_returns({"memories": [
        {"type": "fact", "content": "User is a CS student", "confidence": 0.8, "contradicts": None, "expires_at": None}
    ]})
    result = await extract_memories_from_turn(
        user_id="u1",
        user_message="I'm a CS student at VIT",
        assistant_response="Great! I'll keep that in mind.",
        memory_store=mock_store,
    )

    assert [c.task for c in model.calls] == ["memory_extraction"]
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

    _extraction_returns({"memories": [
        {"type": "preference", "content": "User prefers evenings", "confidence": 0.7, "contradicts": "old-1", "expires_at": None}
    ]})
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

    _extraction_returns({"memories": [
        {"type": "fact", "content": "User is a CS student", "confidence": 0.8, "contradicts": None, "expires_at": None}
    ]})
    await extract_memories_from_turn(
        user_id="u1",
        user_message="As a CS student, I need...",
        assistant_response="Sure!",
        memory_store=mock_store,
    )

    mock_store.reinforce_memory.assert_called_once_with("existing-1", user_id="u1")
    mock_store.store_memory.assert_not_called()


@pytest.mark.asyncio
async def test_extract_returns_empty_when_nothing_new():
    """When LLM finds nothing new, return empty list."""
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []

    _extraction_returns({"memories": []})
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


# --- legacy PEARL: never once the composition root is registered --------------------


async def _safe_extract_with_db(monkeypatch, *, with_container: bool) -> list[str]:
    """Run the v1 fire-and-forget extraction with a db client; return the users the
    legacy PEARL detector ran for."""
    import app.core.config as config
    from app.container import AppContainer
    from app.core.runtime import set_container
    from app.orchestrator.hooks import ActionHooks

    ran_for: list[str] = []
    monkeypatch.setattr(
        "app.services.memory.pearl.detect_patterns",
        lambda user_id, *_args, **_kwargs: ran_for.append(user_id),
    )
    if with_container:
        set_container(AppContainer.build(
            supabase_client=None, cfg=config, hooks=ActionHooks(), install_chain=False,
            embedder=MagicMock(),
        ))
    mock_store = MagicMock()
    mock_store.get_active_memories.return_value = []
    _extraction_returns({"memories": []})

    await safe_extract_memories(
        user_id="u1",
        user_message="I prefer mornings",
        assistant_response="Noted.",
        memory_store=mock_store,
        db_client=MagicMock(),
    )
    return ran_for


@pytest.mark.asyncio
async def test_safe_extract__composition_root_registered__legacy_pearl_never_runs(monkeypatch):
    assert await _safe_extract_with_db(monkeypatch, with_container=True) == []


@pytest.mark.asyncio
async def test_safe_extract__no_composition_root__legacy_pearl_still_runs(monkeypatch):
    assert await _safe_extract_with_db(monkeypatch, with_container=False) == ["u1"]
