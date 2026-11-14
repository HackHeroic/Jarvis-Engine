"""The turn's reply once the graph run is over: its frames, and the ``complete`` payload.

A reply the graph streamed through the progress bridge is already on the wire. Otherwise
it goes out here, by intent: a plan's structured message; a chat reply the graph wrote,
or, when it wrote none, one the conversation module writes now; a pipeline's reply, or
one Voice of Jarvis streams now. Both late replies get the turn's context note and
history, exactly as the graph's own calls do.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from app.modules import conversation
from app.schemas.chat_stream import ChatStreamRequest, SseEncoder, SseEvent, TokenFrame
from app.services.analytical import voice_of_jarvis

_CHAT_INTENTS = ("CHAT", "GREETING", "GENERAL_QA")
_STANDING_BY = "Standing by, sir."


def text_frame(event: SseEvent, text: str) -> str:
    """A ``thinking`` or ``message`` frame carrying ``text``."""
    return SseEncoder.frame(event, TokenFrame(token=text))


@dataclass
class ReplyText:
    """The reply and its reasoning as they reached the wire, and how fast."""

    turn_start: float
    gen_start: float | None = None
    ttft: float | None = None
    token_count: int = 0
    thinking: str = ""
    message: str = ""
    tokens_streamed: bool = False
    thinking_streamed: bool = False
    # How the reply was made when no model call is credited with it.
    model: str | None = None

    def streamed(self, evt_type: str, token: str) -> None:
        """Count one token that came through the progress bridge."""
        if self.gen_start is None:
            self.gen_start = time.monotonic()
            self.ttft = (self.gen_start - self.turn_start) * 1000
        self.token_count += 1
        if evt_type == "token":
            self.tokens_streamed = True
            self.message += token
        else:
            self.thinking_streamed = True
            self.thinking += token

    def said(self, message: str, thinking: str | None = None) -> list[str]:
        """The frames of a reply sent whole: its reasoning (when given), then the message."""
        frames = []
        if thinking:
            frames.append(text_frame(SseEvent.THINKING, thinking))
            self.thinking = thinking
        frames.append(text_frame(SseEvent.MESSAGE, message))
        self.message = message
        self.token_count = 1
        return frames


async def reply_frames(
    final_state: dict,
    text: ReplyText,
    *,
    intent: str,
    has_schedule: bool,
    request: ChatStreamRequest,
    initial_state: dict,
) -> AsyncIterator[str]:
    """The reply's frames, unless the graph already streamed it."""
    if text.gen_start is None:
        # Nothing came through the bridge: time the emission window only.
        text.gen_start = time.monotonic()
    if text.tokens_streamed:
        return  # re-emitting the assembled message would duplicate it

    pre_msg = final_state.get("response_message", "")
    pre_think = final_state.get("thinking_process")
    keep_think = pre_think if not text.thinking_streamed else None
    if has_schedule and intent == "PLAN_DAY":
        # A plan's message is pre-computed; no Voice of Jarvis needed.
        for frame in text.said(final_state.get("response_message") or "Here's your schedule.",
                               keep_think):
            yield frame
        text.model = "none (structured response)"
    elif intent in _CHAT_INTENTS:
        if pre_msg:
            for frame in text.said(pre_msg, keep_think):
                yield frame
        else:
            # The graph wrote no chat reply: the conversation module writes one now (no
            # hardcoded text). The note and history come from the turn's input:
            # final_state holds node outputs only, and neither is one.
            out = await conversation.run_general_chat({
                "user_message": request.user_prompt,
                "user_model": final_state.get("user_model"),
                "memory_context": initial_state.get("memory_context", ""),
                "conversation_history": initial_state.get("conversation_history", []),
            })
            for frame in text.said(out.get("response_message") or _STANDING_BY,
                                   out.get("thinking_process")):
                yield frame
    elif pre_msg and pre_msg not in ("", _STANDING_BY):
        # A pipeline intent whose reply synthesize_response already wrote.
        for frame in text.said(pre_msg, keep_think):
            yield frame
    else:
        async for frame in _synthesized(final_state, text, intent, request, initial_state):
            yield frame


async def _synthesized(
    final_state: dict, text: ReplyText, intent: str, request: ChatStreamRequest,
    initial_state: dict,
) -> AsyncIterator[str]:
    """A pipeline reply Voice of Jarvis streams now, through the same bridge as the
    orchestrator's synthesis, so the two cannot drift."""
    try:
        summary = voice_of_jarvis.build_summary_from_state(final_state, intent)
        summary["user_prompt"] = request.user_prompt
        # From the turn's input, like the chat fallback.
        summary["memory_context"] = initial_state.get("memory_context", "")
        async for event_type, tok in voice_of_jarvis.synthesize_jarvis_response_stream(
            summary, conversation_history=initial_state.get("conversation_history"),
        ):
            if text.ttft is None:
                text.ttft = (time.monotonic() - text.turn_start) * 1000
            text.token_count += 1
            if event_type == "thinking":
                text.thinking += tok
            else:
                text.message += tok
            yield text_frame(SseEvent(event_type), tok)
    except Exception as voj_exc:
        print(f"[chat/stream] VoJ streaming failed: {voj_exc}")
        for frame in text.said(final_state.get("response_message") or _STANDING_BY):
            yield frame


@dataclass
class FinalReply:
    """The reply as it is saved and shown."""

    message: str
    thinking: str
    draft_id: str | None
    # The handed-over draft's version, when there is one: what the review card accepts with.
    draft_version: str | None
    total_s: float


def final_reply(
    final_state: dict, text: ReplyText, *, has_schedule: bool, answering_model: Callable[[], str]
) -> FinalReply:
    """Credit the reply, time its generation window and strip the think tags."""
    if text.model is None:
        text.model = answering_model()
    # Generation window, not turn duration: tok/sec describes the model's output rate.
    total_s = time.monotonic() - (text.gen_start or text.turn_start)
    message = re.sub(r"</?think>", "", text.message, flags=re.IGNORECASE).strip()
    thinking = re.sub(r"</?think>", "", text.thinking, flags=re.IGNORECASE).strip()
    if not thinking and message:  # no separate reasoning: try a <think> block
        message, extracted = voice_of_jarvis.extract_thinking_process(message)
        thinking = extracted or ""
    return FinalReply(
        message=message or final_state.get("response_message") or _STANDING_BY,
        thinking=thinking,
        # A draft id belongs to the turn that proposed it: only a turn that produced a
        # schedule did. Any other turn would re-announce the thread's draft as new.
        draft_id=final_state.get("draft_id") if has_schedule else None,
        draft_version=(
            final_state.get("draft_version")
            if has_schedule and final_state.get("draft_id") else None
        ),
        total_s=total_s,
    )


def build_complete_payload(
    final_state: dict,
    text: ReplyText,
    reply: FinalReply,
    *,
    intent: str,
    has_schedule: bool,
    session_id: str,
    message_id: str | None,
    memories: list[dict],
    since_last_spoke: str | None,
    insights: list[dict] | None,
) -> dict[str, Any]:
    """The ``complete`` frame's data (``ChatCompletePayload``): the reply, the plan or why
    it did not fit, a re-plan hint, what the model was given and how the reply was made."""
    infeasible = final_state.get("error") == "INFEASIBLE_EXHAUSTED"
    replan = final_state.get("saved_constraints") or final_state.get("anti_guilt_message")
    payload = {
        "intent": intent,
        "message": reply.message,
        "schedule": final_state.get("schedule"),
        "execution_graph": final_state.get("execution_graph"),
        "ingestion_result": final_state.get("ingestion_result"),
        "action_proposals": None,
        "search_result": None,
        "suggested_action": "replan" if replan else None,
        "thinking_process": reply.thinking or final_state.get("thinking_process"),
        "schedule_status": "draft" if has_schedule else ("infeasible" if infeasible else None),
        "conflict_summary": final_state.get("clarification_request") if infeasible else None,
        "draft_id": reply.draft_id,
        "saved_constraints": final_state.get("saved_constraints"),
        "conversation_id": session_id,
        "message_id": message_id,
        "clarification_options": None,
        # {memory_id, memory_type, content, confidence, provenance}: what the model was
        # given about the user this turn.
        "memories": list(memories) or None,
        "since_last_spoke": since_last_spoke,
        "pearl_insights": insights,
        "generation_metrics": {
            "total_tokens": text.token_count,
            "total_time_s": round(reply.total_s, 2),
            "tok_per_sec": round(text.token_count / reply.total_s, 2) if reply.total_s > 0 else 0,
            "ttft_ms": round(text.ttft, 1) if text.ttft is not None else None,
            "model": text.model,
        },
    }
    if reply.draft_version:  # only a turn that hands over a draft carries its version
        payload["draft_version"] = reply.draft_version
    return payload
