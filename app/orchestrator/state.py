"""Jarvis orchestrator state types for LangGraph."""

from enum import Enum
from typing import Any, Optional, TypedDict

from app.schemas.context import (
    BrainDumpExtraction,
    IntentType,
)

# ExecutionGraph lives in the reasoning endpoint module.
# We use Any here to avoid importing from the API layer into the orchestrator.
# The type annotation is preserved as a comment for documentation.
ExecutionGraphType = Any  # app.api.v1.endpoints.reasoning.ExecutionGraph


class ConversationPhase(str, Enum):
    GREETING = "greeting"
    PLANNING = "planning"
    NEGOTIATION = "negotiation"
    REVIEW = "review"
    CHAT = "chat"


class NegotiationPhase(str, Enum):
    NONE = "none"
    PROPOSED = "proposed"
    REVIEWING = "reviewing"
    EDITING = "editing"
    ACCEPTED = "accepted"


class JarvisState(TypedDict):
    """State that flows through the LangGraph orchestrator.

    Working Memory tier — exists only per-turn, in-memory.
    """

    # Serializable identity — the checkpointer persists this, never the facade
    user_id: str

    # User Model (lazy facade, loaded once per session)
    # NOT serializable: dropped by the checkpointer, rehydrated from user_id
    # by _load_context on resumed turns.
    user_model: Any  # UserModel — Any to avoid circular import

    # Current turn
    user_message: str
    file_base64: Optional[str]
    file_media_type: Optional[str]
    file_name: Optional[str]
    brain_dump: Optional[BrainDumpExtraction]
    intent: Optional[IntentType]
    initiated_by: str  # "user" | "system" | "pearl"

    # Module outputs
    execution_graph: Optional[Any]  # ExecutionGraph — Any to avoid cross-layer import
    schedule: Optional[dict]
    draft_id: Optional[str]
    draft_response: Optional[dict]
    research_results: Optional[list[dict]]
    ingestion_result: Optional[dict]
    clarification_request: Optional[str]

    # Response
    thinking_process: Optional[str]
    response_message: Optional[str]
    saved_constraints: Optional[list[str]]  # habits persisted by store_constraint

    # Orchestrator control
    conversation_phase: ConversationPhase
    negotiation_state: NegotiationPhase
    modules_invoked: list[str]
    needs_followup: bool
    needs_consent: Optional[bool]
    error: Optional[str]

    # Conversation context — prior messages + archival memory for multi-turn coherence
    conversation_history: Optional[list[dict]]
    memory_context: Optional[str]

    # Progress bridge — callable emitting SSE phase events from sub-graphs
    progress_callback: Any
    progress_queue: Any

    # Live Supabase-backed DraftStore from app.state — transient like
    # user_model, re-supplied per turn by chat.py, never checkpointed.
    draft_store: Any

    # Per-turn flags
    trivial_input: Optional[bool]            # set when greeting/emotional fast-path fires
    force_cloud_request: Optional[bool]      # set when frontend model picker chose Gemini
