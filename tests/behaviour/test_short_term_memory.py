"""Short-term (working) memory: what Jarvis keeps in mind within one conversation.

Within a conversation the model sees the most recent messages, newest last, and is
told when older ones were left out. A new conversation starts clean: no history,
no draft under review. Uploaded files never reach the checkpoint file — that is
pinned by ``tests/test_checkpointer.py::test_checkpointer__uploaded_document_is_never_persisted``
and not repeated here.
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
from app.services.chat_history import build_context_messages
from tests.behaviour.conftest import brain_dump, decomposition
from tests.fakes import FakeSupabase

_T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _seed_conversation(supabase: FakeSupabase, session: str, user: str, count: int) -> None:
    """``count`` alternating user/assistant messages, one minute apart, oldest first."""
    for i in range(1, count + 1):
        supabase.table("chat_messages").insert({
            "id": f"{session}-m{i}",
            "session_id": session,
            "user_id": user,
            "role": "user" if i % 2 else "assistant",
            "content": f"message {i}",
            "created_at": (_T0 + timedelta(minutes=i)).isoformat(),
        }).execute()


@pytest.mark.asyncio
async def test_context__fifteen_messages__newest_ten_in_order_after_a_placeholder():
    supabase = FakeSupabase()
    _seed_conversation(supabase, "s1", "alice", 15)

    context = await build_context_messages("s1", "alice", max_messages=10, supabase=supabase)

    placeholder, window = context[0], context[1:]
    assert [m["content"] for m in window] == [f"message {i}" for i in range(6, 16)]
    assert [m["role"] for m in window] == ["assistant", "user"] * 5
    # The five older messages are announced, not silently dropped.
    assert placeholder["role"] == "system"
    assert "earlier message" in placeholder["content"]
    # And never misreported: if the note gives a number, it is the true one.
    assert all(int(n) == 5 for n in re.findall(r"\d+", placeholder["content"]))


@pytest.mark.asyncio
async def test_context__exactly_ten_messages__all_ten_in_order_and_no_placeholder():
    supabase = FakeSupabase()
    _seed_conversation(supabase, "s1", "alice", 10)

    context = await build_context_messages("s1", "alice", max_messages=10, supabase=supabase)

    assert [m["content"] for m in context] == [f"message {i}" for i in range(1, 11)]
    assert all(m["role"] != "system" for m in context)


@pytest.mark.asyncio
async def test_context__another_users_conversation_id__returns_nothing():
    supabase = FakeSupabase()
    _seed_conversation(supabase, "alice-session", "alice", 6)

    assert await build_context_messages("alice-session", "bob", supabase=supabase) == []


@pytest.mark.asyncio
async def test_chat__second_turn__model_sees_the_first_exchange_but_not_the_new_message_twice(
    jarvis, llm
):
    llm.script("voice_of_jarvis", "Noted, sir: graphs today.")
    first = await jarvis.say("I'm revising graph algorithms today")
    llm.script("voice_of_jarvis", "Graph algorithms, sir.")
    calls_before = len(llm.calls)

    await jarvis.say("what was I revising?", session=first.conversation_id)

    (reply_call,) = [c for c in llm.calls[calls_before:] if c.task == "voice_of_jarvis"]
    assert reply_call.prompt == "what was I revising?"
    *turns, note = reply_call.conversation_history  # the turn's context note comes last
    assert turns == [
        {"role": "user", "content": "I'm revising graph algorithms today"},
        {"role": "assistant", "content": "Noted, sir: graphs today."},
    ]
    assert note["role"] == "system" and "I'm revising" not in note["content"]


@pytest.mark.asyncio
async def test_chat__new_conversation__starts_with_no_history_and_no_draft_under_review(
    jarvis, llm, clock
):
    # 08:00 in India, the start of the user's day (was 07:30 UTC, before the
    # horizon was the user's local day).
    clock.freeze(datetime(2026, 11, 16, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")), schedule_endpoint)
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA") if "DSA" in req.prompt else "{}",
    )
    llm.script("socratic_chunker", decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    planned = await jarvis.say("Plan my DSA revision for Friday")
    assert planned.draft_id
    calls_before = len(llm.calls)

    fresh = await jarvis.say("looks good")  # a brand-new conversation

    assert fresh.conversation_id != planned.conversation_id
    new_calls = llm.calls[calls_before:]
    assert new_calls, "the new conversation was answered by the model"
    # No earlier turns: at most the turn's context note.
    assert all(
        m["role"] == "system" for c in new_calls for m in (c.conversation_history or [])
    )
    # "looks good" here is small talk: the other conversation's draft is untouched.
    assert jarvis.draft(planned.draft_id)["status"] == "pending"
    assert jarvis.rows("user_tasks") == []


@pytest.mark.asyncio
async def test_short_term__new_session__model_told_since_we_last_spoke(jarvis, llm, clock):
    import app.services.chat_history as chat_history
    import app.services.task_service as task_service

    ist = ZoneInfo("Asia/Kolkata")
    clock.freeze(datetime(2026, 11, 15, 20, 0, tzinfo=ist), chat_history, task_service)
    first = await jarvis.say("I'm revising graph algorithms this week")
    assert first.payload["since_last_spoke"] is None  # a first conversation: nothing to pick up
    clock.advance(hours=13)  # 09:00 the next morning
    calls_before = len(llm.calls)

    fresh = await jarvis.say("good morning")  # a new conversation

    since = (
        'Since we last spoke (yesterday): you were talking about '
        '"I\'m revising graph algorithms this week".'
    )
    (reply,) = [c for c in llm.calls[calls_before:] if c.task == "voice_of_jarvis"]
    note = reply.conversation_history[-1]
    assert note["role"] == "system"
    assert f"<since>{since}</since>" in note["content"]
    assert reply.system_prompt.count("<since>") == 1  # the prompt explains the tag, nothing more
    assert since not in reply.system_prompt
    assert fresh.payload["since_last_spoke"] == since  # what the model was told
    assert not any(c.task == "session_summary" for c in llm.calls)  # no model call for it

    calls_before = len(llm.calls)
    second = await jarvis.say("what should I start with?", session=fresh.conversation_id)

    (reply,) = [c for c in llm.calls[calls_before:] if c.task == "voice_of_jarvis"]
    assert "<since>" not in reply.conversation_history[-1]["content"]
    assert second.payload["since_last_spoke"] is None


@pytest.mark.asyncio
async def test_short_term__window_unreadable__plain_newest_ten_and_the_turn_goes_on(
    jarvis, llm, monkeypatch
):
    first = await jarvis.say("I'm revising graph algorithms today")
    calls_before = len(llm.calls)

    async def unreadable(*args, **kwargs):  # e.g. the summary columns not migrated yet
        from app.domain.errors import PersistenceError

        raise PersistenceError("Could not reach the database.")

    monkeypatch.setattr(jarvis.state.container.conversation_memory, "build_history", unreadable)
    second = await jarvis.say("what was I revising?", session=first.conversation_id)

    (reply,) = [c for c in llm.calls[calls_before:] if c.task == "voice_of_jarvis"]
    *turns, note = reply.conversation_history
    assert turns == [
        {"role": "user", "content": "I'm revising graph algorithms today"},
        {"role": "assistant", "content": "Very good, sir."},
    ]
    assert "<earlier>" not in note["content"] and "<since>" not in note["content"]
    assert second.reply == "Very good, sir."
