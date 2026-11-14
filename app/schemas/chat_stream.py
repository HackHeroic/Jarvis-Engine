"""The ``complete`` frame of ``POST /api/v1/chat/stream``: the turn's one typed contract.

The handler validates the frame against ``ChatCompletePayload`` before sending it, so
the contract the frontend is built against is explicit and cannot drift: the fields are
exactly the ones it reads today, none renamed.

**The vocabulary.** A record's id is named ``<kind>_id`` wherever it is referred to
(``draft_id``, ``memory_id``, ``message_id``; ``conversation_id`` is the chat session's
id, the one the frontend sends back); it is a bare ``id`` only on a list item that is
the record itself (a rule or a suggestion on the You page). Where a memory came from is
``provenance``: ``said`` (the user's own words) or ``inferred`` (Jarvis noticed it from
what happened). Work that did not happen is "set aside", never anything that blames.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities import MemorySource, MemoryType


class MemoryShown(BaseModel):
    """A memory the model was given about the user this turn."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(description="The memory's id (as in /memories).")
    memory_type: MemoryType = Field(description="What kind of thing is remembered.")
    content: str = Field(description="The memory in words.")
    confidence: float = Field(description="How sure Jarvis is that it holds, 0 to 1.")
    provenance: MemorySource = Field(
        description="'said': the user said it. 'inferred': Jarvis noticed it."
    )


class PearlInsight(BaseModel):
    """Something PEARL noticed in what happened to the user's tasks."""

    model_config = ConfigDict(extra="forbid")

    insight: str = Field(description="What Jarvis tells the user, in words without blame.")
    confidence: float = Field(description="How consistent the behaviour is, 0 to 1.")
    occurrences: int = Field(description="How many outcomes it rests on.")
    memory_id: str | None = Field(
        description="The memory that holds the pattern, to confirm or dismiss."
    )


class GenerationMetrics(BaseModel):
    """How the reply was generated."""

    model_config = ConfigDict(extra="forbid")

    total_tokens: int = Field(description="Tokens (or frames) the reply streamed.")
    total_time_s: float = Field(description="The generation window, in seconds.")
    tok_per_sec: int | float = Field(description="total_tokens over total_time_s; 0 when no time.")
    ttft_ms: float | None = Field(description="Milliseconds to the first token; null if none.")
    model: str = Field(description="The model that wrote the reply, or how it was made.")


class ChatCompletePayload(BaseModel):
    """The turn's last frame (``event: complete``): the reply and everything around it."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="The turn's classified intent (e.g. PLAN_DAY, CHAT).")
    message: str = Field(description="Jarvis's reply, as saved to the conversation.")
    schedule: dict | None = Field(
        description="The proposed schedule (horizon_start + task_id -> minutes), when the "
        "turn planned; null otherwise."
    )
    execution_graph: dict | None = Field(
        description="The decomposition behind the schedule, when the turn planned."
    )
    ingestion_result: dict | None = Field(description="What a document upload produced.")
    action_proposals: list[dict] | None = Field(
        description="Proposed action items; null on this route."
    )
    search_result: dict | None = Field(description="Web search results; null on this route.")
    suggested_action: Literal["replan"] | None = Field(
        description="'replan' when a rule was saved or work was set aside: offer a new plan."
    )
    thinking_process: str | None = Field(
        description="The model's own reasoning, when it gave any; never made up."
    )
    schedule_status: Literal["draft", "infeasible"] | None = Field(
        description="'draft': a plan awaits the user's accept (draft_id); 'infeasible': it "
        "did not fit (conflict_summary says why); null when the turn did not plan."
    )
    conflict_summary: str | None = Field(
        description="Why the plan did not fit, without blame; only when infeasible."
    )
    draft_id: str | None = Field(
        description="The draft this turn proposed (accept it at /drafts/{draft_id}/accept); "
        "null for a turn that proposed none."
    )
    saved_constraints: list[str] | None = Field(
        description="The rules this turn saved, in the user's words."
    )
    conversation_id: str | None = Field(
        description="The chat session's id: the frontend sends it back on the next turn."
    )
    message_id: str | None = Field(description="The saved reply's id.")
    clarification_options: list[str] | None = Field(
        description="Quick replies; null on this route."
    )
    memories: list[MemoryShown] | None = Field(
        description="What the model was given about the user this turn; null for none."
    )
    since_last_spoke: str | None = Field(
        description="What the model was told about the last conversation, or null."
    )
    pearl_insights: list[PearlInsight] | None = Field(
        description="What PEARL noticed and has confirmed enough to say; null for none."
    )
    generation_metrics: GenerationMetrics = Field(description="How the reply was generated.")
