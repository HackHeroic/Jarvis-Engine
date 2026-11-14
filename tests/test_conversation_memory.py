"""Conversations that remember: the history window, rolling compaction, idle summaries
and "since we last spoke" (``ConversationMemoryService``, ``SessionSummarySweep``).

The conversation lives in the in-memory repository family, seeded straight into its
store (the app writes sessions and messages through ``chat_history``; the repository
only reads them and writes the summary columns). The one language model is a
``ScriptedLLM``: an unscripted call fails the test, so "no model call" has teeth.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.core.config as config
from app.domain.entities import ChatMessage, ConversationSession, TaskEvent, TaskEventType
from app.domain.errors import EmbeddingUnavailableError, LLMUnavailableError
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryStore
from app.schemas.memory import SessionSummary
from app.services.memory.context import Recall, render_context_note
from app.services.memory.conversation_memory import (
    ConversationMemoryService,
    SessionSummarySweep,
)
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from tests.behaviour.conftest import ScriptedLLM, blame_phrases_in
from tests.fakes import BagOfWordsEmbedder

U, BOB = "alice", "bob"
IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 11, 16, 3, 30, tzinfo=timezone.utc)  # 09:00 IST, Monday
S1, S2, S3, S4, S5 = (str(uuid.UUID(int=n)) for n in range(1, 6))


# --- fixtures and builders ---------------------------------------------------------


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def repos(store):
    return RepositoryFactory.in_memory(store)


@pytest.fixture
def clock() -> _Clock:
    return _Clock(T0 + timedelta(hours=2))


@pytest.fixture
def llm():
    """The only model the test can reach; any call it was not scripted for fails."""
    scripted = ScriptedLLM()
    set_chain(FallbackChainProvider([scripted]))  # conftest's _fresh_llm_chain resets it
    yield scripted
    assert not scripted.unscripted, f"unscripted LLM calls: {scripted.unscripted}"


@pytest.fixture
def service(repos, clock) -> ConversationMemoryService:
    return ConversationMemoryService(repos.conversations, repos.task_events, clock=clock)


def _session(
    store: InMemoryStore,
    session_id: str,
    *,
    user: str = U,
    title: str | None = None,
    updated_at: datetime = T0,
    summary: str | None = None,
    through: datetime | None = None,
) -> None:
    store.chat_sessions.append(
        ConversationSession(
            id=session_id,
            user_id=user,
            title=title,
            created_at=T0 - timedelta(days=30),
            updated_at=updated_at,
            summary=summary,
            summary_through=through,
            summary_updated_at=through,
        )
    )


def _messages(
    store: InMemoryStore,
    session_id: str,
    count: int,
    *,
    user: str = U,
    start: datetime = T0,
    first: int = 1,
) -> list[ChatMessage]:
    """``count`` alternating user/assistant messages, a minute apart, "message N"."""
    added = []
    for n in range(first, first + count):
        message = ChatMessage(
            id=str(uuid.uuid4()),
            session_id=session_id,
            user_id=user,
            role="user" if n % 2 else "assistant",
            content=f"message {n}",
            created_at=start + timedelta(minutes=n),
        )
        store.chat_messages.append(message)
        added.append(message)
    return added


def _turns(*numbers: int) -> list[dict]:
    return [
        {"role": "user" if n % 2 else "assistant", "content": f"message {n}"} for n in numbers
    ]


def _stored(store: InMemoryStore, session_id: str) -> ConversationSession:
    return next(s for s in store.chat_sessions if s.id == session_id)


def _summary(text: str = "You planned graphs.", threads: list[str] | None = None):
    return SessionSummary(summary=text, open_threads=threads or [])


def _fails(_req):
    raise LLMUnavailableError("the model is down")


# --- build_history -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_history__short_session__all_messages_no_summary(store, service):
    _session(store, S1)
    _messages(store, S1, 6)

    window = await service.build_history(U, S1)

    assert window.history == _turns(1, 2, 3, 4, 5, 6)
    assert window.summarized is False
    assert window.message_count == 6
    assert window.earlier is None


@pytest.mark.asyncio
async def test_build_history__summary__leading_note_then_messages_after_through(store, service):
    _session(store, S1)
    sent = _messages(store, S1, 20)
    store.chat_sessions[0] = store.chat_sessions[0].model_copy(
        update={
            "summary": "You were planning DSA revision.",
            "summary_through": sent[11].created_at,
        }
    )

    window = await service.build_history(U, S1)

    assert window.earlier == "Earlier in this conversation: You were planning DSA revision."
    assert window.history == _turns(*range(13, 21))
    assert window.summarized is True
    assert window.message_count == 8
    # The leading note rides in the turn's context note, through the data guard.
    note = render_context_note(
        Recall(profile=[], relevant=[], tokens=0), now=T0, zone=IST, earlier=window.earlier
    )
    assert (
        "<earlier>Earlier in this conversation: You were planning DSA revision.</earlier>" in note
    )


@pytest.mark.asyncio
async def test_build_history__long_message__cut_to_1500_chars(store, service):
    _session(store, S1)
    store.chat_messages.append(
        ChatMessage(
            id=str(uuid.uuid4()), session_id=S1, user_id=U, role="user",
            content="x" * 4000 + "END", created_at=T0,
        )
    )

    (turn,) = (await service.build_history(U, S1)).history

    assert len(turn["content"]) == config.HISTORY_MAX_CHARS_PER_MESSAGE == 1500
    assert turn["content"] == "x" * 1499 + "…"


@pytest.mark.asyncio
async def test_build_history__more_unsummarized_than_fit__newest_24_after_a_not_shown_note(
    store, service
):
    _session(store, S1)
    _messages(store, S1, 30)

    window = await service.build_history(U, S1)

    assert window.history == _turns(*range(7, 31))
    assert window.earlier == (
        "Earlier messages in this conversation are not shown here; "
        "the ones that follow are the most recent."
    )
    assert window.summarized is False


@pytest.mark.asyncio
async def test_build_history__summary_over_its_budget__whole_sentences_within_it(
    store, service, monkeypatch
):
    from app.services.memory.context import estimate_tokens

    monkeypatch.setattr(config, "CONVERSATION_SUMMARY_TOKENS", 30)
    long = "You described the whole history of graph theory since Euler in great detail."
    _session(store, S1, summary=f"You chose graphs. {long} We set Friday.\n- Trees?", through=T0)
    _messages(store, S1, 2)

    first = await service.build_history(U, S1)
    second = await service.build_history(U, S1)

    # The long sentence does not fit and is left out whole; the rest keep their order.
    assert first.earlier == (
        "Earlier in this conversation: You chose graphs. We set Friday. Still open: Trees?"
    )
    assert estimate_tokens(first.earlier) <= 30
    assert second.earlier == first.earlier  # the same bytes every turn


@pytest.mark.asyncio
async def test_build_history__another_users_session__empty(store, service):
    _session(store, S1, user=BOB)
    _messages(store, S1, 4, user=BOB)

    window = await service.build_history(U, S1)

    assert window.history == [] and window.earlier is None and window.message_count == 0


# --- compaction --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact__17_unsummarized__one_fast_call_keeps_newest_8(store, service, llm):
    _session(store, S1)
    sent = _messages(store, S1, 17)
    llm.script("session_summary", _summary("You planned graphs.", ["Pick a time for trees."]))

    assert await service.compact_if_needed(U, S1) is True

    (call,) = llm.calls
    assert (call.task, call.role) == ("session_summary", "fast")
    assert (call.user_id, call.session_id) == (U, S1)  # metered to the user
    assert "message 9" in call.prompt and "message 10" not in call.prompt
    session = _stored(store, S1)
    assert session.summary == "You planned graphs.\n- Pick a time for trees"
    assert session.summary_through == sent[8].created_at
    window = await service.build_history(U, S1)
    assert window.history == _turns(*range(10, 18))
    assert window.earlier == (
        "Earlier in this conversation: You planned graphs. Still open: Pick a time for trees."
    )


@pytest.mark.asyncio
async def test_compact__16__no_call(store, service, llm):
    _session(store, S1)
    _messages(store, S1, 16)

    assert await service.compact_if_needed(U, S1) is False

    assert llm.calls == []
    assert _stored(store, S1).summary is None


@pytest.mark.asyncio
async def test_compact__llm_fails__nothing_changes(store, service, llm):
    _session(store, S1)
    _messages(store, S1, 17)
    llm.script("session_summary", _fails)

    assert await service.compact_if_needed(U, S1) is False

    session = _stored(store, S1)
    assert (session.summary, session.summary_through, session.summary_updated_at) == (
        None, None, None,
    )
    assert (await service.build_history(U, S1)).history == _turns(*range(1, 18))


@pytest.mark.asyncio
async def test_compact__after_a_failure__retried_later_not_every_turn(store, service, llm, clock):
    _session(store, S1)
    _messages(store, S1, 17)
    llm.script("session_summary", _fails)
    await service.compact_if_needed(U, S1)
    llm.script("session_summary", _summary())

    assert await service.compact_if_needed(U, S1) is False  # the next turn: no call
    clock.now += timedelta(minutes=config.SUMMARY_RETRY_MINUTES)
    assert await service.compact_if_needed(U, S1) is True

    assert llm.tasks() == ["session_summary", "session_summary"]


@pytest.mark.asyncio
async def test_compact__blank_summary__treated_as_a_failure(store, service, llm):
    _session(store, S1, summary="You planned graphs.", through=T0)
    _messages(store, S1, 17)
    llm.script("session_summary", _summary("   "))

    assert await service.compact_if_needed(U, S1) is False

    assert _stored(store, S1).summary == "You planned graphs."  # the old one stands


@pytest.mark.asyncio
async def test_compact__legacy_backlog__oldest_first_pages_until_every_message_is_covered(
    store, service, llm
):
    _session(store, S1)
    for n in range(1, 71):  # a chat older than summaries: 70 long messages, none summarised
        store.chat_messages.append(ChatMessage(
            id=str(uuid.uuid4()), session_id=S1, user_id=U,
            role="user" if n % 2 else "assistant",
            content=f"msg-{n:03d}-" + "x" * 990, created_at=T0 + timedelta(minutes=n),
        ))
    llm.script("session_summary", _summary())

    runs = [await service.compact_if_needed(U, S1) for _ in range(5)]

    calls = llm.calls_for("session_summary")
    assert runs == [True, True, True, False, False]  # one call per run; then nothing to do
    covered = [
        [n for n in range(1, 71) if f"msg-{n:03d}-" in call.prompt] for call in calls
    ]
    # Oldest first, no gaps, no repeats: every message but the newest 8 reaches a summary.
    assert [n for batch in covered for n in batch] == list(range(1, 63))
    for call in calls:
        transcript = call.prompt.split("<messages>\n", 1)[1].rsplit("\n</messages>", 1)[0]
        assert len(transcript) <= config.SUMMARY_INPUT_MAX_CHARS
    assert (await service.build_history(U, S1)).message_count == 8


@pytest.mark.asyncio
async def test_long_conversation__60_turns__window_bounded_and_newest_kept(store, service, llm):
    _session(store, S1)
    llm.script(
        "session_summary",
        lambda req: _summary(f"Summary {len(llm.calls_for('session_summary'))}."),
    )
    previous: list[dict] = []
    prefix_moves = 0
    writes = 0
    window_starts = 0
    for turn in range(60):
        window = await service.build_history(U, S1)
        assert len(window.history) <= config.HISTORY_MAX_MESSAGES
        if turn:
            assert window.history[-2:] == _turns(2 * turn - 1, 2 * turn)  # newest kept
            # However it was summarised, the newest 8 messages are always word for word.
            assert window.history[-8:] == _turns(*range(max(1, 2 * turn - 7), 2 * turn + 1))
        if window.history[: len(previous)] != previous:  # the prefix moved: a summary write
            prefix_moves += 1
        previous = window.history
        _messages(store, S1, 2, first=2 * turn + 1)
        start = _stored(store, S1).summary_through
        writes += await service.compact_if_needed(U, S1)
        if turn in (20, 40):  # the user goes idle; the sweep summarises everything
            writes += await service.summarize_session(U, S1)
        window_starts += _stored(store, S1).summary_through != start

    # One call per summary written, never one per turn. The window's start moved only
    # when a summary moved it (between those it only grew); an idle summary over 8 or
    # fewer new messages leaves it where it was.
    assert len(llm.calls_for("session_summary")) == writes <= 60 // 5 + 2
    assert prefix_moves == window_starts < writes
    # Each summary is written over the one before it.
    assert all(
        f"Summary {n}." in call.prompt
        for n, call in enumerate(llm.calls_for("session_summary")[1:], start=1)
    )


# --- background compaction ---------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_in_background__twice_at_once__one_call_tracked_until_shutdown(
    store, service, llm
):
    _session(store, S1)
    _messages(store, S1, 18)
    llm.script("session_summary", _summary())

    service.compact_in_background(U, S1)
    service.compact_in_background(U, S1)  # in flight: asks for one more look after it
    await service.settle()

    assert llm.tasks() == ["session_summary"]  # the second look found nothing to do
    await service.shutdown()
    service.compact_in_background(U, S1)  # closed: nothing starts
    await asyncio.sleep(0)
    assert llm.tasks() == ["session_summary"]


# --- idle summaries ----------------------------------------------------------------


class _CountingConversations:
    """Wraps a ConversationRepository and counts every read and write made through it."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        method = getattr(self._inner, name)

        async def counted(*args, **kwargs):
            self.calls.append(name)
            return await method(*args, **kwargs)

        return counted


@pytest.mark.asyncio
async def test_summary_sweep__only_idle_unsummarized_sessions(store, repos, clock, llm):
    now = clock.now
    idle = now - timedelta(minutes=45)
    # S1: idle 45 minutes, four turns nobody summarised -> summarised.
    _session(store, S1, updated_at=idle)
    s1 = _messages(store, S1, 4, start=idle - timedelta(minutes=5))
    # S2: still active (10 minutes) -> left alone.
    _session(store, S2, updated_at=now - timedelta(minutes=10))
    _messages(store, S2, 4, start=now - timedelta(minutes=15))
    # S3: idle, already summarised through its last message -> left alone.
    s3_last = now - timedelta(hours=2)
    _session(store, S3, updated_at=s3_last, summary="Done before.", through=s3_last)
    # S4: idle with a single message -> nothing worth a call.
    _session(store, S4, updated_at=now - timedelta(hours=1))
    _messages(store, S4, 1, start=now - timedelta(hours=1, minutes=2))
    # S5: another user's idle session -> not this user's sweep.
    _session(store, S5, user=BOB, updated_at=idle)
    _messages(store, S5, 4, user=BOB, start=idle - timedelta(minutes=5))
    llm.script("session_summary", _summary("You talked about graphs."))
    sweep = SessionSummarySweep(
        ConversationMemoryService(repos.conversations, repos.task_events, clock=clock)
    )

    assert await sweep.run([U], now) == 1

    assert [(c.task, c.session_id) for c in llm.calls] == [("session_summary", S1)]
    assert _stored(store, S1).summary == "You talked about graphs."
    # Four messages, all kept word for word: the window still starts at the session's start.
    assert _stored(store, S1).summary_through == _stored(store, S1).created_at
    assert all(m.content in llm.calls[0].prompt for m in s1)
    assert _stored(store, S2).summary is None and _stored(store, S4).summary is None
    assert _stored(store, S5).summary is None
    assert (SessionSummarySweep.name, SessionSummarySweep.interval) == (
        "session_summary", timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_summary_sweep__nothing_due__no_reads_until_the_daily_rescan(
    store, repos, clock, llm
):
    _session(store, S1, updated_at=clock.now - timedelta(days=2), summary="Old.", through=T0)
    counting = _CountingConversations(repos.conversations)
    sweep = SessionSummarySweep(
        ConversationMemoryService(counting, repos.task_events, clock=clock)
    )

    await sweep.run([U], clock.now)  # the first sight of the user: one bounded read
    first = list(counting.calls)
    for minutes in range(5, 60, 5):
        await sweep.run([U], clock.now + timedelta(minutes=minutes))

    assert first == ["recent_sessions"]
    assert counting.calls == first  # every later tick: no reads at all
    await sweep.run([U], clock.now + timedelta(hours=config.SESSION_SUMMARY_RESCAN_HOURS))
    assert counting.calls == first + ["recent_sessions"]
    assert llm.calls == []


@pytest.mark.asyncio
async def test_summary_sweep__after_a_turn__summarised_once_idle(store, repos, clock, llm):
    _session(store, S1, updated_at=clock.now)
    _messages(store, S1, 4, start=clock.now - timedelta(minutes=5))
    service = ConversationMemoryService(repos.conversations, repos.task_events, clock=clock)
    sweep = SessionSummarySweep(service)
    await sweep.run([U], clock.now)  # first sight: the session is not idle yet
    llm.script("session_summary", _summary("You talked about graphs."))

    service.compact_in_background(U, S1)  # the turn notes the session as active
    await service.settle()
    await sweep.run([U], clock.now + timedelta(minutes=20))
    assert llm.calls == []
    await sweep.run([U], clock.now + timedelta(minutes=config.SESSION_IDLE_MINUTES + 1))

    assert llm.tasks() == ["session_summary"]
    assert _stored(store, S1).summary == "You talked about graphs."


@pytest.mark.asyncio
async def test_summary_sweep__call_fails__session_kept_and_retried_later(store, repos, clock, llm):
    idle = clock.now - timedelta(hours=1)
    _session(store, S1, updated_at=idle)
    _messages(store, S1, 4, start=idle - timedelta(minutes=5))
    sweep = SessionSummarySweep(
        ConversationMemoryService(repos.conversations, repos.task_events, clock=clock)
    )
    llm.script("session_summary", _fails)

    assert await sweep.run([U], clock.now) == 0
    await sweep.run([U], clock.now + timedelta(minutes=5))  # within the back-off: no call
    llm.script("session_summary", _summary())
    later = clock.now + timedelta(minutes=config.SUMMARY_RETRY_MINUTES)
    clock.now = later
    assert await sweep.run([U], later) == 1

    assert llm.tasks() == ["session_summary", "session_summary"]


@pytest.mark.asyncio
async def test_summary_sweep__idle_session_over_8__covers_all_window_keeps_newest_8(
    store, repos, clock, llm
):
    idle = clock.now - timedelta(hours=1)
    _session(store, S1, updated_at=idle)
    sent = _messages(store, S1, 12, start=idle - timedelta(minutes=15))
    llm.script("session_summary", _summary("You talked about graphs."))
    service = ConversationMemoryService(repos.conversations, repos.task_events, clock=clock)

    assert await SessionSummarySweep(service).run([U], clock.now) == 1

    (call,) = llm.calls
    assert all(m.content in call.prompt for m in sent)  # the summary covers every message
    assert _stored(store, S1).summary_through == sent[3].created_at  # the 9th newest
    window = await service.build_history(U, S1)
    assert window.history == _turns(*range(5, 13))  # the newest 8, word for word
    assert window.earlier == "Earlier in this conversation: You talked about graphs."


@pytest.mark.asyncio
async def test_summarize_session__eight_or_fewer_after_the_summary__window_start_kept(
    store, service, llm
):
    _session(store, S1, summary="You chose graphs.", through=T0)
    _messages(store, S1, 5)
    llm.script("session_summary", _summary("You chose graphs, then trees."))

    assert await service.summarize_session(U, S1) is True

    session = _stored(store, S1)
    assert (session.summary, session.summary_through) == ("You chose graphs, then trees.", T0)
    assert (await service.build_history(U, S1)).history == _turns(1, 2, 3, 4, 5)
    # The prompt says the newest turns may already be in the summary so far.
    assert "may already be covered" in llm.calls[0].system_prompt


@pytest.mark.asyncio
async def test_summary_sweep__idle_summarised_session__not_picked_again(store, repos, clock, llm):
    last = clock.now - timedelta(hours=3)
    # S1: idle-summarised 31 minutes after its last message. The DB stamps updated_at
    # from its own clock, a little after the message's created_at: still not new.
    _session(store, S1, updated_at=last + timedelta(milliseconds=50))
    _messages(store, S1, 4, start=last - timedelta(minutes=4))
    store.chat_sessions[0] = store.chat_sessions[0].model_copy(update={
        "summary": "You talked about graphs.", "summary_through": T0 - timedelta(days=30),
        "summary_updated_at": last + timedelta(minutes=31),
    })
    # S2: summarised, then the user said more: picked.
    _session(store, S2, updated_at=last)
    _messages(store, S2, 4, start=last - timedelta(minutes=4))
    store.chat_sessions[1] = store.chat_sessions[1].model_copy(update={
        "summary": "Earlier.", "summary_through": T0 - timedelta(days=30),
        "summary_updated_at": last - timedelta(hours=1),
    })
    llm.script("session_summary", _summary("You talked about trees."))
    service = ConversationMemoryService(repos.conversations, repos.task_events, clock=clock)
    sweep = SessionSummarySweep(service)

    assert await sweep.run([U], clock.now) == 1
    later = clock.now + timedelta(hours=config.SESSION_SUMMARY_RESCAN_HOURS)
    clock.now = later
    await sweep.run([U], later)  # the daily rescan: both summarised, neither again

    assert [c.session_id for c in llm.calls] == [S2]
    assert _stored(store, S1).summary == "You talked about graphs."


# --- since we last spoke -----------------------------------------------------------


def _event(
    event_type: TaskEventType, at: datetime, task_id: str, slot: datetime | None = None
) -> TaskEvent:
    return TaskEvent(
        user_id=U, task_id=task_id, event_type=event_type, occurred_at=at,
        scheduled_start=slot or at - timedelta(minutes=30),
    )


@pytest.mark.asyncio
async def test_since_last_spoke__new_session__previous_summary_and_counts(
    store, repos, service, clock, llm
):
    clock.now = datetime(2026, 11, 16, 9, 0, tzinfo=IST)
    last_spoke = datetime(2026, 11, 15, 20, 0, tzinfo=IST)
    _session(
        store, S1, title="Plan my DSA revision.", updated_at=last_spoke,
        summary="You were planning your DSA revision for Friday.", through=last_spoke,
    )
    _messages(store, S1, 4, start=last_spoke - timedelta(minutes=10))
    _session(store, S2, updated_at=clock.now)
    t3_slot = last_spoke + timedelta(minutes=30)
    events = [
        _event(TaskEventType.COMPLETED, last_spoke - timedelta(hours=2), "before"),  # earlier
        _event(TaskEventType.COMPLETED, last_spoke + timedelta(hours=13), "t1"),
        _event(TaskEventType.COMPLETED, last_spoke + timedelta(hours=13), "t2"),
        _event(TaskEventType.MISSED, last_spoke + timedelta(hours=1), "t3", t3_slot),
        # The same slot, done after all: it counts once, as done.
        _event(TaskEventType.COMPLETED, last_spoke + timedelta(hours=12), "t3", t3_slot),
        _event(TaskEventType.SKIPPED, last_spoke + timedelta(hours=12), "t4"),
        _event(TaskEventType.MISSED, last_spoke + timedelta(hours=11), "t5"),
    ]
    await repos.task_events.append_many(U, events)

    text = await service.since_last_spoke(U, S2, zone=IST)

    assert text == (
        "Since we last spoke (yesterday): You were planning your DSA revision for Friday. "
        "Since then: 3 done, 2 set aside."
    )
    assert llm.calls == []


@pytest.mark.asyncio
async def test_since_last_spoke__unsummarized_previous__title_fallback_and_no_llm_call(
    store, service, clock, llm
):
    three_days_ago = clock.now - timedelta(days=3)
    _session(store, S1, title="Plan my DSA revision for Friday.", updated_at=three_days_ago)
    _messages(store, S1, 2, start=clock.now - timedelta(days=3, minutes=5))
    _session(store, S2, updated_at=clock.now)

    text = await service.since_last_spoke(U, S2, zone=IST)

    assert text == (
        "Since we last spoke (3 days ago): "
        'you were talking about "Plan my DSA revision for Friday".'
    )
    assert llm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last", "now", "gap"),
    [
        # 00:30 IST is still the 15th in UTC: the user's clock says yesterday.
        (datetime(2026, 11, 15, 23, 0, tzinfo=IST), datetime(2026, 11, 16, 0, 30, tzinfo=IST),
         "yesterday"),
        (datetime(2026, 11, 16, 8, 0, tzinfo=IST), datetime(2026, 11, 16, 8, 40, tzinfo=IST),
         "40 minutes ago"),
        (datetime(2026, 11, 16, 1, 0, tzinfo=IST), datetime(2026, 11, 16, 9, 0, tzinfo=IST),
         "earlier today"),
        (datetime(2026, 10, 26, 9, 0, tzinfo=IST), datetime(2026, 11, 16, 9, 0, tzinfo=IST),
         "3 weeks ago"),
    ],
)
async def test_since_last_spoke__gap__counted_on_the_users_clock(
    store, service, clock, llm, last, now, gap
):
    clock.now = now
    _session(store, S1, title="Plan my week.", updated_at=last)
    _messages(store, S1, 2, start=last - timedelta(minutes=5))
    _session(store, S2, updated_at=now)

    text = await service.since_last_spoke(U, S2, zone=IST)

    assert text == f'Since we last spoke ({gap}): you were talking about "Plan my week".'


@pytest.mark.asyncio
async def test_since_last_spoke__second_turn__none(store, service, clock, llm):
    _session(store, S1, title="Plan my week.", updated_at=clock.now - timedelta(days=1))
    _messages(store, S1, 2, start=clock.now - timedelta(days=1, minutes=5))
    _session(store, S2, updated_at=clock.now)
    _messages(store, S2, 2, start=clock.now - timedelta(minutes=5))

    assert await service.since_last_spoke(U, S2, zone=IST) is None
    # A first conversation has no previous one to pick up from.
    _session(store, S3, user=BOB, updated_at=clock.now)
    assert await service.since_last_spoke(BOB, S3, zone=IST) is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_since_last_spoke__no_blame_phrases(store, repos, service, clock, llm):
    last = clock.now - timedelta(days=2)
    _session(store, S1, title="Plan my week.", updated_at=last)
    _messages(store, S1, 2, start=last - timedelta(minutes=5))
    _session(store, S2, updated_at=clock.now)
    await repos.task_events.append_many(U, [
        _event(TaskEventType.MISSED, last + timedelta(hours=3), "t1"),
        _event(TaskEventType.SKIPPED, last + timedelta(hours=4), "t2"),
        _event(TaskEventType.MISSED, last + timedelta(hours=5), "t3"),
    ])

    text = await service.since_last_spoke(U, S2, zone=IST)

    assert text == (
        'Since we last spoke (2 days ago): you were talking about "Plan my week". '
        "Since then: 3 set aside."
    )
    assert blame_phrases_in(text) == []
    assert not {"missed", "behind", "failed"} & set(text.lower().replace(".", "").split())


@pytest.mark.asyncio
async def test_since_last_spoke__new_patterns_and_waiting_suggestions__counted(
    store, repos, clock, llm
):
    from app.domain.entities import Memory, MemorySource, Suggestion, SuggestionStatus

    last = clock.now - timedelta(days=1)
    _session(store, S1, title="Plan my week.", updated_at=last)
    _messages(store, S1, 2, start=last - timedelta(minutes=5))
    _session(store, S2, updated_at=clock.now)
    memory = MemoryService(repos.memories, BagOfWordsEmbedder(), clock=clock)
    await repos.memories.add(U, Memory(
        user_id=U, memory_type="behavioral_pattern", provenance=MemorySource.INFERRED,
        content="You tend to set aside tasks after 21:00.", confidence=0.7,
        strength_as_of=clock.now, created_at=last + timedelta(hours=2),
    ))
    await repos.suggestions.add(U, Suggestion(
        user_id=U, trigger="morning_brief", kind="replan", title="A new slot",
        body="Two tasks found a new slot.", status=SuggestionStatus.PENDING,
        consent_behavior="ask", consent_reason="You asked to be asked.",
        created_at=last + timedelta(hours=3),
    ))
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock,
        memory=memory, suggestions=repos.suggestions,
    )

    text = await service.since_last_spoke(U, S2, zone=IST)

    assert text.endswith("Since then: 1 new pattern noticed, 1 suggestion waiting.")


# --- forget holds ------------------------------------------------------------------

_SISTER = "My sister Priya lives in Pune"
_SUMMARY_WITH_SISTER = (
    "You were planning your DSA revision for Friday. "
    "Your sister Priya lives in Pune and visits on Sunday."
)


async def _forgotten(repos, clock) -> MemoryService:
    memory = MemoryService(repos.memories, BagOfWordsEmbedder(), clock=clock)
    outcome = await memory.remember(U, MemoryCandidate(memory_type="fact", content=_SISTER))
    await memory.forget(U, outcome.memory.id)
    return memory


@pytest.mark.asyncio
async def test_since_last_spoke__forgotten_fact__dropped_from_the_summary(
    store, repos, clock, llm
):
    memory = await _forgotten(repos, clock)
    last = clock.now - timedelta(days=1)
    _session(store, S1, title="Sister visiting.", updated_at=last,
             summary=_SUMMARY_WITH_SISTER, through=last)
    _messages(store, S1, 2, start=last - timedelta(minutes=5))
    _session(store, S2, updated_at=clock.now)
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock, memory=memory
    )

    text = await service.since_last_spoke(U, S2, zone=IST)

    assert text == (
        "Since we last spoke (yesterday): You were planning your DSA revision for Friday."
    )


@pytest.mark.asyncio
async def test_build_history__forgotten_fact__dropped_from_the_summary(store, repos, clock, llm):
    memory = await _forgotten(repos, clock)
    _session(store, S1, summary=_SUMMARY_WITH_SISTER, through=T0)
    _messages(store, S1, 2)
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock, memory=memory
    )

    window = await service.build_history(U, S1)

    assert window.earlier == (
        "Earlier in this conversation: You were planning your DSA revision for Friday."
    )


@pytest.mark.asyncio
async def test_build_history__suppression_unreadable__summary_withheld(store, repos, clock, llm):
    memory = await _forgotten(repos, clock)

    class _Down(BagOfWordsEmbedder):
        def embed(self, texts):
            raise EmbeddingUnavailableError("the embedder is down")

    memory._embedder = _Down()
    _session(store, S1, summary=_SUMMARY_WITH_SISTER, through=T0)
    _messages(store, S1, 2)
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock, memory=memory
    )

    window = await service.build_history(U, S1)

    # Fails closed: the summary is not shown unchecked.
    assert "Priya" not in (window.earlier or "")
    assert window.earlier == (
        "Earlier messages in this conversation are not shown here; "
        "the ones that follow are the most recent."
    )


@pytest.mark.asyncio
async def test_compact__old_summary_with_a_forgotten_fact__not_sent_to_the_model(
    store, repos, clock, llm
):
    memory = await _forgotten(repos, clock)
    _session(store, S1, summary=_SUMMARY_WITH_SISTER, through=T0)
    _messages(store, S1, 17)
    llm.script("session_summary", _summary())
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock, memory=memory
    )

    assert await service.compact_if_needed(U, S1) is True

    (call,) = llm.calls
    assert "DSA revision" in call.prompt and "Priya" not in call.prompt


@pytest.mark.asyncio
async def test_compact__stored_summary_holds_a_forgotten_fact__written_back_without_a_model_call(
    store, repos, clock, llm
):
    memory = await _forgotten(repos, clock)
    written = T0 + timedelta(minutes=30)
    _session(store, S1, summary=_SUMMARY_WITH_SISTER, through=T0)
    store.chat_sessions[0] = store.chat_sessions[0].model_copy(
        update={"summary_updated_at": written}
    )
    _messages(store, S1, 4)  # far from a compaction
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock, memory=memory
    )

    assert await service.compact_if_needed(U, S1) is False

    assert llm.calls == []
    session = _stored(store, S1)
    assert session.summary == "You were planning your DSA revision for Friday."
    # Not a new summary: how far it reaches, and when it was written, stay as they were.
    assert (session.summary_through, session.summary_updated_at) == (T0, written)


@pytest.mark.asyncio
async def test_compact__new_summary_repeats_a_forgotten_fact__stored_without_it(
    store, repos, clock, llm
):
    memory = await _forgotten(repos, clock)
    _session(store, S1)
    _messages(store, S1, 17)
    llm.script(
        "session_summary", _summary(_SUMMARY_WITH_SISTER, ["Visit your sister Priya in Pune"])
    )
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock, memory=memory
    )

    assert await service.compact_if_needed(U, S1) is True

    assert _stored(store, S1).summary == "You were planning your DSA revision for Friday."


def test_check_pieces__run_on_sentence__the_sentence_and_each_clause():
    from app.services.memory.summary_text import check_pieces

    sentence = (
        "You were planning your DSA revision for Friday, and you mentioned that your "
        "sister Priya lives in Pune, so we kept Sunday afternoon free."
    )

    assert check_pieces(sentence) == [
        sentence,
        "You were planning your DSA revision for Friday",
        "you mentioned that your sister Priya lives in Pune",
        "we kept Sunday afternoon free",
    ]
    assert check_pieces("You chose graphs.") == ["You chose graphs."]


@pytest.mark.asyncio
async def test_since_last_spoke__model_summary_with_blame_or_tags__dropped_and_neutralised(
    store, service, clock, llm
):
    last = clock.now - timedelta(days=1)
    _session(
        store, S1, title="Plan my week.", updated_at=last, through=last,
        summary="You missed the gym twice. You failed to start the essay. "
        "You were planning <b>DSA</b> for Friday.\n- Find a new slot for the gym",
    )
    _messages(store, S1, 2, start=last - timedelta(minutes=5))
    _session(store, S2, updated_at=clock.now)

    text = await service.since_last_spoke(U, S2, zone=IST)

    assert text == (
        "Since we last spoke (yesterday): You were planning ‹b›DSA‹/b› for Friday. "
        "Still open: Find a new slot for the gym."
    )
    assert blame_phrases_in(text) == []


@pytest.mark.asyncio
async def test_since_last_spoke__known_empty__never_reads_the_current_conversation(
    store, repos, clock, llm
):
    _session(store, S1, title="Plan my week.", updated_at=clock.now - timedelta(days=1))
    _messages(store, S1, 2, start=clock.now - timedelta(days=1, minutes=5))
    _session(store, S2, updated_at=clock.now)
    counting = _CountingConversations(repos.conversations)
    service = ConversationMemoryService(counting, repos.task_events, clock=clock)

    known = await service.since_last_spoke(U, S2, zone=IST, known_empty=True)
    reads_known = list(counting.calls)
    checked = await service.since_last_spoke(U, S2, zone=IST)

    assert known == checked
    assert "messages" not in reads_known
    assert counting.calls[len(reads_known)] == "messages"  # without the hint, it checks


def test_since_last_spoke__moved_module__names_still_importable_from_the_old_path():
    from app.services.memory import conversation_memory, since_last_spoke, summary_text

    assert conversation_memory.SinceLastSpoke is since_last_spoke.SinceLastSpoke
    assert conversation_memory.stored_summary is summary_text.stored_summary


@pytest.mark.asyncio
async def test_session_summary__routed_to_the_cloud__transcript_goes_through_the_pii_gate(
    store, service
):
    from app.core.model_router import PreCloudLLMRedactor
    from app.infrastructure.llm.decorators import RedactingProvider
    from app.orchestrator.hooks import ActionHooks, register_all_hooks

    hooks = ActionHooks()
    register_all_hooks(hooks)
    cloud = ScriptedLLM()  # a cloud provider, behind the chain's PreCloudLLM gate
    cloud.script("session_summary", _summary())
    set_chain(FallbackChainProvider([RedactingProvider(cloud, PreCloudLLMRedactor(hooks))]))
    _session(store, S1)
    _messages(store, S1, 16)
    store.chat_messages.insert(0, ChatMessage(
        id=str(uuid.uuid4()), session_id=S1, user_id=U, role="user",
        content="Mail madhav@example.com or call 555-123-4567 about the plan",
        created_at=T0,
    ))

    assert await service.compact_if_needed(U, S1) is True

    (call,) = cloud.calls
    assert "madhav@example.com" not in call.prompt and "555-123-4567" not in call.prompt
    assert "[EMAIL]" in call.prompt and "[PHONE]" in call.prompt


@pytest.mark.slow
@pytest.mark.asyncio
async def test_forget_filter__production_embedder__run_on_sentence_dropped_unrelated_kept(
    store, repos, clock, llm
):
    """Calibration on the real sentence model (all-MiniLM-L6-v2, local, no network once
    cached). Skipped when the model is not on this machine."""
    from app.infrastructure.embedding import MiniLMEmbedder

    embedder = MiniLMEmbedder()
    try:
        embedder.embed(["warm up"])
    except EmbeddingUnavailableError:
        pytest.skip("the local sentence model is not downloaded")
    memory = MemoryService(repos.memories, embedder, clock=clock)
    for fact in ("My sister Priya lives in Pune", "I'm going through a divorce"):
        outcome = await memory.remember(U, MemoryCandidate(memory_type="fact", content=fact))
        await memory.forget(U, outcome.memory.id)
    kept = [
        "You were planning your DSA revision for Friday.",
        "You said evenings are hard this week, so we kept them light.",
        "Your family is visiting on Sunday, so we kept the afternoon free.",
    ]
    dropped = [
        "We moved graphs to Saturday morning, and you mentioned that your sister Priya lives "
        "in Pune and is visiting on Sunday, so we kept Sunday afternoon free.",
        "You said the divorce has made evenings hard, so we kept evenings light this week "
        "and moved the reading to mornings.",
    ]
    _session(store, S1, summary=" ".join([kept[0], dropped[0], kept[1], dropped[1], kept[2]]),
             through=T0)
    _messages(store, S1, 2)
    service = ConversationMemoryService(
        repos.conversations, repos.task_events, clock=clock, memory=memory
    )

    window = await service.build_history(U, S1)

    assert window.earlier == "Earlier in this conversation: " + " ".join(kept)
