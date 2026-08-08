from app.orchestrator.state import (
    ConversationPhase,
    NegotiationPhase,
    JarvisState,
)
from app.schemas.context import IntentType

from tests.fakes import CANNED_LLM_REPLY, FakeDBClient, make_jarvis_state


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
async def test_draft_action__accept__persists_tasks_and_closes_negotiation(_captured_persist):
    from app.orchestrator.graph import handle_draft_action

    store = _FakeDraftStore()
    state = _make_state(
        intent=IntentType.ACCEPT_DRAFT, negotiation_state=NegotiationPhase.REVIEWING,
        draft_store=store, user_message="accept",
    )
    result = await handle_draft_action(state)

    assert len(_captured_persist) == 1
    assert _captured_persist[0]["user_id"] == "test_user"
    assert len(_captured_persist[0]["chunks"]) == 1
    assert _captured_persist[0]["horizon_start"] == "2026-08-08T08:00:00Z"
    assert store.accepted == [("d1", "test_user")]
    assert result["negotiation_state"] == NegotiationPhase.ACCEPTED
    assert result["draft_id"] is None
    assert result["response_message"]


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
async def test_graph__accept_turn_reaches_draft_action_end_to_end(no_llm, _captured_persist):
    """A REVIEWING turn saying "accept" must not be swallowed by the planning shortcut."""
    graph = build_jarvis_graph()
    store = _FakeDraftStore()
    result = await graph.ainvoke(
        _make_state(
            user_message="accept", negotiation_state=NegotiationPhase.REVIEWING,
            draft_store=store, draft_id="d1",
        )
    )

    assert store.accepted == [("d1", "test_user")]
    assert result["negotiation_state"] == NegotiationPhase.ACCEPTED
    assert result["draft_id"] is None
    assert len(_captured_persist) == 1
