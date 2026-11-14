"""One chat turn: the LangGraph orchestrator run, streamed as server-sent events.

``ChatTurnService.open_turn`` runs in the request handler's context: the model picker's
cloud flag, the usage attribution and the session are set there, before the response
starts, so the stream and every task it starts inherit them. ``ChatTurn.frames`` is the
stream; ``ChatTurn.finish`` (the response's background task) ends what the handler set.
"""

import asyncio
import json
import time
from contextlib import AsyncExitStack, aclosing
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

import anyio

import app.core.config as _cfg
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.core.model_router import (
    ServedModels, attribute_llm_calls, force_cloud_var, recording_served_models,
)
from app.core.observation import pearl_insights
from app.core.user_model import UserModel
from app.domain.errors import JarvisError
from app.orchestrator.checkpoint import make_thread_id
from app.orchestrator.state import ConversationPhase, NegotiationPhase
from app.schemas.chat_stream import (
    ChatCompletePayload, ChatStreamRequest, SseEncoder, SseEvent, StepFrame,
)
from app.services import chat_history
from app.services.chat_turn.context import initial_state, read_conversation
from app.services.chat_turn.frames import error_frame, intent_str, node_frames
from app.services.chat_turn.reply import (
    ReplyText, build_complete_payload, final_reply, reply_frames,
)
from app.services.turn_locks import thread_lock

if TYPE_CHECKING:
    from app.container import AppContainer

# The LLM tasks whose model is credited with the reply.
_REPLY_TASKS = ("voice_of_jarvis", "voice_of_jarvis_synthesis")
# The model picker's choices that put the whole turn on the cloud providers: 'cloud',
# and the aliases it has had ('gemini', the older '4b'). Every other mode is 'auto'.
CLOUD_MODEL_MODES = frozenset({"cloud", "gemini", "4b"})
# Graph node events ride the SAME queue as progress and token events, so the stream keeps
# true emission order: draining only between astream() iterations would block for the
# whole synthesis call and deliver every "streamed" token in one burst at the end.
_NODE, _ERROR, _DONE = "__node__", "__error__", "__done__"


class ChatTurn:
    """One open turn: ``frames()`` streams it, ``finish()`` ends what opening it started."""

    def __init__(self, service: "ChatTurnService", request: ChatStreamRequest, *,
                 session_id: str, request_id: str, state: dict, served: ServedModels,
                 ends: list[Callable[[], None]]) -> None:
        self.session_id = session_id
        self._service = service
        self._request = request
        self._request_id = request_id
        self._state = state
        self._served = served
        self._ends = ends
        # One checkpoint thread per (user, session). The user prefix matters: a session-only
        # key would let one user's turn resume another user's checkpointed conversation.
        self._thread_id = make_thread_id(request.user_id, session_id)
        self._config = {"configurable": {"thread_id": self._thread_id}}
        self._memories: list[dict] = []
        self._since: str | None = None

    async def finish(self) -> None:
        """End the attribution, the served-model record and the cloud flag. Async on
        purpose: a sync background task runs on a worker thread, in a copy of this
        context, where the ContextVar reset would raise."""
        for end in self._ends:
            end()

    def _answering_model(self) -> str:
        """The model that wrote this turn's reply; "cloud"/"local" when no model call did."""
        model = self._served.last_model_for(_REPLY_TASKS)
        if model:
            return model
        cloud_first = _cfg.GEMINI_PRIMARY or _cfg.CLAUDE_PRIMARY or force_cloud_var.get()
        return "cloud" if cloud_first else "local"

    async def _seed_if_fresh_thread(self) -> None:
        try:
            existing = await self._service.graph.aget_state(self._config)
            resumed = bool(existing and getattr(existing, "values", None))
        except Exception as _e:
            print(f"[chat] Checkpoint lookup failed ({_e}) — starting a fresh thread")
            resumed = False
        if not resumed:
            self._state["conversation_phase"] = ConversationPhase.GREETING
            self._state["negotiation_state"] = NegotiationPhase.NONE
            self._state["draft_id"] = None

    async def _read_conversation(self) -> None:
        """The conversation as it stands now, into the turn's state. Under the turn lock."""
        service = self._service
        context = await read_conversation(service.container, self._state["user_model"],
                                          self._request, self.session_id,
                                          supabase=service.supabase)
        self._state["conversation_history"] = context.history
        self._state["memory_context"] = context.memory_note
        self._state["user_message_id"] = context.user_message_id
        self._state["memory_snapshot"] = context.memory_snapshot
        self._memories = context.memories_shown
        self._since = context.since_last_spoke

    async def _pump_graph(self) -> None:
        queue = self._state["progress_queue"]
        try:
            await self._seed_if_fresh_thread()
            async for _ev in self._service.graph.astream(self._state, config=self._config):
                queue.put_nowait((_NODE, _ev))
        except Exception as _exc:
            queue.put_nowait((_ERROR, _exc))
        finally:
            queue.put_nowait((_DONE, None))

    async def frames(self) -> AsyncIterator[str]:
        """The turn's frames, then one ``complete`` frame, or one ``error`` frame."""
        request, state, queue = self._request, self._state, self._state["progress_queue"]
        text = ReplyText(turn_start=time.monotonic())
        pump_task: asyncio.Task | None = None
        turn_lock = AsyncExitStack()
        try:
            # One turn at a time per conversation: a second tab's turn on this thread
            # queues here until this turn is over — its reply saved, or failed, or
            # cancelled by a disconnect. History, memory and the checkpoint are all
            # read under the lock, so a queued turn sees what the turn before it left.
            # A wait past TURN_LOCK_WAIT_SECONDS ends in a conflict frame instead.
            await turn_lock.enter_async_context(thread_lock(self._thread_id))
            await self._read_conversation()
            final_state: dict = {}
            graph_exc: Exception | None = None
            pump_task = asyncio.create_task(self._pump_graph())

            while True:
                raw = await queue.get()
                if isinstance(raw, tuple):
                    kind, payload = raw
                    if kind == _DONE:
                        break
                    if kind == _ERROR:
                        graph_exc = payload
                        continue
                    node_name = list(payload.keys())[0]
                    node_state = payload[node_name]
                    if not isinstance(node_state, dict):
                        node_state = {}  # normalize None / non-dict to empty dict
                    final_state.update(node_state)
                    turns = len(state.get("conversation_history") or [])
                    for frame in node_frames(node_name, node_state,
                                              memories=len(self._memories), turns=turns):
                        yield frame
                    continue
                # Sub-graph / synthesis bridge event (JSON string)
                frame, evt_type, token = SseEncoder.render_progress(raw)
                if evt_type in ("token", "thinking_token"):
                    text.streamed(evt_type, token)
                yield frame

            await pump_task
            if graph_exc is not None:
                raise graph_exc

            intent = intent_str(final_state.get("intent", "CHAT"))
            has_schedule = bool(final_state.get("schedule"))
            yield SseEncoder.frame(SseEvent.STEP, StepFrame(
                intent=intent, stage="pipeline_done", model_mode=request.model_mode or "auto",
                synthesis_model=self._answering_model(), force_cloud=force_cloud_var.get(),
            ))
            async with aclosing(reply_frames(final_state, text, intent=intent,
                                             has_schedule=has_schedule, request=request,
                                             initial_state=state)) as reply:
                async for frame in reply:
                    yield frame
            reply_out = final_reply(final_state, text, has_schedule=has_schedule,
                                    answering_model=self._answering_model)

            message_id = await chat_history.save_assistant_message(
                self.session_id, request.user_id, reply_out.message, intent,
                {"schedule": final_state.get("schedule"), "draft_id": reply_out.draft_id,
                 "intent": intent},
                self._service.supabase,
            )
            # Memories and PEARL were learned in the graph's observation loop, and their
            # frames are already sent: every learning moment arrives before complete. Its
            # own look holds PEARL's insights; PEARL is asked only when that look did not
            # run or finish (a greeting, a slow store).
            insights = final_state.get("pearl_insights")
            if insights is None:
                insights = await pearl_insights(self._service.container, request.user_id)
            complete = build_complete_payload(
                final_state, text, reply_out, intent=intent, has_schedule=has_schedule,
                session_id=self.session_id, message_id=message_id, memories=self._memories,
                since_last_spoke=self._since, insights=insights or None,
            )
            # Compaction: a tracked background task the turn never waits for. It starts once
            # this frame is on its way; created before the yield, so a client that leaves at
            # the frame cannot skip it.
            if self._service.container is not None:
                self._service.container.conversation_memory.compact_in_background(
                    request.user_id, self.session_id
                )
            # The frame's contract, checked before it is sent: a frame off the contract is
            # a failed turn, not a silently different payload.
            yield SseEncoder.frame(SseEvent.COMPLETE, ChatCompletePayload.model_validate(complete))
        except JarvisError as exc:
            # Raised on purpose, with a user-facing message (a turn that waited too long
            # for the one before it is a conflict): the standard envelope.
            yield error_frame(self._request_id, exc.code, exc.message, exc.details)
        except Exception as exc:
            # The cause is logged with the request id; its text never goes to the client.
            logger.error(
                f"chat stream failed [request_id={self._request_id}]: {type(exc).__name__}",
                exc_info=exc,
            )
            yield error_frame(self._request_id, "internal_error",
                               "Something went wrong on our side.")
        finally:
            # Shielded: under uvicorn, Starlette streams inside an anyio task group and a
            # client disconnect cancels that scope — anyio then re-cancels every await made
            # inside it, so an unshielded wait below would itself be cancelled, the lock
            # would be left to the garbage collector, and a queued turn could start while
            # this run was still unwinding.
            with anyio.CancelScope(shield=True):
                # The graph runs in its own task, so a client disconnect would otherwise
                # leave it churning through LLM calls nobody reads.
                if pump_task is not None and not pump_task.done():
                    pump_task.cancel()
                    # Release the conversation only once this run has stopped writing it.
                    await asyncio.wait([pump_task])
                await turn_lock.aclose()


class ChatTurnService:
    """Opens chat turns over the orchestrator graph and the composition root."""

    def __init__(self, graph: Any, *, container: "AppContainer | None",
                 db_client: Any = None) -> None:
        # db_client: only for the rows with no repository yet (Plan 3 cut Task 6): the
        # session and its messages (chat_history), and a document the knowledge module
        # records. Everything else comes from the container.
        self.graph = graph
        self.container = container
        self.db_client = db_client
        self.supabase = getattr(db_client, "supabase", None) if db_client else None

    @classmethod
    def for_app(cls, state: Any) -> "ChatTurnService":
        """The service over what the lifespan put on ``app.state``."""
        return cls(state.jarvis_graph, container=getattr(state, "container", None),
                   db_client=getattr(state, "db_client", None))

    async def open_turn(self, request: ChatStreamRequest, *, request_id: str) -> ChatTurn:
        """Open one turn in the caller's context: the cloud flag (when the model picker
        chose a cloud model), the session, and the usage attribution every LLM call of
        the turn is metered under. ``ChatTurn.finish`` ends them."""
        model_mode = request.model_mode or "auto"
        force_cloud_request = model_mode in CLOUD_MODEL_MODES
        force_cloud_token = None
        if force_cloud_request:
            # Set BEFORE the graph is invoked so every LLM call inherits it.
            force_cloud_token = force_cloud_var.set(True)
            print(f"[chat] model_mode={model_mode} → force_cloud=True (cloud providers only)")
        session_id = await chat_history.get_or_create_session(
            request.user_id, request.conversation_id, self.supabase
        )
        # The user, read through the planning ports; without a container there is no
        # facade, and every node that reads the user degrades as it does without one.
        user_model = (
            UserModel.for_planning(request.user_id, self.container.planning)
            if self.container is not None else None
        )
        # Progress bridge: sub-graph callbacks → SSE phase events.
        progress_queue: asyncio.Queue = asyncio.Queue()

        def progress_cb(phase, **detail):
            progress_queue.put_nowait(json.dumps({"phase": phase, **detail}))

        state = initial_state(
            request, session_id=session_id, user_model=user_model,
            progress_callback=progress_cb, progress_queue=progress_queue,
            container=self.container,
            db_client=self.db_client,
            force_cloud_request=force_cloud_request,
        )
        # Which provider and model answered each LLM call, recorded by the metering
        # decorator; the reply is credited to the model that wrote it.
        served = ServedModels()
        # Every LLM call of this turn — the graph pump and every task it starts copy this
        # context — is metered to this user and session, until finish().
        end_attribution = attribute_llm_calls(request.user_id, session_id)
        end_recording = recording_served_models(served)
        ends = [end_recording, end_attribution]
        if force_cloud_token is not None:
            ends.append(lambda: force_cloud_var.reset(force_cloud_token))
        return ChatTurn(self, request, session_id=session_id, request_id=request_id,
                        state=state, served=served, ends=ends)
