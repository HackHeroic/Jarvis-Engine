from app.orchestrator.state import (
    ConversationPhase,
    NegotiationPhase,
    JarvisState,
)
from app.schemas.context import IntentType

from tests.fakes import CANNED_LLM_REPLY, FakeDBClient, FakeSupabase, make_jarvis_state


def test_conversation_phase_values():
    assert ConversationPhase.GREETING == "greeting"
    assert ConversationPhase.PLANNING == "planning"
    assert ConversationPhase.NEGOTIATION == "negotiation"
    assert ConversationPhase.REVIEW == "review"
    assert ConversationPhase.CHAT == "chat"


def test_negotiation_phase_values():
    assert NegotiationPhase.NONE == "none"
    assert NegotiationPhase.PROPOSED == "proposed"
    assert NegotiationPhase.REVIEWING == "reviewing"
    assert NegotiationPhase.EDITING == "editing"
    assert NegotiationPhase.ACCEPTED == "accepted"


def test_jarvis_state_is_typed_dict():
    """JarvisState should be a TypedDict usable by LangGraph."""
    state: JarvisState = {
        "user_model": None,
        "user_message": "plan my day",
        "brain_dump": None,
        "intent": None,
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "response_message": None,
        "conversation_phase": ConversationPhase.GREETING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "needs_consent": None,
        "error": None,
        "progress_callback": None,
    }
    assert state["user_message"] == "plan my day"
    assert state["conversation_phase"] == ConversationPhase.GREETING


def test_new_intent_types_exist():
    assert IntentType.EDIT_TASK == "EDIT_TASK"
    assert IntentType.REARRANGE == "REARRANGE"
    assert IntentType.ACCEPT_DRAFT == "ACCEPT_DRAFT"
    assert IntentType.REJECT_DRAFT == "REJECT_DRAFT"
    assert IntentType.ADD_CONSTRAINT == "ADD_CONSTRAINT"
    assert IntentType.CHECK_PROGRESS == "CHECK_PROGRESS"
    assert IntentType.RESEARCH == "RESEARCH"
    assert IntentType.CHAT == "CHAT"


# ---------------------------------------------------------------------------
# Routing tests (Task 5)
# ---------------------------------------------------------------------------

from app.orchestrator.routing import (
    route_to_module,
    check_negotiation_shortcut,
    check_needs_followup,
    INTENT_TO_MODULE,
)


def _make_state(**overrides) -> JarvisState:
    """Full-shape JarvisState — shared with tests/test_checkpointer.py."""
    return make_jarvis_state(**overrides)


def test_route_plan_day():
    state = _make_state(intent="PLAN_DAY")
    assert route_to_module(state) == "planning_module"


def test_route_chat_fallback():
    state = _make_state(intent="UNKNOWN_INTENT")
    assert route_to_module(state) == "conversation_module"


def test_route_negotiation_overrides_intent():
    state = _make_state(intent="CHAT", conversation_phase=ConversationPhase.NEGOTIATION)
    assert route_to_module(state) == "planning_module"


def test_route_infeasible_fallback_to_coach():
    state = _make_state(intent="PLAN_DAY", modules_invoked=["planning_module"], error="INFEASIBLE")
    assert route_to_module(state) == "coach_module"


def test_negotiation_shortcut_active():
    state = _make_state(negotiation_state=NegotiationPhase.REVIEWING)
    assert check_negotiation_shortcut(state) == "negotiation_active"


def test_negotiation_shortcut_normal():
    state = _make_state(negotiation_state=NegotiationPhase.NONE)
    assert check_negotiation_shortcut(state) == "normal"


def test_needs_followup_false():
    state = _make_state(needs_followup=False)
    assert check_needs_followup(state) == "done"


def test_needs_followup_true():
    state = _make_state(needs_followup=True)
    assert check_needs_followup(state) == "continue"


# ---------------------------------------------------------------------------
# Graph tests (Task 6)
# ---------------------------------------------------------------------------

import pytest
from app.orchestrator.graph import build_jarvis_graph


@pytest.mark.asyncio
async def test_graph_compiles():
    graph = build_jarvis_graph()
    assert graph is not None


@pytest.mark.asyncio
async def test_graph_has_expected_nodes():
    graph = build_jarvis_graph()
    node_names = set(graph.nodes.keys())
    expected = {
        "load_context",
        "extract_brain_dump",
        "classify_intent",
        "planning_module",
        "research_agent",
        "coach_module",
        "knowledge_module",
        "conversation_module",
        "synthesize_response",
        "observation_loop",
    }
    assert expected.issubset(node_names), f"Missing nodes: {expected - node_names}"


@pytest.mark.asyncio
async def test_graph_runs_chat_end_to_end(no_llm):
    graph = build_jarvis_graph()
    initial_state = _make_state(user_message="hello")
    result = await graph.ainvoke(initial_state)
    # The mocked reply must survive the whole graph; `is not None` would also
    # be satisfied by the conversation module's LLM-failure fallback string.
    assert result["response_message"] == CANNED_LLM_REPLY
    assert "conversation_module" in result["modules_invoked"]
    assert result["needs_followup"] is False


# ---------------------------------------------------------------------------
# Serializable state — user_id in state, UserModel hydrated in load_context
# ---------------------------------------------------------------------------


def test_jarvis_state__declares_serializable_user_id():
    """The checkpointer persists user_id; the UserModel facade is rebuilt per turn."""
    assert JarvisState.__annotations__.get("user_id") is str


@pytest.mark.asyncio
async def test_load_context__hydrates_user_model_from_user_id():
    from app.orchestrator.graph import _load_context

    state = _make_state(user_id="u42", user_model=None)
    result = await _load_context(state)

    assert result["user_model"] is not None
    assert result["user_model"].user_id == "u42"


@pytest.mark.asyncio
async def test_graph__hydrated_user_model_reaches_final_state(no_llm):
    """The load_context node's hydration must merge into the state the graph carries."""
    graph = build_jarvis_graph()
    result = await graph.ainvoke(_make_state(user_id="u42", user_model=None, user_message="hello"))

    assert result["user_id"] == "u42"
    assert result["user_model"] is not None
    assert result["user_model"].user_id == "u42"


@pytest.mark.asyncio
async def test_load_context__keeps_existing_user_model():
    """Live requests pre-wire the facade with shared db/memory clients — never clobber it."""
    from app.orchestrator.graph import _load_context

    class Prebuilt:
        user_id = "u42"

    prebuilt = Prebuilt()
    state = _make_state(user_id="u42", user_model=prebuilt)
    result = await _load_context(state)

    assert "user_model" not in result


@pytest.mark.asyncio
async def test_load_context__no_user_id__stays_a_noop():
    """Without an identity there is nothing to hydrate — behave exactly as before."""
    from app.orchestrator.graph import _load_context

    state = _make_state(user_model=None)
    state.pop("user_id", None)

    assert await _load_context(state) == {}


# ---------------------------------------------------------------------------
# Hydration must produce a FUNCTIONAL facade (Task 7)
#
# A db=None facade is truthy, so it silently inverts every downstream
# `if user_model:` guard: a checkpoint-resumed PLAN_DAY turn would reach
# planning_graph's `get_behavioral_constraints()` and die on
# `AttributeError: 'NoneType' object has no attribute 'supabase'`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_context__hydrated_facade_can_query_the_registered_db():
    from app.core.runtime import set_shared_clients
    from app.orchestrator.graph import _load_context

    db = FakeDBClient()
    set_shared_clients(db=db)

    result = await _load_context(_make_state(user_id="u42", user_model=None))
    user_model = result["user_model"]

    # The real assertion: this call is what a resumed PLAN_DAY turn makes.
    # With db=None it raises AttributeError instead of returning rows.
    assert await user_model.get_behavioral_constraints() == []


@pytest.mark.asyncio
async def test_load_context__hydrated_facade_gets_the_registered_memory_store():
    from app.core.runtime import set_shared_clients
    from app.orchestrator.graph import _load_context

    store = object()
    set_shared_clients(db=FakeDBClient(), memory_store=store)

    result = await _load_context(_make_state(user_id="u42", user_model=None))

    assert await result["user_model"].get_memory_store() is store


@pytest.mark.asyncio
async def test_load_context__before_startup__degrades_to_a_db_less_facade():
    """No lifespan yet (tests, scripts) — hydrate anyway rather than crash."""
    from app.orchestrator.graph import _load_context

    result = await _load_context(_make_state(user_id="u42", user_model=None))
    user_model = result["user_model"]

    assert user_model.user_id == "u42"
    assert user_model._db is None


def test_runtime__accessors_are_none_before_registration():
    from app.core import runtime

    assert runtime.get_db() is None
    assert runtime.get_memory_store() is None


# ---------------------------------------------------------------------------
# Draft negotiation intents (Task 10)
#
# `_classify_intent` is an async node returning the LangGraph patch
# ``{"intent": IntentType.X}`` — not a bare enum — so every assertion below
# unwraps that key. `IntentType` already carries all four draft verbs
# (context.py:37-40); what was missing was anything that ever *emits* them.
# ---------------------------------------------------------------------------


async def _intent_of(state) -> str:
    from app.orchestrator.graph import _classify_intent

    patch = await _classify_intent(state)
    intent = patch["intent"]
    return getattr(intent, "value", intent)


@pytest.mark.asyncio
async def test_classify__accept_during_review__accept_draft():
    state = _make_state(user_message="accept", negotiation_state=NegotiationPhase.REVIEWING)
    assert await _intent_of(state) == "ACCEPT_DRAFT"


@pytest.mark.asyncio
async def test_classify__reject_during_review__reject_draft():
    state = _make_state(
        user_message="no, scrap that plan", negotiation_state=NegotiationPhase.REVIEWING
    )
    assert await _intent_of(state) == "REJECT_DRAFT"


@pytest.mark.asyncio
async def test_classify__rearrange_during_review__rearrange():
    state = _make_state(
        user_message="move the calculus block to the evening",
        negotiation_state=NegotiationPhase.PROPOSED,
    )
    assert await _intent_of(state) == "REARRANGE"


@pytest.mark.asyncio
async def test_classify__edit_during_review__edit_task():
    state = _make_state(
        user_message="shorten the second task", negotiation_state=NegotiationPhase.EDITING
    )
    assert await _intent_of(state) == "EDIT_TASK"


@pytest.mark.asyncio
async def test_classify__accept_without_active_negotiation__not_accept():
    """"accept" is only a draft verb while something is actually on the table."""
    state = _make_state(user_message="accept", negotiation_state=NegotiationPhase.NONE)
    assert await _intent_of(state) != "ACCEPT_DRAFT"


@pytest.mark.asyncio
async def test_classify__file_upload_still_wins_over_draft_verbs():
    """An upload is a knowledge event no matter what the caption says."""
    state = _make_state(
        user_message="accept", negotiation_state=NegotiationPhase.REVIEWING,
        file_base64="ZmFrZQ==",
    )
    assert await _intent_of(state) == "KNOWLEDGE_INGESTION"


def test_routing__draft_intents_reach_the_draft_action_node():
    for intent in ("ACCEPT_DRAFT", "REJECT_DRAFT", "EDIT_TASK", "REARRANGE"):
        assert INTENT_TO_MODULE[intent] == "draft_action"
        assert route_to_module(_make_state(intent=intent)) == "draft_action"


# --- the negotiation shortcut pre-check ------------------------------------


class _FakeDraftStore:
    """Minimal DraftStore surface: the four calls the draft_action node makes."""

    _DEFAULT = object()  # so draft=None can mean "this store is empty"

    def __init__(self, draft=_DEFAULT):
        self.draft = draft if draft is not _FakeDraftStore._DEFAULT else {
            "id": "d1",
            "user_id": "test_user",
            "tasks": [{"task_id": "t1", "title": "read", "duration_minutes": 25}],
            "horizon_start": "2026-08-08T08:00:00Z",
        }
        self.accepted: list[tuple] = []
        self.rejected: list[tuple] = []
        self.get_calls: list[tuple] = []

    def get_pending_draft(self, user_id):
        self.get_calls.append(("pending", user_id))
        return self.draft if self.draft and self.draft["user_id"] == user_id else None

    def get_draft(self, draft_id, user_id):
        self.get_calls.append(("by_id", draft_id, user_id))
        if self.draft and self.draft["id"] == draft_id and self.draft["user_id"] == user_id:
            return self.draft
        return None

    def accept_draft(self, draft_id, user_id):
        self.accepted.append((draft_id, user_id))
        return True

    def reject_draft(self, draft_id, user_id, reason=None):
        self.rejected.append((draft_id, user_id, reason))
        return True


def _user_model_with_db():
    """A real UserModel over a real FakeSupabase.

    Accept now verifies its own write by re-reading user_tasks, so the fake has
    to be a working store: a MagicMock would confirm any write, including one
    that never happened. The facade is the genuine class so the observation
    loop downstream finds the whole surface it expects.
    """
    from app.core.user_model import UserModel
    from tests.fakes import FakeDBClient, FakeSupabase

    return UserModel(user_id="test_user", db=FakeDBClient(FakeSupabase()))


@pytest.mark.asyncio
async def test_precheck__draft_verb__routes_to_draft_action():
    from app.orchestrator.graph import _negotiation_precheck
    from app.orchestrator.routing import route_draft_action

    state = _make_state(user_message="looks good", negotiation_state=NegotiationPhase.REVIEWING)
    state.update(await _negotiation_precheck(state))

    assert route_draft_action(state) == "draft_action"


@pytest.mark.asyncio
async def test_precheck__no_draft_verb__falls_through_to_planning():
    """The shortcut's old behaviour must survive: a non-verb turn still re-plans."""
    from app.orchestrator.graph import _negotiation_precheck
    from app.orchestrator.routing import route_draft_action

    state = _make_state(
        user_message="also add my chemistry revision", negotiation_state=NegotiationPhase.REVIEWING
    )
    state.update(await _negotiation_precheck(state))

    assert route_draft_action(state) == "planning_module"


# --- the draft_action node --------------------------------------------------


@pytest.fixture
def _captured_persist(monkeypatch):
    """Record _persist_fused_tasks calls; the node imports it function-locally."""
    calls = []

    def fake_persist(user_id, chunks, supabase_client, schedule=None, horizon_start=None):
        calls.append({
            "user_id": user_id, "chunks": list(chunks), "supabase": supabase_client,
            "schedule": schedule, "horizon_start": horizon_start,
        })

    monkeypatch.setattr(
        "app.services.analytical.control_policy._persist_fused_tasks", fake_persist
    )
    return calls


@pytest.mark.asyncio
async def test_draft_action__accept__persists_tasks_and_closes_negotiation():
    """Runs the real _persist_fused_tasks against a FakeSupabase — a stubbed
    persist would satisfy the accept path's verification query with nothing."""
    from app.orchestrator.graph import handle_draft_action

    store = _FakeDraftStore()
    user_model = _user_model_with_db()
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, user_message="accept", user_model=user_model,
    )
    result = await handle_draft_action(state)

    rows = user_model._db.supabase.rows.get("user_tasks", [])
    assert [(r["task_id"], r["user_id"]) for r in rows] == [("t1", "test_user")]
    assert store.accepted == [("d1", "test_user")]
    assert result["negotiation_state"] == NegotiationPhase.ACCEPTED
    assert result["draft_id"] is None
    assert "1 task is" in result["response_message"]


@pytest.mark.asyncio
async def test_draft_action__accept_targets_the_draft_id_in_state(_captured_persist):
    """State's draft_id wins over get_pending_draft — orphaned pending rows exist."""
    from app.orchestrator.graph import handle_draft_action

    store = _FakeDraftStore()
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, draft_id="d1", user_message="accept",
    )
    await handle_draft_action(state)

    assert store.get_calls[0] == ("by_id", "d1", "test_user")


@pytest.mark.asyncio
async def test_draft_action__reject__clears_negotiation(_captured_persist):
    from app.orchestrator.graph import handle_draft_action

    store = _FakeDraftStore()
    state = _make_state(
        intent=IntentType.REJECT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, user_message="scrap that",
    )
    result = await handle_draft_action(state)

    assert store.rejected == [("d1", "test_user", "scrap that")]
    assert result["negotiation_state"] == NegotiationPhase.NONE
    assert result["draft_id"] is None
    assert _captured_persist == []  # rejection must never touch user_tasks


@pytest.mark.asyncio
async def test_draft_action__edit__keeps_negotiation_open(_captured_persist):
    from app.orchestrator.graph import handle_draft_action

    state = _make_state(
        intent=IntentType.EDIT_TASK, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=_FakeDraftStore(), user_message="shorten task 2",
    )
    result = await handle_draft_action(state)

    assert result["negotiation_state"] == NegotiationPhase.EDITING
    assert _captured_persist == []


@pytest.mark.asyncio
async def test_draft_action__no_pending_draft__still_exits_negotiation():
    """The exit door: nothing else writes ACCEPTED/NONE, so a dead end here
    would lock the thread into eternal re-planning via check_negotiation_shortcut."""
    from app.orchestrator.graph import handle_draft_action

    store = _FakeDraftStore(draft=None)
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, user_message="accept",
    )
    result = await handle_draft_action(state)

    assert result["negotiation_state"] == NegotiationPhase.NONE
    assert result["draft_id"] is None


@pytest.mark.asyncio
async def test_draft_action__no_store__still_exits_negotiation():
    from app.orchestrator.graph import handle_draft_action

    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=None, user_message="accept",
    )
    result = await handle_draft_action(state)

    assert result["negotiation_state"] == NegotiationPhase.NONE


@pytest.mark.asyncio
async def test_draft_action__another_users_draft_is_never_reachable(_captured_persist):
    """IDOR: the store is filtered by user_id, so a foreign draft reads as absent."""
    from app.orchestrator.graph import handle_draft_action

    store = _FakeDraftStore()
    store.draft["user_id"] = "someone_else"
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, draft_id="d1", user_message="accept",
    )
    result = await handle_draft_action(state)

    assert store.accepted == []
    assert _captured_persist == []
    assert result["negotiation_state"] == NegotiationPhase.NONE


# --- wiring -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph__has_draft_action_nodes():
    graph = build_jarvis_graph()
    assert {"negotiation_precheck", "draft_action"}.issubset(set(graph.nodes.keys()))


@pytest.mark.asyncio
async def test_graph__accept_turn_reaches_draft_action_end_to_end(no_llm):
    """A REVIEWING turn saying "accept" must not be swallowed by the planning shortcut."""
    graph = build_jarvis_graph()
    store = _FakeDraftStore()
    user_model = _user_model_with_db()
    result = await graph.ainvoke(
        _make_state(
            user_message="accept", negotiation_state=NegotiationPhase.REVIEWING,
            draft_store=store, draft_id="d1", user_model=user_model,
        )
    )

    assert store.accepted == [("d1", "test_user")]
    assert result["negotiation_state"] == NegotiationPhase.ACCEPTED
    assert result["draft_id"] is None
    assert [r["task_id"] for r in user_model._db.supabase.rows.get("user_tasks", [])] == ["t1"]


@pytest.mark.asyncio
async def test_classify__negated_accept_verb__does_not_accept():
    """"don't do it" contains "do it". ACCEPT_DRAFT is the only intent that
    writes user_tasks, so a negated match must never reach it."""
    state = _make_state(
        user_message="don't do it yet", negotiation_state=NegotiationPhase.REVIEWING
    )
    assert await _intent_of(state) != "ACCEPT_DRAFT"


@pytest.mark.asyncio
async def test_draft_action__store_failure__keeps_the_draft_open(_captured_persist):
    """A Supabase blip must not 500 the turn — nor claim the plan went live."""
    from app.orchestrator.graph import handle_draft_action

    class _BrokenStore(_FakeDraftStore):
        def accept_draft(self, draft_id, user_id):
            raise RuntimeError("supabase unreachable")

    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=_BrokenStore(), draft_id="d1", user_message="accept",
    )
    result = await handle_draft_action(state)

    assert result["response_message"]
    assert _captured_persist == []
    # negotiation_state left untouched → still REVIEWING → "accept" retries
    # instead of the next message starting a whole new plan.
    assert "negotiation_state" not in result


# ---------------------------------------------------------------------------
# Draft-verb matcher precision (Task 10 review)
#
# ACCEPT_DRAFT is the only intent in v2 that writes user_tasks, and
# _persist_fused_tasks deletes every pending row before inserting. So a false
# positive here does not merely mis-answer — it destroys the user's task list.
# These utterances are ordinary mid-review conversation and must never match.
# ---------------------------------------------------------------------------

_MUST_NOT_ACCEPT = [
    "I need to confirm my exam registration and finish chapter 3",
    "can you confirm what's at 3pm?",
    "I'll do it later",
    "acceptance testing is due friday",
    "looks good but move the calculus block later",
    "go ahead and tell me what's on friday",
    "do it after the exam",
    "I approve of that approach in general",
    "confirmation email came through",
    "don't do it yet",
]

_MUST_ACCEPT = [
    "accept",
    "yes accept it",
    "approve",
    "looks good",
    "lgtm",
    "lock it in",
    "accept the draft",
    "yes, do it",
    "go ahead",
    "ship it",
    "looks good, lock it in",
    "confirm",
]

_MUST_NOT_REJECT = [
    "don't cancel the plan",
    "don't scrap it, just shorten task 2",
    "never discard my notes",
]

_MUST_REJECT = [
    "reject",
    "scrap that plan",
    "no, scrap it",
    "discard the draft",
    "cancel the plan",
    "start over",
]


@pytest.mark.parametrize("message", _MUST_NOT_ACCEPT)
def test_matcher__conversational_message__never_accepts(message):
    from app.orchestrator.graph import _match_draft_intent
    from app.schemas.context import IntentType

    assert _match_draft_intent(message) is not IntentType.ACCEPT_DRAFT


@pytest.mark.parametrize("message", _MUST_ACCEPT)
def test_matcher__imperative_reply__accepts(message):
    from app.orchestrator.graph import _match_draft_intent
    from app.schemas.context import IntentType

    assert _match_draft_intent(message) is IntentType.ACCEPT_DRAFT


@pytest.mark.parametrize("message", _MUST_NOT_REJECT)
def test_matcher__negated_reject__never_rejects(message):
    from app.orchestrator.graph import _match_draft_intent
    from app.schemas.context import IntentType

    assert _match_draft_intent(message) is not IntentType.REJECT_DRAFT


@pytest.mark.parametrize("message", _MUST_REJECT)
def test_matcher__rejection__rejects(message):
    from app.orchestrator.graph import _match_draft_intent
    from app.schemas.context import IntentType

    assert _match_draft_intent(message) is IntentType.REJECT_DRAFT


def test_matcher__negated_reject_with_an_edit_verb__edits_instead():
    """"don't scrap it, just shorten task 2" is an edit, and must read as one."""
    from app.orchestrator.graph import _match_draft_intent
    from app.schemas.context import IntentType

    assert _match_draft_intent("don't scrap it, just shorten task 2") is IntentType.EDIT_TASK


# --- file uploads must survive an active negotiation ------------------------


@pytest.mark.asyncio
async def test_precheck__file_upload__is_never_a_draft_verb():
    """The live path is the pre-check, not _classify_intent: an upload captioned
    "confirm this" must not accept the draft and drop the file on the floor."""
    from app.orchestrator.graph import _negotiation_precheck
    from app.orchestrator.routing import route_draft_action

    state = _make_state(
        user_message="confirm this", negotiation_state=NegotiationPhase.REVIEWING,
        file_base64="ZmFrZQ==", file_name="syllabus.pdf",
    )
    patch = await _negotiation_precheck(state)
    state.update(patch)

    assert getattr(patch["intent"], "value", patch["intent"]) == "KNOWLEDGE_INGESTION"
    assert route_draft_action(state) == "knowledge_module"


@pytest.mark.asyncio
async def test_graph__upload_during_review_reaches_knowledge_module(no_llm, _captured_persist):
    graph = build_jarvis_graph()
    store = _FakeDraftStore()
    result = await graph.ainvoke(
        _make_state(
            user_message="confirm this", negotiation_state=NegotiationPhase.REVIEWING,
            draft_store=store, draft_id="d1", file_base64="ZmFrZQ==", file_name="syllabus.pdf",
        )
    )

    assert "knowledge_module" in result["modules_invoked"]
    assert store.accepted == []
    assert _captured_persist == []


# --- accept must not claim success it cannot verify -------------------------


class _VerifyingStore(_FakeDraftStore):
    """Observes how many user_tasks rows existed at the moment of the status flip.

    A plain call-order list would not prove anything — both calls happen either
    way. Sampling the table at flip time is what actually distinguishes
    persist-then-flip from flip-then-persist.
    """

    def __init__(self, *args, supabase=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._supabase = supabase
        self.rows_at_flip = None

    def accept_draft(self, draft_id, user_id):
        if self._supabase is not None:
            self.rows_at_flip = len(self._supabase.rows.get("user_tasks", []))
        return super().accept_draft(draft_id, user_id)


@pytest.mark.asyncio
async def test_accept__persist_failure__reports_honestly_and_keeps_the_draft(monkeypatch):
    """_persist_fused_tasks swallows its own exceptions, so "it returned" is not
    evidence anything landed. A silent failure must not answer "Locked in" —
    and must not flip the draft to accepted, which would hide it from the retry."""
    from app.orchestrator.graph import handle_draft_action

    monkeypatch.setattr(
        "app.services.analytical.control_policy._persist_fused_tasks",
        lambda *a, **k: None,  # exactly what a swallowed failure looks like
    )
    store = _VerifyingStore()
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, draft_id="d1", user_message="accept",
        user_model=_user_model_with_db(),
    )
    result = await handle_draft_action(state)

    assert store.accepted == []            # status NOT flipped
    assert "negotiation_state" not in result  # still REVIEWING → "accept" retries
    assert "locked in" not in result["response_message"].lower()


@pytest.mark.asyncio
async def test_accept__flips_status_only_after_the_tasks_are_in_the_table():
    """Order matters: flipping first leaves an accepted-but-empty draft that
    get_pending_draft can no longer find, so the retry is gone too."""
    from app.orchestrator.graph import handle_draft_action

    user_model = _user_model_with_db()
    supabase = user_model._db.supabase
    store = _VerifyingStore(supabase=supabase)
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, draft_id="d1", user_message="accept", user_model=user_model,
    )
    result = await handle_draft_action(state)

    assert [r["task_id"] for r in supabase.rows["user_tasks"]] == ["t1"]
    assert store.accepted == [("d1", "test_user")]
    assert store.rows_at_flip == 1  # the task was already written when we flipped
    assert result["negotiation_state"] == NegotiationPhase.ACCEPTED


@pytest.mark.asyncio
async def test_accept__draft_with_no_tasks__exits_without_claiming_success():
    from app.orchestrator.graph import handle_draft_action

    store = _VerifyingStore()
    store.draft["tasks"] = []
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, draft_id="d1", user_message="accept",
        user_model=_user_model_with_db(),
    )
    result = await handle_draft_action(state)

    assert store.accepted == []
    assert result["negotiation_state"] == NegotiationPhase.NONE  # exit door
    assert "locked in" not in result["response_message"].lower()


# --- acceptance must be the message's whole point, not a phrase inside it ----

_MUST_NOT_ACCEPT_MENTIONS = [
    "my calculus grade looks good",
    "the professor approved my topic",
    "accept my apology",
    "the acceptance criteria looks good",
    "his lgtm came late",
    "that ship it mentality",
    "the plan is good to go",
]


@pytest.mark.parametrize("message", _MUST_NOT_ACCEPT_MENTIONS)
def test_matcher__acceptance_word_inside_a_sentence__never_accepts(message):
    """Short is not the same as imperative. All seven are <= 6 words."""
    from app.orchestrator.graph import _match_draft_intent
    from app.schemas.context import IntentType

    assert _match_draft_intent(message) is not IntentType.ACCEPT_DRAFT


class _WriteFailingSupabase(FakeSupabase):
    """Reads work, every write raises — a Supabase outage mid-accept.

    _persist_fused_tasks catches the exception itself, so the caller sees a
    perfectly clean return with nothing written.
    """

    def table(self, name):
        query = super().table(name)
        real_execute = query.execute

        def execute():
            if query._op in ("insert", "update", "delete"):
                raise RuntimeError("supabase write refused")
            return real_execute()

        query.execute = execute
        return query


@pytest.mark.asyncio
async def test_accept__write_failure_over_a_preexisting_row__is_not_read_as_success():
    """fuse_tasks merges pre-existing pending rows into drafts, so a draft task
    can already be in user_tasks before accept runs. Verifying by task_id alone
    would count that stale row as proof the new write landed — and _persist_fused_tasks
    builds its rows before deleting, so a total failure leaves it sitting there."""
    from app.core.user_model import UserModel
    from app.orchestrator.graph import handle_draft_action
    from tests.fakes import FakeDBClient

    supabase = _WriteFailingSupabase(
        {"user_tasks": [
            {"user_id": "test_user", "task_id": "t1", "status": "pending",
             "plan_id": "plan-from-last-week"},
        ]}
    )
    store = _VerifyingStore(supabase=supabase)
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, draft_id="d1", user_message="accept",
        user_model=UserModel(user_id="test_user", db=FakeDBClient(supabase)),
    )
    result = await handle_draft_action(state)

    assert store.accepted == []                 # status NOT flipped
    assert "negotiation_state" not in result    # still REVIEWING → retryable
    assert "locked in" not in result["response_message"].lower()


# --- Task 11: real token streaming through the progress bridge --------------


def test_progress_queue__token_events_forwarded():
    """Synthesis pushes token events through the progress bridge."""
    import asyncio
    import json

    from app.orchestrator.graph import make_token_emitter

    q = asyncio.Queue()
    emit = make_token_emitter(q)
    emit("Hel")
    emit("lo")

    first = json.loads(q.get_nowait())
    assert first == {"_event_type": "token", "token": "Hel"}
    second = json.loads(q.get_nowait())
    assert second == {"_event_type": "token", "token": "lo"}
    assert q.empty()


def test_token_emitter__thinking_tokens__carry_their_own_event_type():
    """Reasoning tokens must not land in the visible message stream."""
    import asyncio
    import json

    from app.orchestrator.graph import make_token_emitter

    q = asyncio.Queue()
    emit = make_token_emitter(q)
    emit("weighing options", event_type="thinking_token")

    assert json.loads(q.get_nowait()) == {
        "_event_type": "thinking_token",
        "token": "weighing options",
    }


def test_token_emitter__no_queue__is_a_no_op():
    """Non-streaming callers (unit tests, /chat non-stream) pass no queue."""
    from app.orchestrator.graph import make_token_emitter

    emit = make_token_emitter(None)
    emit("nothing happens")  # must not raise


@pytest.mark.asyncio
async def test_synthesis__with_progress_queue__streams_tokens_not_one_blob(monkeypatch):
    """voice_of_jarvis_synthesis streams through the bridge when one is wired."""
    import asyncio
    import json

    from app.modules.conversation import voice_of_jarvis_synthesis

    async def fake_stream(execution_summary, **kwargs):
        for tok in ("Your ", "schedule ", "is set, sir."):
            yield ("message", tok)
        yield ("thinking", "Ran the solver.")

    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.synthesize_jarvis_response_stream",
        fake_stream,
    )

    q = asyncio.Queue()
    state = {
        "intent": IntentType.PLAN_DAY,
        "schedule": {"t1": {"start": 0}},
        "user_message": "plan my day",
        "progress_queue": q,
    }
    result = await voice_of_jarvis_synthesis(state)

    assert result["response_message"] == "Your schedule is set, sir."
    assert result["thinking_process"] == "Ran the solver."

    events = []
    while not q.empty():
        events.append(json.loads(q.get_nowait()))
    message_tokens = [e["token"] for e in events if e["_event_type"] == "token"]
    assert message_tokens == ["Your ", "schedule ", "is set, sir."]
    assert [e["token"] for e in events if e["_event_type"] == "thinking_token"] == [
        "Ran the solver."
    ]


@pytest.mark.asyncio
async def test_synthesis__without_progress_queue__stays_non_streaming(monkeypatch):
    """The plain /chat path must not be forced through the streaming branch."""
    from app.modules.conversation import voice_of_jarvis_synthesis

    async def fake_synthesize(execution_summary, conversation_history=None):
        return ("All sorted, sir.", "did things")

    async def exploding_stream(execution_summary, **kwargs):
        raise AssertionError("streaming path taken without a progress_queue")
        yield  # pragma: no cover

    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.synthesize_jarvis_response",
        fake_synthesize,
    )
    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.synthesize_jarvis_response_stream",
        exploding_stream,
    )

    result = await voice_of_jarvis_synthesis(
        {"intent": IntentType.PLAN_DAY, "schedule": {"t1": {}}, "user_message": "hi"}
    )
    assert result["response_message"] == "All sorted, sir."


@pytest.mark.asyncio
async def test_general_chat__with_progress_queue__streams_and_strips_think(monkeypatch):
    """CHAT streams live; <think> content is routed to the thinking channel."""
    import asyncio
    import json

    from app.modules import conversation

    async def fake_stream(**kwargs):
        async def _gen():
            for evt, tok in (
                ("reasoning", "he said hi"),
                ("content", "Good evening, "),
                ("content", "sir."),
            ):
                yield (evt, tok)
        return _gen()

    monkeypatch.setattr(
        "app.models.brain.litellm_conf.hybrid_route_query", fake_stream
    )
    monkeypatch.setattr("app.utils.chroma_client.query_knowledge", lambda *a, **k: [])

    q = asyncio.Queue()
    result = await conversation.run_general_chat({
        "user_message": "hi",
        "progress_queue": q,
        "trivial_input": True,
    })

    assert result["response_message"] == "Good evening, sir."
    assert result["thinking_process"] == "he said hi"

    events = []
    while not q.empty():
        events.append(json.loads(q.get_nowait()))
    assert [e["token"] for e in events if e["_event_type"] == "token"] == [
        "Good evening, ",
        "sir.",
    ]


# --- Task 11: progress-queue entry → SSE frame -----------------------------


def test_render_progress_event__token__lands_on_the_message_channel():
    """Streamed content tokens use the same channel v1 /chat/stream uses."""
    import json

    from app.api.v1.endpoints.chat import render_progress_event

    frame, evt_type, token = render_progress_event(
        json.dumps({"_event_type": "token", "token": "Hel"})
    )
    assert evt_type == "token"
    assert token == "Hel"
    assert frame == 'event: message\ndata: {"token": "Hel"}\n\n'


def test_render_progress_event__thinking_token__lands_on_the_thinking_channel():
    import json

    from app.api.v1.endpoints.chat import render_progress_event

    frame, evt_type, token = render_progress_event(
        json.dumps({"_event_type": "thinking_token", "token": "hmm"})
    )
    assert evt_type == "thinking_token"
    assert token == "hmm"
    assert frame.startswith("event: thinking\n")


def test_render_progress_event__unknown_type__never_leaks_event_type_to_the_ui():
    """The old drain loop popped _event_type off the parsed dict, then yielded
    the untouched raw string — so the internal key rode along into the phase payload."""
    import json

    from app.api.v1.endpoints.chat import render_progress_event

    frame, evt_type, _ = render_progress_event(
        json.dumps({"_event_type": "something_new", "phase": "planning"})
    )
    assert evt_type == "phase"
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload == {"phase": "planning"}
    assert "_event_type" not in frame


def test_render_progress_event__plain_phase__passes_through():
    import json

    from app.api.v1.endpoints.chat import render_progress_event

    raw = json.dumps({"phase": "decomposing", "detail": {"n": 3}})
    frame, evt_type, token = render_progress_event(raw)
    assert (evt_type, token) == ("phase", "")
    assert json.loads(frame.split("data: ", 1)[1].strip()) == {
        "phase": "decomposing", "detail": {"n": 3},
    }


def test_render_progress_event__malformed__still_emits_a_phase_frame():
    from app.api.v1.endpoints.chat import render_progress_event

    frame, evt_type, token = render_progress_event("not json at all")
    assert (evt_type, token) == ("phase", "")
    assert frame == "event: phase\ndata: not json at all\n\n"


# --- Task 11 review: context and the PII gate on the streaming twin --------


@pytest.mark.asyncio
async def test_voj_stream__conversation_history__reaches_the_model(monkeypatch):
    """Streamed synthesis was context-blind: the parameter did not exist, so
    every live SSE turn (which always has a progress_queue) synthesized without
    the conversation, while the non-streaming twin got it."""
    import app.models.brain.litellm_conf as _llm
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response_stream

    seen: dict = {}
    history = [{"role": "user", "content": "plan my thursday"}]

    async def fake_hybrid(**kwargs):
        seen.update(kwargs)

        async def _gen():
            yield ("content", "Done, sir.")
        return _gen()

    monkeypatch.setattr(_llm, "hybrid_route_query", fake_hybrid)
    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.hybrid_route_query", fake_hybrid
    )

    _ = [
        tok
        async for _evt, tok in synthesize_jarvis_response_stream(
            {"schedule_generated": True}, conversation_history=history
        )
    ]
    assert seen["conversation_history"] == history


@pytest.mark.asyncio
async def test_synthesis_node__streaming__forwards_conversation_history(monkeypatch):
    """The call site must pass it, not just the signature accept it."""
    from app.modules.conversation import voice_of_jarvis_synthesis

    seen: dict = {}
    history = [{"role": "user", "content": "plan my thursday"}]

    async def fake_stream(execution_summary, conversation_history=None):
        seen["history"] = conversation_history
        yield ("message", "Done, sir.")

    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.synthesize_jarvis_response_stream",
        fake_stream,
    )

    import asyncio

    await voice_of_jarvis_synthesis({
        "intent": IntentType.PLAN_DAY,
        "schedule": {"t1": {}},
        "user_message": "plan my thursday",
        "conversation_history": history,
        "progress_queue": asyncio.Queue(),
    })
    assert seen["history"] == history


@pytest.mark.asyncio
async def test_voj_stream__cloud_route__pii_never_reaches_the_wire(monkeypatch):
    """Synthesis calls hybrid_route_query directly, so route_llm_call's
    PreCloudLLM gate never ran — and under GEMINI_PRIMARY every synthesis is a
    cloud send."""
    import app.core.config as _cfg
    import app.models.brain.litellm_conf as _llm
    from app.orchestrator.hooks import ActionHooks, register_all_hooks
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response_stream

    seen: dict = {}

    async def fake_hybrid(**kwargs):
        seen.update(kwargs)

        async def _gen():
            yield ("content", "Noted, sir.")
        return _gen()

    monkeypatch.setattr(_llm, "hybrid_route_query", fake_hybrid)
    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.hybrid_route_query", fake_hybrid
    )
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", True)

    hooks = ActionHooks()
    register_all_hooks(hooks)
    monkeypatch.setattr("app.orchestrator.hooks.get_hooks", lambda: hooks)

    _ = [
        tok
        async for _evt, tok in synthesize_jarvis_response_stream({
            "habits_saved": "reach me on 555-123-4567",
            "memory_context": "user email is madhav@example.com",
        })
    ]

    wire = seen["user_prompt"] + seen["system_prompt"]
    assert "555-123-4567" not in wire
    assert "madhav@example.com" not in wire
    assert "[PHONE]" in wire and "[EMAIL]" in wire


@pytest.mark.asyncio
async def test_voj_nonstream__cloud_route__pii_never_reaches_the_wire(monkeypatch):
    """Both twins gate symmetrically — fixing only the streaming one would
    leave the plain /chat path sending the same PII."""
    import app.core.config as _cfg
    from app.orchestrator.hooks import ActionHooks, register_all_hooks
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

    seen: dict = {}

    async def fake_hybrid(**kwargs):
        seen.update(kwargs)
        return "Noted, sir."

    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.hybrid_route_query", fake_hybrid
    )
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", True)

    hooks = ActionHooks()
    register_all_hooks(hooks)
    monkeypatch.setattr("app.orchestrator.hooks.get_hooks", lambda: hooks)

    await synthesize_jarvis_response({
        "habits_saved": "reach me on 555-123-4567",
        "memory_context": "user email is madhav@example.com",
    })

    wire = seen["user_prompt"] + seen["system_prompt"]
    assert "555-123-4567" not in wire and "madhav@example.com" not in wire
    assert "[PHONE]" in wire and "[EMAIL]" in wire


@pytest.mark.asyncio
async def test_voj_stream__local_route__prompt_is_untouched(monkeypatch):
    """The gate is for cloud sends only — a local Gemma call must not pay for
    it, and must not see redacted text."""
    import app.core.config as _cfg
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response_stream

    seen: dict = {}

    async def fake_hybrid(**kwargs):
        seen.update(kwargs)

        async def _gen():
            yield ("content", "Noted, sir.")
        return _gen()

    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.hybrid_route_query", fake_hybrid
    )
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(
        "app.core.model_router.force_cloud_var",
        __import__("contextvars").ContextVar("fc_local", default=False),
    )

    _ = [
        tok
        async for _evt, tok in synthesize_jarvis_response_stream(
            {"habits_saved": "reach me on 555-123-4567"}
        )
    ]
    assert "555-123-4567" in seen["user_prompt"]


# ---------------------------------------------------------------------------
# store_constraint node — BEHAVIORAL_CONSTRAINT must actually store habits
# ---------------------------------------------------------------------------


def test_routing__behavioral_constraint__goes_to_store_constraint():
    from app.orchestrator.routing import INTENT_TO_MODULE

    assert INTENT_TO_MODULE["BEHAVIORAL_CONSTRAINT"] == "store_constraint"


@pytest.mark.asyncio
async def test_store_constraint__persists_each_habit_and_confirms(monkeypatch):
    from app.orchestrator import graph as graph_mod
    from app.schemas.context import BrainDumpExtraction

    stored = []

    async def fake_store(raw_text, constraint_type="preference", user_id=None,
                         structured_override=None, supabase_client=None):
        stored.append((raw_text, constraint_type, user_id))
        return {"status": "stored", "id": f"c-{len(stored)}"}

    monkeypatch.setattr(
        "app.services.extraction.behavioral_store.store_behavioral_constraint",
        fake_store,
    )
    state = make_jarvis_state(
        user_message="I never want to study before 11am",
        user_id="u1",
    )
    state["brain_dump"] = BrainDumpExtraction(
        inline_habits=["I never want to study before 11am"]
    )
    result = await graph_mod.store_constraint(state)

    assert stored == [("I never want to study before 11am", "habit", "u1")]
    assert result["saved_constraints"] == ["I never want to study before 11am"]
    assert "11am" in result["response_message"]


@pytest.mark.asyncio
async def test_store_constraint__no_habits__honest_message(monkeypatch):
    from app.orchestrator import graph as graph_mod

    called = []
    monkeypatch.setattr(
        "app.services.extraction.behavioral_store.store_behavioral_constraint",
        lambda *a, **k: called.append(1),
    )
    state = make_jarvis_state(user_message="hmm", user_id="u1")
    state["brain_dump"] = None
    result = await graph_mod.store_constraint(state)

    assert called == []
    assert result["saved_constraints"] == []
    assert result["response_message"]


@pytest.mark.asyncio
async def test_store_constraint__storage_fails__does_not_claim_success(monkeypatch):
    from app.orchestrator import graph as graph_mod
    from app.schemas.context import BrainDumpExtraction

    async def failing_store(*a, **k):
        return {"status": "error", "error": "db down"}

    monkeypatch.setattr(
        "app.services.extraction.behavioral_store.store_behavioral_constraint",
        failing_store,
    )
    state = make_jarvis_state(user_message="no meetings on fridays", user_id="u1")
    state["brain_dump"] = BrainDumpExtraction(inline_habits=["no meetings on fridays"])
    result = await graph_mod.store_constraint(state)

    assert result["saved_constraints"] == []
    assert "couldn't" in result["response_message"].lower() or "failed" in result["response_message"].lower()


def test_graph__store_constraint_node_is_wired():
    from app.orchestrator.graph import build_jarvis_graph

    g = build_jarvis_graph()
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert ("store_constraint", "observation_loop") in edges


# ---------------------------------------------------------------------------
# synthesis honesty — clarifications must never be re-synthesized into success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesis__clarification_no_schedule__is_verbatim_no_llm(monkeypatch):
    """INFEASIBLE_EXHAUSTED produced 'has been scheduled, sir' fiction: the
    summary carried the decomposed tasks, so the LLM narrated success. A
    clarification with no schedule must pass through untouched, zero LLM."""
    from app.modules.conversation import voice_of_jarvis_synthesis

    def tripwire(*a, **k):
        raise AssertionError("LLM must not be called for a clarification turn")

    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.synthesize_jarvis_response", tripwire
    )
    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.synthesize_jarvis_response_stream", tripwire
    )
    clar = ("I couldn't fit everything in even with a 30-day window. "
            "This is a scope problem, not a you problem. "
            "Want to reduce scope or extend the deadline?")
    state = {
        "intent": "PLAN_DAY",
        "schedule": None,
        "clarification_request": clar,
        "execution_graph": {"decomposition": [{"task_id": "t1", "title": "x"}]},
        "progress_queue": None,
    }
    result = await voice_of_jarvis_synthesis(state)
    assert result["response_message"] == clar


@pytest.mark.asyncio
async def test_synthesis__clarification_with_anti_guilt__both_surface(monkeypatch):
    from app.modules.conversation import voice_of_jarvis_synthesis

    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.synthesize_jarvis_response",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM")),
    )
    state = {
        "intent": "PLAN_DAY",
        "schedule": None,
        "clarification_request": "Want to reduce scope?",
        "anti_guilt_message": "2 earlier tasks slipped past their dates — rolled forward, no harm done.",
        "progress_queue": None,
    }
    result = await voice_of_jarvis_synthesis(state)
    assert "rolled forward" in result["response_message"]
    assert "reduce scope" in result["response_message"]
