"""Short-term (working) memory: what Jarvis keeps in mind within one conversation.

Within a conversation the model sees the most recent messages, newest last, and is
told when older ones were left out. A new conversation starts clean: no history,
no draft under review. Uploaded files never reach the checkpoint file — that is
pinned by ``tests/test_checkpointer.py::test_checkpointer__uploaded_document_is_never_persisted``
and not repeated here.
"""

import re
from datetime import datetime, timedelta, timezone

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
    assert reply_call.conversation_history == [
        {"role": "user", "content": "I'm revising graph algorithms today"},
        {"role": "assistant", "content": "Noted, sir: graphs today."},
    ]


@pytest.mark.asyncio
async def test_chat__new_conversation__starts_with_no_history_and_no_draft_under_review(
    jarvis, llm, clock
):
    clock.freeze(datetime(2026, 11, 16, 7, 30, tzinfo=timezone.utc), schedule_endpoint)
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
    assert all(not c.conversation_history for c in new_calls)
    # "looks good" here is small talk: the other conversation's draft is untouched.
    assert jarvis.draft(planned.draft_id)["status"] == "pending"
    assert jarvis.rows("user_tasks") == []
