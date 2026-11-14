"""Long-term memory: what Jarvis remembers across conversations, and what it lets go.

A fact mentioned in one conversation is in the model's context in the next. A
contradiction replaces the old fact; "forget" removes it; dismissing a learned
pattern hides it and confirming it raises its confidence. Memories fade with
time since they were last reinforced (SM-2 style decay) and drop out of context
below the retention floor. Nobody else's memories are ever in your context.

The memory extraction model is scripted to react only to the exchange in front of
it (the "User:" line of its prompt), the way a real extractor would; ids it cites
for contradictions are read off the EXISTING MEMORIES list in that prompt.
"""

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest

import app.services.memory.retriever as retriever_module
import app.services.memory.store as store_module
from app.services.memory.retriever import build_memory_context

MARATHON = "Training for the Mumbai marathon in March"
MORNINGS = "Prefers studying in the mornings"
NIGHTS = "Now studies best late at night"
_T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _script_memories(llm, rules: dict) -> None:
    """When the exchange's user line contains a trigger, extract that rule's memory.

    A rule's memory is a dict, or a callable taking the extraction prompt (so it
    can cite the id of an existing memory it contradicts).
    """

    def reply(req) -> str:
        user_line = re.search(r"^User: (.*)$", req.prompt, flags=re.MULTILINE).group(1)
        memories = [
            memory(req.prompt) if callable(memory) else memory
            for trigger, memory in rules.items() if trigger in user_line
        ]
        return json.dumps({"memories": memories})

    llm.script("memory_extraction", reply)


def _contradicting_mornings(prompt: str) -> dict:
    match = re.search(rf"^\[([^\]]+)\] \(preference\) {MORNINGS}$", prompt, flags=re.MULTILINE)
    return {
        "type": "preference",
        "content": NIGHTS,
        "confidence": 0.8,
        "contradicts": match.group(1) if match else None,
    }


_RULES = {
    "marathon": {"type": "goal", "content": MARATHON, "confidence": 0.8},
    "in the mornings": {"type": "preference", "content": MORNINGS, "confidence": 0.8},
    "late at night": _contradicting_mornings,
}


def _system_prompt_of_reply(llm, since: int) -> str:
    (reply_call,) = [c for c in llm.calls[since:] if c.task == "voice_of_jarvis"]
    return reply_call.system_prompt


def _memories(jarvis, user="alice", content=None) -> list[dict]:
    rows = jarvis.rows("user_memories", user_id=user)
    return [r for r in rows if content is None or r["content"] == content]


@asynccontextmanager
async def _memory_api(jarvis, monkeypatch):
    """The memories REST API (the Memory panel), over this test's composition root."""
    from app.main import app

    monkeypatch.setattr(app.state, "container", jarvis.state.container, raising=False)
    monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def frozen_memory_clock(clock):
    """Memory writes and decay read one controllable clock."""
    clock.freeze(_T0, store_module, retriever_module)
    return clock


# --- remembering across conversations ------------------------------------------


@pytest.mark.asyncio
async def test_memory__fact_from_one_conversation__is_in_the_models_context_in_the_next(
    jarvis, llm
):
    _script_memories(llm, _RULES)
    first = await jarvis.say("I'm training for the Mumbai marathon in March, so mornings are runs")
    calls_before = len(llm.calls)

    second = await jarvis.say("What should I focus on this week?")

    assert second.conversation_id != first.conversation_id
    assert MARATHON in _system_prompt_of_reply(llm, calls_before)
    assert MARATHON in [m["content"] for m in second.payload["memories"]]


@pytest.mark.asyncio
async def test_memory__fact_mentioned_again__reinforced_not_stored_twice(jarvis, llm):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March")
    (stored,) = _memories(jarvis, content=MARATHON)

    await jarvis.say("Long run today for the marathon")

    (again,) = _memories(jarvis, content=MARATHON)
    assert again["id"] == stored["id"]
    assert again["stability"] > stored["stability"]
    assert again["confidence"] > stored["confidence"]


@pytest.mark.asyncio
async def test_memory__contradiction__supersedes_the_old_fact_everywhere(jarvis, llm):
    _script_memories(llm, _RULES)
    await jarvis.say("I prefer studying in the mornings")
    (old,) = _memories(jarvis, content=MORNINGS)

    await jarvis.say("Actually I've switched: I study best late at night now")
    calls_before = len(llm.calls)
    await jarvis.say("Plan nothing yet, just checking in about my week")

    (new,) = _memories(jarvis, content=NIGHTS)
    assert _memories(jarvis, content=MORNINGS)[0]["superseded_by"] == new["id"]
    assert old["id"] != new["id"]
    context = _system_prompt_of_reply(llm, calls_before)
    assert NIGHTS in context
    assert MORNINGS not in context
    assert MORNINGS not in build_memory_context("alice", jarvis.memory_store)


# --- the user's controls -------------------------------------------------------


@pytest.mark.asyncio
async def test_memory__forget__gone_from_context_and_the_memory_panel(jarvis, llm, monkeypatch):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March")
    (memory,) = _memories(jarvis, content=MARATHON)

    async with _memory_api(jarvis, monkeypatch) as api:
        forgot = await api.delete(f"/api/v1/memories/{memory['id']}", params={"user_id": "alice"})
        panel = await api.get("/api/v1/memories/", params={"user_id": "alice"})

    assert forgot.status_code == 200
    assert MARATHON not in [m["content"] for m in panel.json()["memories"]]
    calls_before = len(llm.calls)
    await jarvis.say("What should I focus on this week?")
    assert MARATHON not in _system_prompt_of_reply(llm, calls_before)


@pytest.mark.asyncio
async def test_memory__dismiss_hides_and_confirm_raises_confidence(jarvis, llm, monkeypatch):
    from app.domain.entities import MemorySource
    from app.services.memory.memory_service import MemoryCandidate

    memory = jarvis.state.container.memory_service
    dismissed, confirmed = [
        (
            await memory.remember(
                "alice",
                MemoryCandidate(memory_type="behavioral_pattern", content=content, confidence=0.8),
                provenance=MemorySource.INFERRED,
                origin="behavior",
            )
        ).memory
        for content in ("Skips work around 9 AM", "Most productive after lunch")
    ]

    async with _memory_api(jarvis, monkeypatch) as api:
        r1 = await api.post(f"/api/v1/memories/{dismissed.id}/dismiss", params={"user_id": "alice"})
        r2 = await api.post(f"/api/v1/memories/{confirmed.id}/confirm", params={"user_id": "alice"})
        panel = (await api.get("/api/v1/memories/", params={"user_id": "alice"})).json()

    assert (r1.status_code, r2.status_code) == (200, 200)
    confidence = {m["memory_id"]: m["confidence"] for m in panel["memories"]}
    assert dismissed.id not in confidence
    assert confidence[confirmed.id] == pytest.approx(0.9)
    # Hidden from the model's context too, not only from the panel.
    context = build_memory_context("alice", jarvis.memory_store)
    assert dismissed.content not in context
    assert confirmed.content in context


# --- fading -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory__unused_for_a_week__still_in_context(jarvis, llm, frozen_memory_clock):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March")

    frozen_memory_clock.advance(days=7)

    assert MARATHON in build_memory_context("alice", jarvis.memory_store)


@pytest.mark.asyncio
async def test_memory__unused_for_a_month__fades_below_the_floor_and_leaves_context(
    jarvis, llm, frozen_memory_clock
):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March")

    frozen_memory_clock.advance(days=30)
    calls_before = len(llm.calls)
    turn = await jarvis.say("What should I focus on this week?")

    assert MARATHON not in _system_prompt_of_reply(llm, calls_before)
    assert MARATHON not in [m["content"] for m in (turn.payload["memories"] or [])]


@pytest.mark.asyncio
async def test_memory__mentioned_again_after_three_weeks__survives_the_month(
    jarvis, llm, frozen_memory_clock
):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March")
    frozen_memory_clock.advance(days=21)
    await jarvis.say("Did a long run for the marathon this morning")

    frozen_memory_clock.advance(days=9)  # 30 days after it was first learned

    assert MARATHON in build_memory_context("alice", jarvis.memory_store)


@pytest.mark.asyncio
async def test_memory__confirmed_pattern__outlasts_an_unconfirmed_one(
    jarvis, llm, monkeypatch, frozen_memory_clock
):
    for content in ("Skips work around 9 AM", "Most productive after lunch"):
        jarvis.memory_store.store_memory(
            "alice", {"type": "behavioral_pattern", "content": content, "confidence": 0.8}
        )
    unconfirmed, confirmed = _memories(jarvis)
    async with _memory_api(jarvis, monkeypatch) as api:
        await api.post(f"/api/v1/memories/{confirmed['id']}/confirm", params={"user_id": "alice"})

    frozen_memory_clock.advance(days=30)

    context = build_memory_context("alice", jarvis.memory_store)
    assert confirmed["content"] in context
    assert unconfirmed["content"] not in context


# --- isolation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory__another_users_memories__never_reach_my_context_or_panel(
    jarvis, llm, monkeypatch
):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March", user="alice")
    (alices,) = _memories(jarvis, user="alice", content=MARATHON)
    calls_before = len(llm.calls)

    await jarvis.say("What should I focus on this week?", user="bob")

    assert MARATHON not in _system_prompt_of_reply(llm, calls_before)
    # Bob's own extraction is not shown Alice's memories as "existing" ones either.
    for call in llm.calls[calls_before:]:
        if call.task == "memory_extraction":
            assert MARATHON not in call.prompt.split("CURRENT EXCHANGE")[0]
    async with _memory_api(jarvis, monkeypatch) as api:
        panel = (await api.get("/api/v1/memories/", params={"user_id": "bob"})).json()
        forget = await api.delete(f"/api/v1/memories/{alices['id']}", params={"user_id": "bob"})
        confirm = await api.post(f"/api/v1/memories/{alices['id']}/confirm", params={"user_id": "bob"})
    assert panel["memories"] == []
    assert (forget.status_code, confirm.status_code) == (404, 404)
    assert _memories(jarvis, user="alice", content=MARATHON) == [alices]
