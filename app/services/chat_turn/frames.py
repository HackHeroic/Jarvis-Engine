"""The frames a graph run sends as its nodes finish, and the stream's error frame."""

from typing import Any

from app.schemas.chat_stream import (
    ConsentRequestFrame,
    PhaseFrame,
    SseEncoder,
    SseEvent,
    StepFrame,
)
from app.schemas.errors import ErrorBody, ErrorResponse
from app.utils import spinner_verbs

NODE_TO_PHASE = {
    "load_context": "loading_context",
    "extract_brain_dump": "brain_dump_extraction",
    "classify_intent": "intent_classified",
    "planning_module": "planning",
    "research_agent": "researching",
    "coach_module": "coaching",
    "knowledge_module": "ingesting",
    "conversation_module": "responding",
    "synthesize_response": "synthesizing",
    "observation_loop": "learning",
}
_MODULE_NODES = ("planning_module", "research_agent", "coach_module", "knowledge_module",
                 "conversation_module")


def intent_str(intent: Any) -> str:
    """Safely convert IntentType enum or string to plain string."""
    return intent.value if hasattr(intent, "value") else str(intent)


def error_frame(request_id: str, code: str, message: str, details: dict | None = None) -> str:
    """The error envelope as the stream's ``error`` frame: the 200 has already gone out."""
    body = ErrorBody(code=code, message=message, details=details, request_id=request_id)
    return SseEncoder.frame(SseEvent.ERROR, ErrorResponse(error=body))


def node_frames(node_name: str, node_state: dict, *, memories: int, turns: int) -> list[str]:
    """What a finished orchestrator node sends: its phase, the intent, a consent request."""
    frames = []
    phase = NODE_TO_PHASE.get(node_name)
    if phase:
        phase_detail: dict[str, Any] = {
            "phase": phase, "verb": spinner_verbs.get_spinner_verb(phase)
        }
        if node_name == "load_context":
            phase_detail["detail"] = {"memories_count": memories, "conversation_turns": turns}
        elif node_name == "classify_intent" and node_state.get("intent"):
            phase_detail["detail"] = {
                "intent": intent_str(node_state["intent"]), "method": "rule-based"
            }
        elif node_name in _MODULE_NODES:
            phase_detail["detail"] = {"module": node_name}
        elif node_name == "observation_loop":
            phase_detail["detail"] = {
                "memories_extracted": node_state.get("memories_extracted_count", 0),
                "patterns_detected": node_state.get("patterns_detected_count", 0),
            }
        frames.append(SseEncoder.frame(SseEvent.PHASE, PhaseFrame.model_validate(phase_detail)))
    if node_name == "classify_intent" and node_state.get("intent"):
        frames.append(SseEncoder.frame(SseEvent.STEP, StepFrame(
            intent=intent_str(node_state["intent"]), stage="intent_classified"
        )))
    if node_state.get("needs_consent"):  # a hook asked for consent
        frames.append(SseEncoder.frame(SseEvent.CONSENT_REQUEST, ConsentRequestFrame(
            reason=node_state.get("response_message") or "", module=node_name
        )))
    return frames
