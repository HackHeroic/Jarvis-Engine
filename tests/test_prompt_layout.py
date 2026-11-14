"""A cache-stable prompt: constant system prompts, all volatile context in one note.

The reply's system prompt is the same bytes for every user, at every hour, whatever
Jarvis remembers: Claude caches it, and a prompt that changed per turn would never be
read back from the cache. Everything that does change — memories, the plate, the
user's documents, the time — rides in one ``system`` note after the history, and every
provider folds that note into the final user turn. So two turns share the system block
and every earlier message byte for byte, and differ only from the note on.

These drive the real ``/chat/v2/stream`` handler through the behaviour harness
(``tests/behaviour/conftest.py``) and read the ``LLMRequest`` the scripted model got.
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.core.config as config
import app.services.task_service as task_service
from app.domain.entities import Memory, MemorySource
from app.infrastructure.llm.claude import ClaudeProvider
from app.modules.conversation import JARVIS_CHAT_PROMPT
from app.services.analytical.voice_of_jarvis import (
    VOICE_OF_JARVIS_PROMPT,
    synthesize_jarvis_response,
    synthesize_jarvis_response_stream,
)
from app.services.memory.context import render_memory_line
from app.services.memory.memory_service import MemoryCandidate
from tests.behaviour.conftest import (  # noqa: F401 - embeddings, jarvis and llm are fixtures
    embeddings,
    jarvis,
    llm,
)

IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
MORNINGS = "Prefers studying in the mornings"
MARATHON = "Training for the Mumbai marathon in March"
CONTEXT_TAGS = ("<memory>", "<since>", "<plate>", "<documents>", "<now>")


@pytest.fixture
def frozen(clock):
    """Memory writes, recall and the note's clock read one frozen time: Monday 08:00 IST."""
    clock.freeze(MONDAY_8AM, schedule_endpoint, task_service)
    return clock


async def _remember(jarvis, content: str, memory_type: str = "preference", user: str = "alice"):
    outcome = await jarvis.state.container.memory_service.remember(
        user, MemoryCandidate(memory_type=memory_type, content=content, confidence=0.8)
    )
    return outcome.memory


def _reply_calls(llm, since: int = 0) -> list:
    return [c for c in llm.calls[since:] if c.task == "voice_of_jarvis"]


def _note(call) -> str:
    """The context note: the last history entry, a system message."""
    last = call.conversation_history[-1]
    assert last["role"] == "system"
    return last["content"]


# --- the chat reply ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_system_prompt__identical_for_two_users_and_two_times(jarvis, llm, frozen):
    await _remember(jarvis, MORNINGS, user="alice")
    await jarvis.say("What should I focus on this week?", user="alice")
    frozen.advance(hours=9, minutes=35)
    await _remember(jarvis, MARATHON, "goal", user="bob")
    await jarvis.say("hi", user="bob")

    alice, bob = _reply_calls(llm)

    assert alice.system_prompt == bob.system_prompt == JARVIS_CHAT_PROMPT
    assert MORNINGS in _note(alice) and MARATHON not in _note(alice)
    assert MARATHON in _note(bob) and MORNINGS not in _note(bob)
    assert _note(alice).endswith("<now>Mon 16 Nov 2026, 08:00 (Asia/Kolkata)</now>")
    assert _note(bob).endswith("<now>Mon 16 Nov 2026, 17:35 (Asia/Kolkata)</now>")


def test_chat_system_prompt__at_least_2400_chars():
    assert len(JARVIS_CHAT_PROMPT) >= 2400
    for tag in CONTEXT_TAGS:
        assert tag in JARVIS_CHAT_PROMPT, tag
    assert "you said" in JARVIS_CHAT_PROMPT and "I noticed" in JARVIS_CHAT_PROMPT


@pytest.mark.asyncio
async def test_reply_request__memory_note_is_last_history_entry(jarvis, llm, frozen):
    stored = await _remember(jarvis, MORNINGS)
    first = await jarvis.say("I'm revising graph algorithms today")
    since = len(llm.calls)

    second = await jarvis.say("What should I focus on this week?", session=first.conversation_id)

    (reply,) = _reply_calls(llm, since)
    assert reply.system_prompt == JARVIS_CHAT_PROMPT
    assert reply.prompt == "What should I focus on this week?"
    assert reply.conversation_history == [
        {"role": "user", "content": "I'm revising graph algorithms today"},
        {"role": "assistant", "content": "Very good, sir."},
        {
            "role": "system",
            "content": (
                "<memory>\n"
                "About you:\n"
                f"- (you said, today) {MORNINGS}\n"
                "</memory>\n"
                "<now>Mon 16 Nov 2026, 08:00 (Asia/Kolkata)</now>"
            ),
        },
    ]
    # The memory panel shows exactly what the model was given.
    assert second.payload["memories"] == [
        {
            "memory_id": stored.id,
            "memory_type": "preference",
            "content": MORNINGS,
            "confidence": 0.8,
            "provenance": "said",
        }
    ]


@pytest.mark.asyncio
async def test_two_turns__system_block_and_message_prefix_identical_up_to_the_note(
    jarvis, llm, frozen, monkeypatch
):
    await _remember(jarvis, MORNINGS)
    first = await jarvis.say("I'm revising graph algorithms today")
    session = first.conversation_id
    since = len(llm.calls)
    await jarvis.say("What should I focus on this week?", session=session)
    # Between the turns: a new memory, a task on the plate, a document match, three hours.
    await _remember(jarvis, MARATHON, "goal")
    jarvis.db.table("user_tasks").insert({
        "id": "row-1", "user_id": "alice", "task_id": "run_1", "goal_id": "marathon",
        "title": "Long run, 20 km", "status": "pending", "duration_minutes": 25,
        "deadline_hint": "Sunday",
    }).execute()
    monkeypatch.setattr(
        "app.utils.chroma_client.query_knowledge",
        lambda user_id, query, n: ["Marathon plan: week 12 has a 30 km long run"],
    )
    frozen.advance(hours=3)
    await jarvis.say("And what about the marathon?", session=session)

    earlier, later = _reply_calls(llm, since)

    # The request: one constant system prompt, the history, then one note.
    assert earlier.system_prompt == later.system_prompt == JARVIS_CHAT_PROMPT
    *earlier_turns, _ = earlier.conversation_history
    *later_turns, _ = later.conversation_history
    assert later_turns[: len(earlier_turns)] == earlier_turns
    assert len(later_turns) == len(earlier_turns) + 2
    assert all(m["role"] != "system" for m in later_turns)  # no note is ever carried forward
    assert _note(later) == (
        "<memory>\n"
        "About you:\n"
        f"- (you said, today) {MORNINGS}\n"
        f"- (you said, today) {MARATHON}\n"
        "</memory>\n"
        "<plate>\n"
        "- Long run, 20 km (due Sunday)\n"
        "</plate>\n"
        "<documents>\n"
        "Marathon plan: week 12 has a 30 km long run\n"
        "</documents>\n"
        "<now>Mon 16 Nov 2026, 11:00 (Asia/Kolkata)</now>"
    )

    # The wire, as Claude gets it: a byte-identical cached system block, and every
    # message before the final user turn (which opens with the note) unchanged.
    claude = ClaudeProvider(config)
    before, after = claude._params(earlier), claude._params(later)
    assert json.dumps(before["system"]) == json.dumps(after["system"])
    assert before["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert after["messages"][: len(before["messages"]) - 1] == before["messages"][:-1]
    assert before["messages"][-1]["content"] == (
        f"{_note(earlier)}\n\nWhat should I focus on this week?"
    )
    assert after["messages"][-1]["content"] == f"{_note(later)}\n\nAnd what about the marathon?"


# --- the synthesis reply ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_voj_system_prompt__identical_and_at_least_2400_chars(llm):
    history = [{"role": "user", "content": "plan my thursday"}]
    morning = "<memory>\nAbout you:\n- (you said, today) Prefers mornings\n</memory>"
    evening = "<now>Mon 16 Nov 2026, 21:10 (Asia/Kolkata)</now>"

    await synthesize_jarvis_response(
        {"schedule_generated": True, "memory_context": morning}, conversation_history=history
    )
    _ = [
        event
        async for event in synthesize_jarvis_response_stream(
            {"habits_saved": "gym at 7", "memory_context": evening}
        )
    ]

    plain, streamed = llm.calls_for("voice_of_jarvis_synthesis")
    assert plain.system_prompt == streamed.system_prompt == VOICE_OF_JARVIS_PROMPT
    assert len(VOICE_OF_JARVIS_PROMPT) >= 2400
    for tag in CONTEXT_TAGS:
        assert tag in VOICE_OF_JARVIS_PROMPT, tag
    assert plain.conversation_history == [*history, {"role": "system", "content": morning}]
    assert streamed.conversation_history == [{"role": "system", "content": evening}]


# --- one memory line ----------------------------------------------------------------------


def test_render_memory_line__said_vs_noticed_and_age():
    now = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)

    def memory(content: str, provenance: MemorySource, **fields) -> Memory:
        fields.setdefault("created_at", now)
        return Memory(user_id="alice", memory_type="preference", content=content,
                      provenance=provenance, strength_as_of=now, **fields)

    said = MemorySource.SAID
    noticed = MemorySource.INFERRED
    assert render_memory_line(
        memory("Prefers  studying\nin the mornings", said, created_at=now - timedelta(days=12)), now
    ) == "- (you said, 12 days ago) Prefers studying in the mornings"
    assert render_memory_line(
        memory("Skips work around 9 AM", noticed, observation_count=4,
               created_at=now - timedelta(days=20), last_confirmed_at=now - timedelta(days=3)),
        now,
    ) == "- (I noticed, 3 days ago; seen 4 times) Skips work around 9 AM"
    assert render_memory_line(memory("Likes tea", noticed, created_at=now - timedelta(hours=2)),
                              now) == "- (I noticed, today) Likes tea"
    assert render_memory_line(memory("Likes tea", said, created_at=now - timedelta(days=1)),
                              now) == "- (you said, yesterday) Likes tea"
    assert render_memory_line(memory("Likes tea", said, created_at=now - timedelta(days=21)),
                              now) == "- (you said, 3 weeks ago) Likes tea"
    assert render_memory_line(memory("Likes tea", said, created_at=now - timedelta(days=95)),
                              now) == "- (you said, 3 months ago) Likes tea"
