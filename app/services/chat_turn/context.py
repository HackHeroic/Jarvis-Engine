"""What a turn knows before the graph runs: its initial state, and the conversation as
it stands, read under the turn lock."""

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain.clock import utc_now
from app.orchestrator.graph import is_trivial_input
from app.schemas.chat_stream import ChatStreamRequest
from app.services import chat_history
from app.services.memory.context import Recall, note_line, render_context_note
from app.services.planning.local_time import user_zone


@dataclass
class TurnContext:
    """The conversation as the turn read it, for the graph and the ``complete`` frame."""

    history: list[dict]
    memory_note: str
    # {memory_id, memory_type, content, confidence, provenance}: what recall found.
    memories_shown: list[dict]
    # "Since we last spoke" exactly as the note gave it to the model, when it did.
    since_last_spoke: str | None
    user_message_id: str | None
    memory_snapshot: Any


def initial_state(
    request: ChatStreamRequest,
    *,
    session_id: str,
    user_model: Any,
    progress_callback: Callable[..., None],
    progress_queue: asyncio.Queue,
    container: Any,
    db_client: Any = None,
    force_cloud_request: bool,
) -> dict:
    """The orchestrator's input for this turn. The conversation fields are filled under
    the turn lock (``read_conversation``), so a turn that waited sees the reply before it."""
    return {
        # Serializable identity: survives checkpointing; user_model does not (each turn
        # brings its own; _load_context builds one from the container when it did not).
        "user_id": request.user_id,
        "user_model": user_model,
        "user_message": request.user_prompt,
        "file_base64": request.file_base64,
        "file_media_type": request.media_type,
        "file_name": request.file_name,
        "brain_dump": None,
        "intent": None,
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        # The composition root, for nodes that need it (planning proposes its draft
        # through it, draft_action accepts through it); transient, never checkpointed.
        # draft_id itself is deliberately absent, like the phase fields: a resumed
        # thread's "looks good" must resolve the draft that thread proposed, and a None
        # here would overwrite it.
        "container": container,
        # The DatabaseClient, for the document rows with no repository yet (the knowledge
        # module records an upload with it); transient, never checkpointed.
        "db_client": db_client,
        "saved_constraints": None,
        "saved_rule_ids": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        # Per turn: a checkpointed framing would re-announce last turn's missed tasks.
        "anti_guilt_message": None,
        "missed_deadlines": None,
        "thinking_process": None,
        "response_message": None,
        # conversation_phase / negotiation_state are deliberately absent: on a thread with
        # a checkpoint they come from it, or the negotiation shortcut could never fire.
        # Seeded only for a brand-new thread (ChatTurn._seed_if_fresh_thread).
        "modules_invoked": [],
        "needs_followup": False,
        "needs_consent": None,
        "error": None,
        "conversation_history": [],
        "memory_context": "",
        "progress_callback": progress_callback,
        "progress_queue": progress_queue,
        # Per turn, cleared explicitly: it is checkpointed, and the negotiation shortcut
        # skips extract_brain_dump, so a stale True would mute the observation loop.
        "trivial_input": None,
        # What this turn's memories cite as their source: the conversation, and the
        # user's message (its id filled in once it is saved). The handles are per turn too.
        "session_id": session_id,
        "user_message_id": None,
        "memory_handles": {},
        # The user's memories read once for the turn, and PEARL's insights. Both per turn.
        "memory_snapshot": None,
        "pearl_insights": None,
        # The model picker chose a cloud model: every LLM call of the turn goes there.
        "force_cloud_request": force_cloud_request,
    }


async def read_conversation(
    container: Any,
    user_model: Any,
    request: ChatStreamRequest,
    session_id: str,
    *,
    supabase: Any = None,
) -> TurnContext:
    """Read the conversation as it stands now. Runs under the turn lock.

    History first, then save: the window is the PRIOR context. Saved first, the message
    being answered also sat in its own history — the model read it twice and it took one
    of the window's slots. The window is ``ConversationMemoryService.build_history``: the
    messages after the conversation's summary, the summary itself going in the note
    (``<earlier>``). A conversation with no messages yet, opened after an earlier one,
    gets "since we last spoke" (``<since>``, no model call).

    Two stages of reads: the window with the profile; then, at once, "since we last
    spoke" (only for an empty window), recall, the plate and the save of this message,
    none of which reads this conversation's messages.

    Then the turn's context note: what ``MemoryService.recall`` finds for this message
    within its token budgets (a trivial message gets the profile only), the plate and the
    time on the user's clock. The memory panel shows exactly the recalled memories. A read
    that fails is left out of the note and logged; the turn goes on. Without a composition
    root there is no note, and the history is the plain newest-ten window, as it is when
    the window cannot be read (for one, before the summary columns' migration is applied).
    ``supabase`` is for the ``chat_history`` calls only.
    """
    if container is None:
        history = await chat_history.build_context_messages(
            session_id, request.user_id, max_messages=10, max_chars_per_message=500,
            supabase=supabase,
        )
        user_message_id = await chat_history.save_user_message(
            session_id, request.user_id, request.user_prompt, supabase
        )
        return TurnContext(history=history, memory_note="", memories_shown=[],
                           since_last_spoke=None, user_message_id=user_message_id,
                           memory_snapshot=None)
    conversations = container.conversation_memory

    async def _snapshot():
        try:
            return await container.memory_service.snapshot(request.user_id)
        except Exception as exc:  # each reader reads for itself, never a failed turn
            logger.warning(f"Memories unread for the turn ({type(exc).__name__})")
            return None

    # The user's memories, read once for the whole turn: recall, the extraction's known
    # memories and every forget check share this one read.
    snapshot_read = asyncio.ensure_future(_snapshot())

    async def _let_go_ids():
        snapshot = await snapshot_read
        return snapshot.let_go_ids if snapshot is not None else None

    async def _window():
        try:
            return await conversations.build_history(
                request.user_id, session_id, let_go_ids=_let_go_ids
            )
        except Exception as exc:  # the plain newest-ten window, never a failed turn
            logger.warning(f"Conversation history unavailable this turn ({type(exc).__name__})")
            return None

    # Stage 1, before this message is saved: the window, the profile and the memories.
    window, profile, snapshot = await asyncio.gather(
        _window(), user_model.get_profile(), snapshot_read
    )
    if window is not None:
        history = window.history
    else:
        history = await chat_history.build_context_messages(
            session_id, request.user_id, max_messages=10, max_chars_per_message=500,
            supabase=supabase,
        )
    zone = user_zone(profile)
    now = utc_now()

    async def _since() -> str | None:
        if window is None or window.message_count or window.summarized:
            return None
        try:  # the window found no messages: "since" need not read them again
            return await conversations.since_last_spoke(
                request.user_id, session_id, zone=zone, known_empty=True, snapshot=snapshot,
            )
        except Exception as exc:  # nothing to pick up this turn, never a failed turn
            logger.warning(f"'Since we last spoke' unavailable ({type(exc).__name__})")
            return None

    async def _recall() -> Recall:
        try:
            return await container.memory_service.recall(
                request.user_id,
                request.user_prompt,
                trivial=is_trivial_input(request.user_prompt),
                now=now,
                snapshot=snapshot,
            )
        except Exception as exc:  # no memories this turn, never a failed turn
            logger.warning(f"Recall unavailable this turn ({type(exc).__name__})")
            return Recall(profile=[], relevant=[], tokens=0)

    async def _plate() -> list[str]:
        try:
            pending = await container.repos.tasks.list_pending(request.user_id)
        except Exception as exc:  # no plate this turn, never a failed turn
            logger.warning(f"Pending tasks unavailable for the context note ({type(exc).__name__})")
            return []
        pending.sort(  # soonest first; task_id breaks ties, so the note is stable
            key=lambda t: (t.scheduled_start is None, t.scheduled_start or now, t.task_id)
        )
        return [
            f"{t.title} (due {t.deadline_hint})" if t.deadline_hint else t.title
            for t in pending
        ]

    # Stage 2, all at once: none of these reads this conversation's own messages, so
    # saving the message alongside them is safe.
    since, recall, plate, user_message_id = await asyncio.gather(
        _since(),
        _recall(),
        _plate(),
        chat_history.save_user_message(session_id, request.user_id, request.user_prompt, supabase),
    )
    note = render_context_note(
        recall, now=now, zone=zone, since=since,
        earlier=window.earlier if window is not None else None, plate=plate,
    )
    return TurnContext(
        history=history,
        memory_note=note or "",
        memories_shown=[
            {
                "memory_id": m.id,
                "memory_type": m.memory_type,
                "content": m.content,
                "confidence": m.confidence,
                "provenance": m.provenance.value,
            }
            for m in recall.all()
        ],
        since_last_spoke=note_line(since) if since else None,
        user_message_id=user_message_id,
        memory_snapshot=snapshot,
    )
