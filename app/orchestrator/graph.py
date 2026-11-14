"""Main LangGraph StateGraph — the Jarvis orchestrator.

Every chat turn (``POST /api/v1/chat/stream``) runs this graph: load the user's
context, read the message, classify the intent, run the module that serves it,
synthesize the reply, then learn from the turn.
"""

import asyncio
import json
import re
from typing import Any, Callable

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.domain.errors import (
    ConflictError,
    DraftOutdatedError,
    EmbeddingUnavailableError,
    InvalidStateTransition,
    LLMUnavailableError,
    NotFoundError,
    ScheduleInfeasibleError,
)

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
# ACCEPT_DRAFT is the only intent in v2 that writes to user_tasks, and
# accepting *replaces every pending row* with the draft's tasks. A false
# positive therefore destroys the user's task list; false negatives merely cost
# a re-plan. The matcher is tuned hard in that direction.
#
# Only consulted while a draft is actually on the table (see _classify_intent /
# _negotiation_precheck). Outside a negotiation "accept" is just a word.

# One acceptance phrase, with its optional object ("accept" / "accept it" /
# "accept the draft"). Word-bounded, so "acceptance criteria" is not "accept".
_ACCEPT_PHRASE_RE = re.compile(
    r"\b(accept|approved?|confirm(ed)?|lgtm|ship it|do it|go ahead|"
    r"looks good|sounds good|lock it in|locked in|good to go|"
    r"that works|works for me)\b"
    r"(\s+(it|that|this|them|all))?"
    r"(\s+the)?(\s+(draft|plan|schedule|tasks))?",
    re.IGNORECASE,
)

# Leading/trailing conversational padding, stripped before the whole-message
# test so "yes, accept it please" reduces to "accept it". Interior words are
# left alone — dropping those is how a matcher starts seeing verbs that
# aren't there.
_LEAD_FILLER = frozenset(
    "yes yeah yep yup ok okay sure alright right great perfect cool nice "
    "and then so well just please".split()
)
_TRAIL_FILLER = frozenset("please sir thanks thank you now then".split())
_PUNCT_RE = re.compile(r"[^\w\s']+")

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
# "don't do it" contains "do it"; "don't scrap it" contains "scrap". Both
# ACCEPT and REJECT are destructive — one replaces every pending task, the
# other throws the draft away — so a negated message is disqualified from both
# and falls through to the branches that only ever ask a question.
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


def _normalize_reply(text: str) -> str:
    """Lowercase, drop punctuation, strip leading/trailing conversational filler."""
    words = _PUNCT_RE.sub(" ", text.lower()).split()
    while words and words[0] in _LEAD_FILLER:
        words.pop(0)
    while words and words[-1] in _TRAIL_FILLER:
        words.pop()
    return " ".join(words)


def _is_acceptance(text: str, normalized: str) -> bool:
    """True only for a message whose whole point is "yes, commit this".

    The rule is *end-to-end coverage*, not presence: every word of the
    normalized message must be consumed by acceptance phrases. Length alone is
    not a proxy for intent — "accept my apology", "my calculus grade looks
    good" and "the professor approved my topic" are all short, and none of them
    is an instruction. Consuming the whole string separates
    "looks good, lock it in" (two phrases, nothing left over) from "looks good
    but move the calculus block later" (leftovers, so not an acceptance).

    Two cheap disqualifiers run first: a negation anywhere, and a question mark
    — "can you confirm what's at 3pm?" is a lookup, not an approval.
    """
    if not normalized or _DRAFT_NEGATION_RE.search(text) or "?" in text:
        return False

    position = 0
    matched = False
    while position < len(normalized):
        match = _ACCEPT_PHRASE_RE.match(normalized, position)
        if match is None or match.end() == position:
            return False
        matched = True
        position = match.end()
        while position < len(normalized) and normalized[position] == " ":
            position += 1
    return matched


def _match_draft_intent(text: str):
    """Classify a reply aimed at a draft, or ``None`` if it isn't one.

    Order is deliberate: accept and reject are terminal, so they are tested
    first — but both are also destructive, so both are gated on the message
    being an actual instruction rather than prose that merely contains the
    verb. Rearrange and edit come last and only ever ask a follow-up question,
    which is why they can afford far looser patterns.
    """
    from app.schemas.context import IntentType

    if not text or not text.strip():
        return None
    normalized = _normalize_reply(text)
    if _is_acceptance(text, normalized):
        return IntentType.ACCEPT_DRAFT
    if _DRAFT_REJECT_RE.search(text) and not _DRAFT_NEGATION_RE.search(text):
        return IntentType.REJECT_DRAFT
    if _DRAFT_REARRANGE_RE.search(text):
        return IntentType.REARRANGE
    if _DRAFT_EDIT_RE.search(text):
        return IntentType.EDIT_TASK
    return None


def is_trivial_input(text: str) -> bool:
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
    """Build the turn's ``UserModel`` facade when the turn arrived without one.

    A live turn arrives with the facade ``ChatTurnService`` built, and this is a no-op.
    A turn with only the serializable ``user_id`` (the facade is never checkpointed)
    gets one over the planning ports of the composition root in its state
    (``UserModel.for_planning``). Without a user_id there is no identity to build from,
    and without a container there are no ports to read through: the state is left as
    it is, and every node that reads the user degrades as it does with no facade.
    """
    if state.get("user_model") is not None:
        return {}

    user_id = state.get("user_id")
    if not user_id:
        return {}

    container = state.get("container")
    if container is None:
        logger.debug("load_context: no container in the turn's state, so no user model")
        return {}

    from app.core.user_model import UserModel

    return {"user_model": UserModel.for_planning(user_id, container.planning)}


# The extraction is shown at most this many of the user's memories (the newest), each
# under a handle it can cite to say a memory in the message replaces it.
KNOWN_MEMORY_LIMIT = 15
# Reading them is one query on the reply's critical path: past this, extract without them.
KNOWN_MEMORIES_TIMEOUT_S = 1.0


async def _known_memories(
    user_id: str | None, snapshot: Any = None, container: Any = None
) -> list:
    """The user's newest active memories, oldest first; none without a composition root.

    From the turn's ``snapshot`` (``MemorySnapshot``) when it has one: no read of its
    own. Otherwise through the turn's ``container``; a slow or failing read gives none:
    the turn is extracted without them.
    """
    from app.domain.clock import utc_now

    if snapshot is not None:
        memories = snapshot.in_use(utc_now())
        return [m for m in memories if m.id and m.content.strip()][-KNOWN_MEMORY_LIMIT:]
    if container is None or not user_id:
        return []
    try:
        memories = await asyncio.wait_for(
            container.memory_service.list_active(user_id), timeout=KNOWN_MEMORIES_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning("Known memories took too long to read; extracting without them")
        return []
    except Exception as e:
        logger.warning(f"Known memories could not be read ({type(e).__name__}); extracting without them")
        return []
    return [m for m in memories if m.id and m.content.strip()][-KNOWN_MEMORY_LIMIT:]


def _extraction_prompt(user_msg: str, memories: list) -> tuple[str, dict[str, str]]:
    """The extraction's user prompt, and the handle -> memory id map it shows.

    The known memories are volatile, so they ride in the user prompt (never the
    byte-stable system prompt): ``[m1] (type) content`` lines, then ``Message:`` and the
    message. With none known the prompt is the message alone.
    """
    if not memories:
        return user_msg, {}
    handles: dict[str, str] = {}
    lines = []
    for number, memory in enumerate(memories, start=1):
        handle = f"m{number}"
        handles[handle] = memory.id
        lines.append(f"[{handle}] ({memory.memory_type}) {' '.join(memory.content.split())}")
    return "Known memories:\n" + "\n".join(lines) + "\n\nMessage:\n" + user_msg, handles


async def _extract_brain_dump(state: JarvisState) -> dict:
    """Extract planning goal, habits, action items and memories from the user message.

    One model call: the lasting facts the user states about themselves come back in
    ``memories`` (the observation loop remembers them), citing the handles of the known
    memories listed in the prompt. Trivial inputs (greetings, acks) skip the LLM
    entirely → routes to CHAT. Avoids 26B over-extraction when E4B isn't loaded.
    """
    from app.schemas.context import BrainDumpExtraction
    from app.services.planning.brain_dump_prompt import BRAIN_DUMP_EXTRACTION_PROMPT
    from app.core.model_router import route_llm_call

    user_msg = state.get("user_message", "")

    # Greeting / ack fast path — no LLM call, returns null brain_dump → CHAT intent
    if is_trivial_input(user_msg):
        return {"brain_dump": None, "trivial_input": True}

    # Decided fresh every turn: the checkpointer persists this flag, and a
    # stale True from an earlier greeting would suppress memory extraction
    # (observation.py) on a real turn.
    if not user_msg.strip():
        return {"brain_dump": None, "trivial_input": False}

    known = await _known_memories(
        state.get("user_id"), state.get("memory_snapshot"), state.get("container")
    )
    prompt, handles = _extraction_prompt(user_msg, known)
    try:
        result = await route_llm_call(
            task="brain_dump_extraction",
            prompt=prompt,
            system_prompt=BRAIN_DUMP_EXTRACTION_PROMPT,
            response_schema=BrainDumpExtraction,
            conversation_history=state.get("conversation_history"),
        )
        if isinstance(result, BrainDumpExtraction):
            return {"brain_dump": result, "trivial_input": False, "memory_handles": handles}
        if isinstance(result, dict):
            return {
                "brain_dump": BrainDumpExtraction.model_validate(result),
                "trivial_input": False,
                "memory_handles": handles,
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

    # A second "looks good" after this thread's draft was accepted is about that
    # same draft. It goes back to draft_action, which finds the draft closed and
    # says it is already locked in — writing nothing. Only with the accepted
    # draft's id in hand: without one, draft_action would fall back to the
    # user's newest pending draft, which may belong to another conversation.
    if (
        state.get("negotiation_state") == NegotiationPhase.ACCEPTED
        and state.get("draft_id")
        and _match_draft_intent(state.get("user_message") or "") == IntentType.ACCEPT_DRAFT
    ):
        return {"intent": IntentType.ACCEPT_DRAFT}

    bd = state.get("brain_dump")
    if not bd:
        return {"intent": IntentType.CHAT}

    # Rule-based classification: no model call
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

    The file-upload guard is repeated from ``_classify_intent`` rather than
    inherited, because this node *is* the live path during a negotiation and
    that function never runs: an upload captioned "confirm this" would
    otherwise accept the draft and drop the document on the floor.
    """
    from app.schemas.context import IntentType

    if state.get("file_base64"):
        return {"intent": IntentType.KNOWLEDGE_INGESTION}

    intent = _match_draft_intent(state.get("user_message") or "")
    return {"intent": intent if intent is not None else IntentType.PLAN_DAY}


async def _store_rule(state: JarvisState, habit: str) -> dict:
    """Store one stated habit as a rule: ``{"status": "stored", "id": ...}`` or not stored.

    Through the ``ConstraintRepository`` of the composition root in the turn's state
    (the rule's id is known at once, for linking it to the memory of stating it).
    Without a container (tests, scripts) nothing is stored, and the turn says so.
    """
    from app.services.extraction import behavioral_store

    container = state.get("container")
    user_id = state.get("user_id")
    if container is None or not user_id:
        logger.debug("store_constraint: no container in the turn's state, so no rule is stored")
        return {"status": "skipped"}
    return await behavioral_store.store_behavioral_constraint(
        container.repos.constraints, habit, user_id=user_id
    )


async def _not_let_go(state: JarvisState, habits: list[str]) -> list[str] | None:
    """``habits`` less each one the user let go of: said again in words close to a memory
    they forgot or dismissed (``MemoryService.drop_let_go``, the test ``remember`` uses).

    Forgetting the memory of a rule deleted the rule; saying it again must not bring it
    back. None when that cannot be checked (the embedder is down while the user has let
    something go): then no rule is stored rather than one they may have forgotten.
    Without a composition root nothing is stored anyway (``_store_rule``).
    """
    container = state.get("container")
    user_id = state.get("user_id")
    if container is None or not user_id:
        return habits
    try:
        return await container.memory_service.drop_let_go(user_id, habits)
    except EmbeddingUnavailableError:
        logger.warning(
            "store_constraint: the rules could not be checked against what the user let go "
            "of (embedder unavailable), so none is stored"
        )
        return None


async def store_constraint(state: JarvisState) -> dict:
    """Persist the turn's inline habits as behavioral constraints.

    BEHAVIORAL_CONSTRAINT is classified from ``brain_dump.inline_habits`` — the
    extraction already carries the verbatim habit phrases, and this node stores each as
    a rule. A habit the user let go of (its memory forgotten or dismissed) is not stored
    again, and nothing is said about it: like the suppressed memory, it is simply not
    kept. Deterministic confirmation, no LLM call.
    """
    bd = state.get("brain_dump")
    habits = list(getattr(bd, "inline_habits", None) or [])
    if not habits:
        return {
            "saved_constraints": [],
            "response_message": (
                "I heard a preference in there, sir, but couldn't pin down the "
                "rule. Could you phrase it as one habit — e.g. 'no study before 11am'?"
            ),
        }
    kept = await _not_let_go(state, habits)
    if kept is not None and not kept:  # every one was let go of: nothing to keep
        return {"saved_constraints": [], "response_message": "Understood, sir."}
    habits = kept or []

    saved: list[str] = []
    # Each stored rule's id, by its words: the observation loop links the rule to the
    # memory of stating it, so the two are let go of together.
    rule_ids: dict[str, str] = {}
    for habit in habits:
        try:
            result = await _store_rule(state, habit)
        except Exception as e:  # storage must never crash the turn
            logger.warning(f"store_constraint failed ({type(e).__name__})")
            result = {"status": "error"}
        if result.get("status") == "stored":
            saved.append(habit)
            if result.get("id"):
                rule_ids[habit] = str(result["id"])

    if not saved:
        return {
            "saved_constraints": [],
            "response_message": (
                "I couldn't save that constraint just now, sir — the strategy "
                "hub didn't take the write. Worth trying again in a moment."
            ),
        }
    listed = "; ".join(f"“{h}”" for h in saved)
    return {
        "saved_constraints": saved,
        "saved_rule_ids": rule_ids,
        "response_message": (
            f"Noted and locked in, sir: {listed}. "
            "Every schedule I draft from here on will respect it."
        ),
    }


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
        MOVED_ON_ACCEPT,
        accept_draft_on_time,
        reject_draft,
        resolve_draft,
    )

    user_id = state.get("user_id") or "demo"
    intent = state.get("intent")
    intent_value = getattr(intent, "value", intent)
    container = state.get("container")

    if container is None:  # no composition root (unit tests, scripts): no drafts to act on
        logger.debug("draft_action: no container in the turn's state, so no draft system")
        return {
            "response_message": "The draft system is offline at the moment, sir. Nothing to accept.",
            "negotiation_state": NegotiationPhase.NONE,
            "draft_id": None,
        }
    drafts, planner = container.draft_service, container.planner

    draft = await resolve_draft(drafts, user_id, state.get("draft_id"))
    if not draft:
        return {
            "response_message": "There's no draft awaiting review, sir. Tell me what to plan.",
            "negotiation_state": NegotiationPhase.NONE,
            "draft_id": None,
        }

    draft_id = draft.id

    if intent_value == IntentType.ACCEPT_DRAFT.value:
        try:
            outcome = await accept_draft_on_time(drafts, planner, user_id, draft)
        except InvalidStateTransition:
            return await _closed_draft_reply(drafts, user_id, draft_id)
        except (ScheduleInfeasibleError, LLMUnavailableError) as exc:
            # Some times had gone by and the rest no longer fits around now, or the
            # user's habits could not be read to fit them again: nothing was accepted
            # and the draft is still open, so the negotiation stays open too.
            return {"response_message": exc.message, "draft_id": draft_id}
        except DraftOutdatedError as exc:  # every slot has gone by: say so, plainly
            return {
                "response_message": exc.message,
                "negotiation_state": NegotiationPhase.NONE,
                "draft_id": None,
            }
        except ConflictError:
            # The draft reuses a task id the user's finished tasks hold. No retry
            # can write it, so close the negotiation instead of inviting one.
            return {
                "response_message": (
                    "That plan clashes with tasks you already have, sir, so nothing was "
                    "written. Tell me what to plan and I'll build a fresh one."
                ),
                "negotiation_state": NegotiationPhase.NONE,
                "draft_id": None,
            }
        count = outcome.written if outcome is not None else None
        if count is None:
            # The write did not land and the draft is still pending. Keep the
            # phase (omitted, not reset) so "accept" retries — and say what
            # actually happened, because "Locked in" on an empty schedule is
            # the failure the user discovers tomorrow morning.
            return {
                "response_message": (
                    "The schedule didn't commit, sir — nothing was written. "
                    "Say accept again and I'll retry."
                )
            }
        if count == 0:
            return {
                "response_message": (
                    "That draft holds nothing I can schedule, sir. "
                    "Tell me what to plan and I'll build a fresh one."
                ),
                "negotiation_state": NegotiationPhase.NONE,
                "draft_id": None,
            }
        on_schedule = (
            f"{count} task is on your schedule." if count == 1
            else f"{count} tasks are on your schedule."
        )
        return {
            "response_message": (
                f"{MOVED_ON_ACCEPT} {on_schedule}" if outcome.moved
                else f"Locked in, sir — {on_schedule}"
            ),
            "negotiation_state": NegotiationPhase.ACCEPTED,
            # Kept, not cleared: a repeated "looks good" on this thread must find
            # this draft (closed) rather than the user's newest pending one.
            "draft_id": draft_id,
        }

    if intent_value == IntentType.REJECT_DRAFT.value:
        # The rejection message is the reason — worth keeping, it is the signal
        # PEARL learns "don't propose this again" from. A draft already closed
        # (accepted from the review UI while this thread still says REVIEWING)
        # is not discarded, and the reply must not say it was.
        try:
            await reject_draft(drafts, user_id, draft_id, state.get("user_message"))
        except (InvalidStateTransition, NotFoundError):
            return await _closed_draft_reply(drafts, user_id, draft_id, attempted="discard")
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


async def _closed_draft_reply(
    drafts: Any, user_id: str, draft_id: str | None, attempted: str = "apply"
) -> dict:
    """Answer an accept (or a reject) that found the draft closed. Nothing changed this turn.

    Usually it was accepted already: from the review UI, or by a double-click.
    It may instead have expired or been rejected, and then none of its tasks
    were ever written, so "locked in" would be false. Re-read the draft (through
    ``drafts``, the ``DraftService``) rather than trust the copy from before the
    action: a concurrent accept may have closed it since. Either way the negotiation
    ends here.
    """
    from app.domain.entities import DraftStatus

    try:
        current = await drafts.get(user_id, draft_id) if draft_id else None
    except NotFoundError:
        current = None
    if current is not None and current.status is DraftStatus.ACCEPTED:
        return {
            "response_message": (
                "That plan is already locked in, sir."
                if attempted == "apply"
                else "That plan is already locked in, sir, so there was nothing to discard."
            ),
            "negotiation_state": NegotiationPhase.ACCEPTED,
            "draft_id": draft_id,  # so the next "looks good" finds it again
        }
    return {
        "response_message": (
            f"That draft has closed, sir, so I didn't {attempted} it. "
            "Tell me what to plan and I'll build a fresh one."
        ),
        "negotiation_state": NegotiationPhase.NONE,
        "draft_id": None,
    }


def make_token_emitter(progress_queue: Any) -> Callable[..., None]:
    """Bridge: synthesis streams tokens → SSE via the progress queue.

    The returned callable is a no-op when no queue is wired (plain ``/chat``,
    unit tests), so nodes can call it unconditionally. ``event_type`` picks the
    SSE channel the endpoint routes the token to: ``token`` → ``event: message``,
    ``thinking_token`` → ``event: thinking``.
    """

    def _emit(token: str, event_type: str = "token") -> None:
        if progress_queue is None or not token:
            return
        progress_queue.put_nowait(
            json.dumps({"_event_type": event_type, "token": token})
        )

    return _emit


def build_jarvis_graph(checkpointer=None):
    """Build and compile the Jarvis orchestrator graph."""
    graph = StateGraph(JarvisState)

    # LLM-powered nodes
    graph.add_node("load_context", _load_context)
    graph.add_node("extract_brain_dump", _extract_brain_dump)
    graph.add_node("classify_intent", _classify_intent)
    graph.add_node("negotiation_precheck", _negotiation_precheck)
    graph.add_node("draft_action", handle_draft_action)
    graph.add_node("store_constraint", store_constraint)

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
        {
            "draft_action": "draft_action",
            "planning_module": "planning_module",
            "knowledge_module": "knowledge_module",
        },
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
            "store_constraint": "store_constraint",
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
    graph.add_edge("store_constraint", "observation_loop")

    graph.add_conditional_edges(
        "observation_loop",
        check_needs_followup,
        {"continue": "classify_intent", "done": END},
    )

    return graph.compile(checkpointer=checkpointer)
