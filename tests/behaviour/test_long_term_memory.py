"""Long-term memory: what Jarvis remembers across conversations, and what it lets go.

A fact mentioned in one conversation is in the model's context in the next. A
contradiction replaces the old fact; "forget" removes it; dismissing a learned
pattern hides it and confirming it raises its confidence. Memories fade with
time since they were last reinforced (SM-2 style decay) and drop out of context
below the retention floor. Nobody else's memories are ever in your context.

Memories come back from the turn's one extraction call (``brain_dump_extraction``),
scripted to react only to what the user said in this message, the way a real
extractor would. Handles it cites for contradictions are read off the known
memories (``[m1] (type) content``) listed in that prompt.
"""

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest

import app.domain.clock as utc_clock
from tests.behaviour.conftest import brain_dump, said

MARATHON = "Training for the Mumbai marathon in March"
MORNINGS = "Prefers studying in the mornings"
NIGHTS = "Now studies best late at night"
_T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _script_memories(llm, rules: dict) -> None:
    """When what the user said contains a trigger, the extraction returns that rule's memory.

    A rule's memory is a dict, or a callable taking the extraction prompt (so it
    can cite the handle of a known memory it contradicts).
    """

    def reply(req) -> str:
        message = said(req)
        memories = [
            memory(req.prompt) if callable(memory) else memory
            for trigger, memory in rules.items() if trigger in message
        ]
        return brain_dump(memories=memories)

    llm.script("brain_dump_extraction", reply)


def _contradicting_mornings(prompt: str) -> dict:
    match = re.search(rf"^\[(m\d+)\] \(preference\) {MORNINGS}$", prompt, flags=re.MULTILINE)
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


def _context_of_reply(llm, since: int) -> str:
    """What the reply was told about the user: its context note, the last history entry
    (the system prompt is the same constant for everyone)."""
    (reply_call,) = [c for c in llm.calls[since:] if c.task == "voice_of_jarvis"]
    notes = [m["content"] for m in reply_call.conversation_history if m["role"] == "system"]
    return "\n".join(notes)


async def _in_use(jarvis, user="alice", now=None) -> list[str]:
    """The words of the memories Jarvis has in use for ``user`` at ``now``: what recall,
    the extraction's known memories and planning read from (``MemoryService.list_active``)."""
    memories = await jarvis.state.container.memory_service.list_active(user, now=now)
    return [m.content for m in memories]


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
    clock.freeze(_T0, utc_clock)
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
    assert MARATHON in _context_of_reply(llm, calls_before)
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
    context = _context_of_reply(llm, calls_before)
    assert NIGHTS in context
    assert MORNINGS not in context
    assert MORNINGS not in await _in_use(jarvis)


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
    assert MARATHON not in _context_of_reply(llm, calls_before)


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
    in_use = await _in_use(jarvis)
    assert dismissed.content not in in_use
    assert confirmed.content in in_use


# --- fading -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory__unused_for_a_week__still_in_context(jarvis, llm, frozen_memory_clock):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March")

    frozen_memory_clock.advance(days=7)

    assert MARATHON in await _in_use(jarvis, now=frozen_memory_clock.now)


@pytest.mark.asyncio
async def test_memory__unused_for_seven_months__fades_below_the_floor_and_leaves_context(
    jarvis, llm, frozen_memory_clock
):
    _script_memories(llm, _RULES)
    await jarvis.say("I'm training for the Mumbai marathon in March")

    # A said goal loses a factor of e every 12 weeks: under the floor after ~28 unused.
    frozen_memory_clock.advance(days=200)
    calls_before = len(llm.calls)
    turn = await jarvis.say("What should I focus on this week?")

    assert MARATHON not in _context_of_reply(llm, calls_before)
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

    assert MARATHON in await _in_use(jarvis, now=frozen_memory_clock.now)


@pytest.mark.asyncio
async def test_memory__confirmed_pattern__outlasts_an_unconfirmed_one(
    jarvis, llm, monkeypatch, frozen_memory_clock
):
    from app.domain.entities import MemorySource
    from app.services.memory.memory_service import MemoryCandidate

    for content in ("Skips work around 9 AM", "Most productive after lunch"):
        await jarvis.state.container.memory_service.remember(
            "alice",
            MemoryCandidate(memory_type="behavioral_pattern", content=content, confidence=0.8),
            provenance=MemorySource.INFERRED,
            origin="behavior",
        )
    unconfirmed, confirmed = _memories(jarvis)
    async with _memory_api(jarvis, monkeypatch) as api:
        await api.post(f"/api/v1/memories/{confirmed['id']}/confirm", params={"user_id": "alice"})

    frozen_memory_clock.advance(days=30)

    in_use = await _in_use(jarvis, now=frozen_memory_clock.now)
    assert confirmed["content"] in in_use
    assert unconfirmed["content"] not in in_use


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

    assert MARATHON not in _context_of_reply(llm, calls_before)
    # Bob's own extraction is not shown Alice's memories as known ones either.
    for call in llm.calls[calls_before:]:
        if call.task == "brain_dump_extraction":
            assert MARATHON not in call.prompt
    async with _memory_api(jarvis, monkeypatch) as api:
        panel = (await api.get("/api/v1/memories/", params={"user_id": "bob"})).json()
        forget = await api.delete(f"/api/v1/memories/{alices['id']}", params={"user_id": "bob"})
        confirm = await api.post(f"/api/v1/memories/{alices['id']}/confirm", params={"user_id": "bob"})
    assert panel["memories"] == []
    assert (forget.status_code, confirm.status_code) == (404, 404)
    assert _memories(jarvis, user="alice", content=MARATHON) == [alices]
