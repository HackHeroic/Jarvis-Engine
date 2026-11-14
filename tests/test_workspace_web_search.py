"""The workspace's web search: a search that just failed is not waited on again."""

import pytest

from app.domain.events import EventBus
from app.services.analytical import workspace_builder
from app.services.analytical.workspace_builder import (
    WebSearchUnavailableError,
    _search_web,
    web_search_cooldown,
)


def _count_searches(monkeypatch, answers: list[str]) -> list[str]:
    calls: list[str] = []

    async def fake_search(*, user_prompt: str, system_prompt: str) -> str:
        calls.append(user_prompt)
        return answers.pop(0)

    monkeypatch.setattr(workspace_builder, "gemini_web_search", fake_search)
    return calls


@pytest.mark.asyncio
async def test_search_web__search_just_failed__not_called_again_during_the_cooldown(monkeypatch):
    calls = _count_searches(monkeypatch, ["", "would answer now"])

    with pytest.raises(WebSearchUnavailableError):
        await _search_web("graphs", "find links", bus=EventBus())
    with pytest.raises(WebSearchUnavailableError):
        await _search_web("graphs", "find links", bus=EventBus())

    assert len(calls) == 1  # the second workspace did not wait on a search known to fail


@pytest.mark.asyncio
async def test_search_web__cooldown_over__searched_again(monkeypatch):
    calls = _count_searches(monkeypatch, ["", "three links"])

    with pytest.raises(WebSearchUnavailableError):
        await _search_web("graphs", "find links", bus=EventBus())
    web_search_cooldown.reset()  # the cooldown has passed

    assert await _search_web("graphs", "find links", bus=EventBus()) == "three links"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_search_web__search_answers__no_cooldown(monkeypatch):
    calls = _count_searches(monkeypatch, ["links", "more links"])

    await _search_web("graphs", "find links", bus=EventBus())
    await _search_web("trees", "find links", bus=EventBus())

    assert len(calls) == 2
