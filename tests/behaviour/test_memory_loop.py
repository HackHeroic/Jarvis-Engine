"""The memory loop, end to end: said, found, corrected, kept, forgotten, private.

What the user tells Jarvis is in the context note of a later conversation when it bears
on what they ask; each part of a two-part question finds its own memory; a correction
leaves one rule, the new one, and the old memory out of the note; a stated preference
is still there a month later; a forgotten fact is gone and is not stored again when it
is said again; and another user sees none of it.

The real chat turn over the fake database and a scripted model. Each test asserts on
stored state (``GET /api/v1/you``, the rows) and on the context note the model was given
(``ScriptedLLM`` records every request), never on the wording of a reply.
"""

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest

import app.domain.clock as utc_clock
from tests.behaviour.conftest import brain_dump, said

RAO = "Dr. Rao gives surprise quizzes on Thursdays"
MESS = "Hostel mess closes at 9 PM"
GYM_6PM = "Goes to the gym every weekday at 6 PM"
GYM_6PM_SAID = "I go to the gym every weekday at 6 PM"
GYM_7AM = "Goes to the gym at 7 AM on weekdays"
GYM_7AM_SAID = "Actually I moved my gym to 7 AM on weekdays, not 6 PM anymore."
EVENINGS = "Thinks most clearly in the evenings, after about 7 PM"
TWO_FACTS = "My Networks professor Dr. Rao gives surprise quizzes on Thursdays, and the hostel mess closes at 9 PM"
TWO_PART_QUESTION = "When does the hostel mess close, and are there quizzes on Thursdays?"
_T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _handle_of(prompt: str, content: str) -> str | None:
    """The handle (``m3``) the extraction prompt lists a known memory under."""
    match = re.search(rf"^\[(m\d+)\] \(\w+\) {re.escape(content)}$", prompt, flags=re.MULTILINE)
    return match.group(1) if match else None


def _extract(req) -> str:
    """The turn's one extraction, reacting only to what the user said in this message,
    the way a real extractor would."""
    message = said(req)
    if message == TWO_FACTS:
        return brain_dump(memories=[
            {"type": "fact", "content": RAO, "confidence": 0.9},
            {"type": "fact", "content": MESS, "confidence": 0.9},
        ])
    if message == GYM_6PM_SAID:
        return brain_dump(
            inline_habits=[GYM_6PM_SAID],
            memories=[{"type": "constraint", "content": GYM_6PM, "confidence": 0.95}],
        )
    if message == GYM_7AM_SAID:
        return brain_dump(
            inline_habits=[GYM_7AM_SAID],
            memories=[{
                "type": "constraint", "content": GYM_7AM, "confidence": 0.95,
                "contradicts": _handle_of(req.prompt, GYM_6PM),
            }],
        )
    if "clearly in the evenings" in message:
        return brain_dump(memories=[{"type": "preference", "content": EVENINGS, "confidence": 0.9}])
    return "{}"


@pytest.fixture
def scripted(llm):
    llm.script("brain_dump_extraction", _extract)
    return llm


def _note_of_reply(llm, since: int) -> str:
    """The context note the reply was written with: the system entries of its history
    (the system prompt is the same constant for everyone)."""
    (reply_call,) = [c for c in llm.calls[since:] if c.task == "voice_of_jarvis"]
    return "\n".join(
        m["content"] for m in reply_call.conversation_history if m["role"] == "system"
    )


async def _ask(jarvis, llm, text: str, *, user: str = "alice") -> str:
    """Ask in a new conversation; return the context note the reply was given."""
    since = len(llm.calls)
    await jarvis.say(text, user=user)
    return _note_of_reply(llm, since)


@asynccontextmanager
async def _api(jarvis, monkeypatch):
    """The REST API (the You page, the Memory panel) over this test's composition root."""
    from app.main import app

    monkeypatch.setattr(app.state, "container", jarvis.state.container, raising=False)
    monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _you(jarvis, monkeypatch, user: str = "alice") -> dict:
    async with _api(jarvis, monkeypatch) as api:
        response = await api.get("/api/v1/you", params={"user_id": user})
    assert response.status_code == 200, response.text
    return response.json()


def _active(jarvis, user: str = "alice") -> list[str]:
    """The words of the user's active memories, superseded ones left out."""
    return [
        r["content"] for r in jarvis.rows("user_memories", user_id=user, status="active")
        if not r.get("superseded_by")
    ]


# --- said in one chat, found in the next ----------------------------------------------


@pytest.mark.asyncio
async def test_loop__said_in_one_chat__in_the_context_note_of_a_new_chat(
    jarvis, scripted, monkeypatch
):
    await jarvis.say(TWO_FACTS)

    note = await _ask(jarvis, scripted, "Tell me about Dr. Rao and his quizzes")

    assert RAO in note
    memories = [m["content"] for m in (await _you(jarvis, monkeypatch))["memories"]]
    assert RAO in memories and MESS in memories


@pytest.mark.xfail(strict=True, reason="M1")
@pytest.mark.asyncio
async def test_loop__two_part_question__both_memories_in_the_note(jarvis, scripted):
    await jarvis.say(TWO_FACTS)

    note = await _ask(jarvis, scripted, TWO_PART_QUESTION)

    assert RAO in note
    assert MESS in note


# --- corrected -----------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="M2")
@pytest.mark.asyncio
async def test_loop__correction__one_rule_and_old_memory_not_in_the_note(
    jarvis, scripted, monkeypatch
):
    await jarvis.say(GYM_6PM_SAID)
    await jarvis.say(GYM_7AM_SAID)

    note = await _ask(jarvis, scripted, "What does my week look like around the gym?")

    rules = [r["text"] for r in (await _you(jarvis, monkeypatch))["rules"]]
    assert len([text for text in rules if "gym" in text.lower()]) == 1, rules
    assert GYM_7AM in note
    assert GYM_6PM not in note
    assert GYM_6PM not in _active(jarvis)


# --- kept ----------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="M3")
@pytest.mark.asyncio
async def test_loop__thirty_days_later__stated_preference_still_in_the_note(
    jarvis, scripted, clock
):
    clock.freeze(_T0, utc_clock)
    await jarvis.say("I think most clearly in the evenings, after about 7 PM.")

    clock.advance(days=30)
    note = await _ask(jarvis, scripted, "What do you know about how I work?")

    assert EVENINGS in note


# --- forgotten -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop__forgotten__not_in_the_note_and_not_stored_again_when_repeated(
    jarvis, scripted, monkeypatch
):
    await jarvis.say(TWO_FACTS)
    (rao,) = jarvis.rows("user_memories", user_id="alice", content=RAO)
    async with _api(jarvis, monkeypatch) as api:
        forgot = await api.delete(f"/api/v1/memories/{rao['id']}", params={"user_id": "alice"})
    assert forgot.status_code == 200

    repeated = await jarvis.say(TWO_FACTS)
    note = await _ask(jarvis, scripted, "Tell me about Dr. Rao and his quizzes")

    learned = [data["content"] for event, data in repeated.events if event == "memory_extracted"]
    assert RAO not in learned
    assert RAO not in _active(jarvis)
    assert jarvis.rows("user_memories", user_id="alice", content=RAO) == []
    assert RAO not in note
    memories = [m["content"] for m in (await _you(jarvis, monkeypatch))["memories"]]
    assert RAO not in memories
    assert MESS in memories  # the other fact of the same message stays


# --- private -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop__another_user__sees_none_of_it(jarvis, scripted, monkeypatch):
    await jarvis.say(TWO_FACTS)
    await jarvis.say(GYM_6PM_SAID)

    note = await _ask(jarvis, scripted, TWO_PART_QUESTION, user="bob")

    for words in (RAO, MESS, GYM_6PM):
        assert words not in note
    bob = await _you(jarvis, monkeypatch, user="bob")
    assert bob["memories"] == [] and bob["rules"] == []
    alice = await _you(jarvis, monkeypatch)
    assert alice["rules"] and alice["memories"]  # alice's are there, for alice only
