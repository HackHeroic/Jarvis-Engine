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


# --- Claude first, Gemini as the fallback ---------------------------------------------


def _providers(monkeypatch, *, claude: str, gemini: str) -> list[str]:
    asked: list[str] = []

    async def fake_claude(*, user_prompt: str, system_prompt: str) -> str:
        asked.append("claude")
        return claude

    async def fake_gemini(*, user_prompt: str, system_prompt: str) -> str:
        asked.append("gemini")
        return gemini

    monkeypatch.setattr(workspace_builder, "claude_web_search", fake_claude)
    monkeypatch.setattr(workspace_builder, "gemini_web_search", fake_gemini)
    return asked


class _Recorder:
    def __init__(self) -> None:
        self.events: list = []

    async def handle(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_search_web__claude_answers__gemini_not_asked_and_metered_as_claude(monkeypatch):
    from app.domain.events import LLMCallCompleted

    asked = _providers(monkeypatch, claude="https://a.example | A", gemini="unused")
    bus, seen = EventBus(), _Recorder()
    bus.subscribe(LLMCallCompleted, seen)

    assert await _search_web("graphs", "find links", bus=bus) == "https://a.example | A"

    assert asked == ["claude"]
    assert [e.usage.provider for e in seen.events] == ["claude"]
    assert seen.events[0].usage.task == "web_search"


@pytest.mark.asyncio
async def test_search_web__claude_has_no_answer__gemini_answers_and_is_metered(monkeypatch):
    from app.domain.events import LLMCallCompleted

    asked = _providers(monkeypatch, claude="", gemini="https://g.example | G")
    bus, seen = EventBus(), _Recorder()
    bus.subscribe(LLMCallCompleted, seen)

    assert await _search_web("graphs", "find links", bus=bus) == "https://g.example | G"

    assert asked == ["claude", "gemini"]
    assert [e.usage.provider for e in seen.events] == ["gemini"]  # an empty answer bills nothing


@pytest.mark.asyncio
async def test_search_web__neither_answers__unavailable_and_cooldown_starts(monkeypatch):
    asked = _providers(monkeypatch, claude="", gemini="")

    with pytest.raises(WebSearchUnavailableError):
        await _search_web("graphs", "find links", bus=EventBus())
    with pytest.raises(WebSearchUnavailableError):
        await _search_web("graphs", "find links", bus=EventBus())

    assert asked == ["claude", "gemini"]  # the second call waited on neither


# --- Claude's search: only links the search returned ----------------------------------


class _Block:
    def __init__(self, type: str, **fields) -> None:
        self.type = type
        self.__dict__.update(fields)


class _Usage:
    input_tokens, output_tokens, cache_read_input_tokens = 100, 20, 0


class _Reply:
    stop_reason = "end_turn"
    usage = _Usage()

    def __init__(self, content: list) -> None:
        self.content = content


def _claude_replying(monkeypatch, content: list) -> None:
    import app.core.config as cfg
    from app.infrastructure.llm import claude

    class _Messages:
        async def create(self, **_params):
            return _Reply(content)

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(claude, "shared_client", lambda _cfg: _Client())


@pytest.mark.asyncio
async def test_claude_web_search__link_not_in_the_results__line_dropped(monkeypatch):
    from app.models.brain.claude_web_search import claude_web_search

    _claude_replying(monkeypatch, [
        _Block("web_search_tool_result", content=[_Block("web_search_result", url="https://found.example/guide/")]),
        _Block("text", text="Article: https://found.example/guide | A guide\n"),
        _Block("text", text="YouTube: https://youtube.com/watch?v=recalled | From memory"),
    ])

    assert await claude_web_search("normalisation") == "Article: https://found.example/guide | A guide"


@pytest.mark.asyncio
async def test_claude_web_search__no_search_ran__empty_not_links_from_memory(monkeypatch):
    from app.models.brain.claude_web_search import claude_web_search

    _claude_replying(monkeypatch, [
        _Block("text", text="I could not search. Try https://youtube.com/watch?v=recalled | A video"),
    ])

    assert await claude_web_search("normalisation") == ""


@pytest.mark.asyncio
async def test_claude_web_search__search_errored__empty(monkeypatch):
    from app.models.brain.claude_web_search import claude_web_search

    _claude_replying(monkeypatch, [
        _Block("web_search_tool_result", content=_Block("web_search_tool_result_error", error_code="unavailable")),
        _Block("text", text="https://youtube.com/watch?v=recalled | A video"),
    ])

    assert await claude_web_search("normalisation") == ""


@pytest.mark.asyncio
async def test_claude_web_search__no_credential__empty_and_no_call():
    from app.models.brain.claude_web_search import claude_web_search

    assert await claude_web_search("normalisation") == ""  # conftest clears the key
