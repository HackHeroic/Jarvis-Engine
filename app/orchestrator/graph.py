"""Main LangGraph StateGraph — the Jarvis orchestrator.

Replaces execute_agentic_flow() in control_policy.py.
"""

import asyncio
import re

from langgraph.graph import END, StateGraph

from app.orchestrator.state import JarvisState, NegotiationPhase
from app.orchestrator.routing import (
    check_needs_followup,
    check_negotiation_shortcut,
    route_draft_action,
    route_to_module,
)
from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis
from app.modules.coach import run_coaching_response
from app.core.observation import run_observation_loop
from app.modules import module_registry
from app.orchestrator.module_wrapper import create_module_wrapper

# Trivial inputs that should ALWAYS be classified as CHAT — bypass 26B brain-dump extraction
# (which over-extracts greetings into search_queries / action_items when E4B isn't loaded).
_TRIVIAL_INPUT_RE = re.compile(
    r"^\s*(hi+|hey+|hello+|yo+|sup|hola|namaste|hmm+|huh|aha|ah|oh|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"how\s+(are\s+you|r\s+u|you\s+doing)|"
    r"what'?s\s+up|wassup|"
    r"thanks?|thank\s+you|ty|cheers|"
    r"ok+|okay|cool|nice|great|alright|"
    r"bye|goodbye|see\s+ya|cya"
    r")[\s!.?]*$",
    re.IGNORECASE,
)

# Emotional disclosure — the LLM should respond with empathy via the conversation
# module, NOT extract this as a behavioral habit constraint.
_EMOTIONAL_INPUT_RE = re.compile(
    r"\b(i\s*(am|'m|m)\s+|i\s+(feel|felt|feeling)\s+|"
    r"feeling\s+|i\s+got\s+|today\s+i'?m?\s+)"
    r"(sad|happy|anxious|tired|exhausted|overwhelmed|stressed|angry|frustrated|"
    r"depressed|low|down|lonely|scared|afraid|worried|nervous|hopeful|"
    r"excited|grateful|content|relaxed|calm|peaceful|blah|meh|off|fine|okay|ok)"
    r"\b",
    re.IGNORECASE,
)


# Planning / task verbs — if any of these appear, the message is NOT trivial
# even if it ALSO contains emotional language (e.g. "I'm sad, please plan my week").
# NOTE: bare "today" intentionally excluded — appears in "I am sad today"
# without any planning intent. "tomorrow" and "tonight" stay (clearer signals).
_PLANNING_VERBS_RE = re.compile(
    r"\b(plan|schedule|organize|organise|prepare|prep|study|learn|build|create|"
    r"design|write|review|finish|complete|deliver|tomorrow|tonight|"
    r"this\s+(week|month|weekend|morning|afternoon|evening)|"
    r"next\s+(week|month|day|weekend)|deadline|exam|test|interview|presentation|"
    r"contest|hackathon|assignment|homework|task|goal|project|chapter|"
    r"add\s+a\s+task|remind\s+me|create\s+a\s+task)\b",
    re.IGNORECASE,
)


# --- Draft negotiation verbs -----------------------------------------------
# Short imperative replies to a proposed schedule ("accept", "scrap that",
# "move the calculus block"). Rules beat an LLM here: the vocabulary is tiny,
# the latency budget on a one-word reply is ~0, and a misfire is expensive —
# ACCEPT_DRAFT is the only intent in v2 that writes to user_tasks.
#
# Only consulted while a draft is actually on the table (see _classify_intent /
# _negotiation_precheck). Outside a negotiation "accept" is just a word.
_DRAFT_ACCEPT_RE = re.compile(
    r"\b(accept|approve|confirm|looks good|lgtm|ship it|lock it in|"
    r"go ahead|do it|yes,?\s*(do|go|lock|please)\b)",
    re.IGNORECASE,
)
_DRAFT_REJECT_RE = re.compile(
    r"\b(reject|scrap|discard|throw (it|that) out|"
    r"cancel\s+(the\s+)?(plan|draft|schedule)|start over|no,?\s*redo)",
    re.IGNORECASE,
)
_DRAFT_REARRANGE_RE = re.compile(
    r"\b(move|shift|push|swap|rearrange|reorder|reschedule|earlier|later)\b",
    re.IGNORECASE,
)
_DRAFT_EDIT_RE = re.compile(
    r"\b(edit|change|rename|shorten|lengthen|extend|drop|remove|replace|"
    r"make .*\b(longer|shorter|easier|harder))\b",
    re.IGNORECASE,
)
# "don't do it" contains "do it"; "not yet" follows "looks good" often enough.
# ACCEPT_DRAFT is the one intent that writes user_tasks, so a negated message
# is disqualified from accepting and falls through to the safe branches.
_DRAFT_NEGATION_RE = re.compile(
    r"\b(don'?t|do\s+not|never|not\s+yet|hold\s+off|no\s+need|wait)\b",
    re.IGNORECASE,
)

# EDITING is included: a user mid-edit can still accept or scrap the whole thing.
_NEGOTIATION_ACTIVE = (
    NegotiationPhase.PROPOSED,
    NegotiationPhase.REVIEWING,
    NegotiationPhase.EDITING,
)


def _match_draft_intent(text: str):
    """Classify a reply aimed at a draft, or ``None`` if it isn't one.

    Order is deliberate: accept and reject are terminal and unambiguous, so they
    win over the far broader rearrange/edit verb sets ("cancel the plan" must
    not read as an edit because it contains no edit verb, but "move it and
    approve" should accept).
    """
    from app.schemas.context import IntentType

    if not text or not text.strip():
        return None
    if _DRAFT_ACCEPT_RE.search(text) and not _DRAFT_NEGATION_RE.search(text):
        return IntentType.ACCEPT_DRAFT
    if _DRAFT_REJECT_RE.search(text):
        return IntentType.REJECT_DRAFT
    if _DRAFT_REARRANGE_RE.search(text):
        return IntentType.REARRANGE
    if _DRAFT_EDIT_RE.search(text):
        return IntentType.EDIT_TASK
    return None


def _is_trivial_input(text: str) -> bool:
    """Return True for inputs that should bypass 26B brain-dump extraction.

    Three categories route straight to CHAT (which DOES call the LLM via
    conversation module — this never returns hardcoded text):
      1. Empty/very short messages
      2. Greetings, acks, fillers
      3. SHORT emotional disclosure with NO planning verb
         (avoids dropping legitimate work like "I'm sad, plan my week")
    """
    if not text or not text.strip():
        return True
    s = text.strip()
    if len(s) < 3:
        return True
    if _TRIVIAL_INPUT_RE.match(s):
        return True
    # Emotional fast path is only safe when there's no work intent in the message
    if _EMOTIONAL_INPUT_RE.search(s):
        if _PLANNING_VERBS_RE.search(s):
            return False  # legitimate planning request that happens to be emotional
        if len(s) <= 80:
            return True
    return False


# --- Real LLM-powered nodes (replacing stubs) ---

async def _load_context(state: JarvisState) -> dict:
    """Hydrate the UserModel facade for checkpoint-resumed turns.

    Live requests arrive with a pre-wired user_model (shared db_client /
    memory_store) and this is a no-op. A turn resumed from a checkpoint has
    only the serializable ``user_id`` — the facade was never persisted — so
    rebuild it here. Without a user_id there is no identity to rebuild from,
    so leave state untouched.

    The rebuilt facade must be *functional*, not a hollow ``db=None`` shell:
    it is truthy either way, so every downstream ``if user_model:`` guard
    would wave the hollow one through and then fail on
    ``AttributeError: 'NoneType' object has no attribute 'supabase'``. The
    shared clients come from the registry the lifespan populates.
    """
    if state.get("user_model") is not None:
        return {}

    user_id = state.get("user_id")
    if not user_id:
        return {}

    from app.core.runtime import get_db, get_memory_store
    from app.core.user_model import UserModel

    user_model = UserModel(user_id=user_id, db=get_db())
    memory_store = get_memory_store()
    if memory_store is not None:
        user_model.set_memory_store(memory_store)
    return {"user_model": user_model}


async def _extract_brain_dump(state: JarvisState) -> dict:
    """Extract planning goal, habits, action items from user message using LLM.

    Trivial inputs (greetings, acks) skip the LLM entirely → routes to CHAT.
    Avoids 26B over-extraction when E4B isn't loaded.
    """
    from app.schemas.context import BrainDumpExtraction
    from app.services.analytical.control_policy import BRAIN_DUMP_EXTRACTION_PROMPT
    from app.core.model_router import route_llm_call

    user_msg = state.get("user_message", "")

    # Greeting / ack fast path — no LLM call, returns null brain_dump → CHAT intent
    if _is_trivial_input(user_msg):
        return {"brain_dump": None, "trivial_input": True}

    # Decided fresh every turn: the checkpointer persists this flag, and a
    # stale True from an earlier greeting would suppress memory extraction
    # (observation.py) on a real turn.
    if not user_msg.strip():
        return {"brain_dump": None, "trivial_input": False}

    try:
        result = await route_llm_call(
            task="brain_dump_extraction",
            prompt=user_msg,
            system_prompt=BRAIN_DUMP_EXTRACTION_PROMPT,
            response_schema=BrainDumpExtraction,
            conversation_history=state.get("conversation_history"),
        )
        if isinstance(result, BrainDumpExtraction):
            return {"brain_dump": result, "trivial_input": False}
        if isinstance(result, dict):
            return {
                "brain_dump": BrainDumpExtraction.model_validate(result),
                "trivial_input": False,
            }
        return {"brain_dump": None, "trivial_input": False}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Brain dump extraction failed: {e}")
        return {"brain_dump": None, "trivial_input": False}


async def _classify_intent(state: JarvisState) -> dict:
    """Classify intent from extracted brain dump fields. Rule-based, no LLM needed."""
    from app.schemas.context import IntentType

    # File upload → always KNOWLEDGE_INGESTION regardless of brain dump
    if state.get("file_base64"):
        return {"intent": IntentType.KNOWLEDGE_INGESTION}

    # Draft verbs, but only while a draft is actually under review. Checked
    # before the brain-dump rules: "move the calculus block to the evening"
    # extracts as a planning_goal and would otherwise re-plan from scratch.
    if state.get("negotiation_state") in _NEGOTIATION_ACTIVE:
        draft_intent = _match_draft_intent(state.get("user_message") or "")
        if draft_intent is not None:
            return {"intent": draft_intent}

    bd = state.get("brain_dump")
    if not bd:
        return {"intent": IntentType.CHAT}

    # Rule-based classification matching control_policy.py logic
    if bd.planning_goal:
        return {"intent": IntentType.PLAN_DAY}
    if bd.has_calendar:
        return {"intent": IntentType.CALENDAR_SYNC}
    if bd.has_knowledge:
        return {"intent": IntentType.KNOWLEDGE_INGESTION}
    if bd.inline_habits:
        return {"intent": IntentType.BEHAVIORAL_CONSTRAINT}
    if bd.action_items:
        return {"intent": IntentType.ACTION_ITEM}
    if bd.search_queries:
        return {"intent": IntentType.RESEARCH}
    # Check for progress-related keywords in the original message
    user_msg = (state.get("user_message") or "").lower()
    progress_keywords = ["how am i doing", "my progress", "check progress", "how's my", "mastery", "how far", "status update", "how much have i"]
    if any(kw in user_msg for kw in progress_keywords):
        return {"intent": IntentType.CHECK_PROGRESS}
    return {"intent": IntentType.CHAT}


async def _negotiation_precheck(state: JarvisState) -> dict:
    """Classify an active-negotiation turn before the planning shortcut takes it.

    ``check_negotiation_shortcut`` used to hand every turn with a live draft
    straight to planning_module, which bypasses ``_classify_intent`` entirely —
    so "accept" re-ran the whole planner and proposed *another* draft. This node
    runs the same cheap regex (no LLM, no brain-dump extraction) and lets
    ``route_draft_action`` divert only the real draft verbs.

    Anything else falls through as PLAN_DAY, which is what the shortcut always
    meant: an unrecognised reply mid-review is a re-plan request.
    """
    from app.schemas.context import IntentType

    intent = _match_draft_intent(state.get("user_message") or "")
    return {"intent": intent if intent is not None else IntentType.PLAN_DAY}


def _supabase_of(state: JarvisState):
    """The Supabase client for this turn, or None when running degraded.

    Prefers the facade wired for this request; falls back to the registry the
    lifespan populates, which is what a checkpoint-resumed turn has. Matches the
    ``getattr(user_model, "_db", None)`` access observation.py already uses.
    """
    user_model = state.get("user_model")
    db = getattr(user_model, "_db", None) if user_model is not None else None
    if db is None:
        from app.core.runtime import get_db

        db = get_db()
    return getattr(db, "supabase", None)


async def handle_draft_action(state: JarvisState) -> dict:
    """Resolve the draft under review: accept, reject, or keep editing.

    This node is the *exit door* from the negotiation phase. ``planning_state_out``
    sets ``negotiation_state = REVIEWING`` whenever a draft is created and
    nothing else ever writes ACCEPTED or NONE — so every branch here, including
    the degraded ones, must leave a terminal state. A branch that returned
    REVIEWING on failure would lock the thread into re-planning forever via
    ``check_negotiation_shortcut``.

    Accept is also the only place v2 writes to ``user_tasks``: the planning
    sub-graph proposes and persists nothing.
    """
    import logging

    try:
        return await _apply_draft_action(state)
    except Exception as exc:
        logging.getLogger(__name__).error(f"Draft action failed: {exc}")
        # Deliberately omits negotiation_state: a Supabase blip leaves the draft
        # exactly where it was, so the phase must stay put too — saying "accept"
        # again retries instead of starting a second plan from scratch. Claiming
        # success here would be the one lie this node must never tell.
        return {
            "response_message": (
                "That didn't go through, sir — the draft store refused it. "
                "Say it again and I'll retry."
            )
        }


async def _apply_draft_action(state: JarvisState) -> dict:
    """The body of handle_draft_action; see there for the contract."""
    from app.schemas.context import IntentType
    from app.services.draft_actions import (
        accept_draft_and_persist,
        draft_id_of,
        resolve_draft,
    )

    user_id = state.get("user_id") or "demo"
    intent = state.get("intent")
    intent_value = getattr(intent, "value", intent)
    draft_store = state.get("draft_store")

    if draft_store is None:
        return {
            "response_message": "The draft system is offline at the moment, sir. Nothing to accept.",
            "negotiation_state": NegotiationPhase.NONE,
            "draft_id": None,
        }

    draft = await resolve_draft(draft_store, user_id, state.get("draft_id"))
    if not draft:
        return {
            "response_message": "There's no draft awaiting review, sir. Tell me what to plan.",
            "negotiation_state": NegotiationPhase.NONE,
            "draft_id": None,
        }

    draft_id = draft_id_of(draft)

    if intent_value == IntentType.ACCEPT_DRAFT.value:
        count = await accept_draft_and_persist(
            draft_store, user_id, draft, _supabase_of(state)
        )
        return {
            "response_message": (
                f"Locked in, sir — {count} task is on your schedule."
                if count == 1
                else f"Locked in, sir — {count} tasks are on your schedule."
                if count
                else "Draft accepted, sir, though it held no tasks to schedule."
            ),
            "negotiation_state": NegotiationPhase.ACCEPTED,
            "draft_id": None,
        }

    if intent_value == IntentType.REJECT_DRAFT.value:
        # The rejection message is the reason — worth keeping, it is the signal
        # PEARL learns "don't propose this again" from.
        await asyncio.to_thread(
            draft_store.reject_draft, draft_id, user_id, state.get("user_message")
        )
        return {
            "response_message": "Draft discarded, sir. Tell me what to aim for instead.",
            "negotiation_state": NegotiationPhase.NONE,
            "draft_id": None,
        }

    # EDIT_TASK / REARRANGE — the draft stays live and the negotiation stays
    # open. Applying a free-text edit to a specific task is Spec 3; until then
    # the structured route is PATCH /drafts/{id}/tasks/{task_id} from the UI.
    return {
        "response_message": (
            "Which task should I adjust, sir? You can also accept or scrap the draft outright."
        ),
        "negotiation_state": NegotiationPhase.EDITING,
        "draft_id": draft_id,
    }


def build_jarvis_graph(checkpointer=None):
    """Build and compile the Jarvis orchestrator graph."""
    graph = StateGraph(JarvisState)

    # LLM-powered nodes
    graph.add_node("load_context", _load_context)
    graph.add_node("extract_brain_dump", _extract_brain_dump)
    graph.add_node("classify_intent", _classify_intent)
    graph.add_node("negotiation_precheck", _negotiation_precheck)
    graph.add_node("draft_action", handle_draft_action)

    # Real module nodes (from registry)
    for name in module_registry.registered_names():
        graph.add_node(name, create_module_wrapper(name, module_registry))
    graph.add_node("coach_module", run_coaching_response)
    graph.add_node("conversation_module", run_general_chat)
    graph.add_node("synthesize_response", voice_of_jarvis_synthesis)
    graph.add_node("observation_loop", run_observation_loop)

    graph.set_entry_point("load_context")

    # A live draft short-circuits brain-dump extraction, but not classification:
    # the pre-check decides between "resolve the draft" and "re-plan" with a
    # regex, so a one-word "accept" never pays for an LLM call.
    graph.add_conditional_edges(
        "load_context",
        check_negotiation_shortcut,
        {"negotiation_active": "negotiation_precheck", "normal": "extract_brain_dump"},
    )

    graph.add_conditional_edges(
        "negotiation_precheck",
        route_draft_action,
        {"draft_action": "draft_action", "planning_module": "planning_module"},
    )

    graph.add_edge("extract_brain_dump", "classify_intent")

    graph.add_conditional_edges(
        "classify_intent",
        route_to_module,
        {
            "planning_module": "planning_module",
            "research_agent": "research_agent",
            "coach_module": "coach_module",
            "knowledge_module": "knowledge_module",
            "conversation_module": "conversation_module",
            "draft_action": "draft_action",
        },
    )

    for name in module_registry.registered_names():
        graph.add_edge(name, "synthesize_response")
    graph.add_edge("coach_module", "synthesize_response")
    graph.add_edge("synthesize_response", "observation_loop")

    graph.add_edge("conversation_module", "observation_loop")

    # Skips synthesize_response on purpose: the accept/reject confirmations are
    # deterministic and already in voice, and Voice of Jarvis would overwrite
    # them with a generic re-synthesis (plus an LLM call the user didn't need).
    graph.add_edge("draft_action", "observation_loop")

    graph.add_conditional_edges(
        "observation_loop",
        check_needs_followup,
        {"continue": "classify_intent", "done": END},
    )

    return graph.compile(checkpointer=checkpointer)
