"""Chat turns: a brain dump becomes a draft, "looks good" locks it in, and nothing leaks.

Every test drives the real ``/chat/stream`` handler through the real
orchestrator, planning sub-graph and CP-SAT solver, with a scripted LLM and an
in-memory database (see ``conftest.py``). The assertions are what the user would
notice: the reply, whether a draft is waiting, what landed in their task list,
and how many model calls the turn cost.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.services.planning.scheduling as scheduling
from app.domain.events import NullEventBus
from app.infrastructure.persistence.supabase_repos import SupabaseDraftRepository
from app.orchestrator.state import NegotiationPhase
from app.services.draft_service import DraftService
from tests.behaviour.conftest import brain_dump, decomposition

DSA_DUMP = "DSA exam on Friday. I still need to revise graphs, DP and trees, and do some mock problems."
IST = ZoneInfo("Asia/Kolkata")
DSA_TOPICS = ["Graph traversal", "Shortest paths", "DP on sequences", "Tree recursion", "Mock problems"]


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    """Plans are made at 08:00 in India on a Monday, the start of the user's day.

    (Was 07:30 UTC, when the horizon was UTC's 08:00; the day is the user's now.)
    """
    clock.freeze(datetime(2026, 11, 16, 8, 0, tzinfo=IST), scheduling, services_lead=timedelta(minutes=1))


def _script_dsa_plan(llm) -> None:
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA for Friday's exam")
        if "DSA exam" in req.prompt else "{}",
    )
    llm.script("socratic_chunker", decomposition(DSA_TOPICS))


async def _plan(jarvis, llm, *, user="alice", session=None):
    _script_dsa_plan(llm)
    turn = await jarvis.say(DSA_DUMP, user=user, session=session)
    assert turn.draft_id, f"expected a draft, got: {turn.reply!r}"
    return turn


@pytest.mark.asyncio
async def test_chat__brain_dump__proposes_a_draft_and_writes_no_tasks(jarvis, llm):
    _script_dsa_plan(llm)

    turn = await jarvis.say(DSA_DUMP, user="alice")

    assert turn.intent == "PLAN_DAY"
    assert turn.payload["schedule_status"] == "draft"
    draft = jarvis.draft(turn.draft_id)
    assert draft["user_id"] == "alice"
    assert draft["status"] == "pending"
    assert [t["title"] for t in draft["tasks"]] == DSA_TOPICS
    assert len(turn.payload["schedule"]["schedule"]) == len(DSA_TOPICS)
    # Proposing is not committing: the task list stays empty until the user says yes.
    assert jarvis.rows("user_tasks") == []


@pytest.mark.asyncio
async def test_chat__draft_proposed__complete_names_its_version_and_the_card_can_accept_with_it(
    jarvis, llm, monkeypatch
):
    """The chat review card sends the version it showed: the plan it accepts is the one
    the user saw, and a plan changed since is refused rather than locked in unseen."""
    import httpx

    from app.main import app

    turn = await _plan(jarvis, llm)

    container = jarvis.state.container
    drafted = await container.draft_service.get("alice", turn.draft_id)
    assert turn.payload["draft_version"] == drafted.version  # what GET /drafts/{id} says

    monkeypatch.setattr(app.state, "container", container, raising=False)
    monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        accepted = await api.post(
            f"/api/v1/drafts/{turn.draft_id}/accept",
            json={"user_id": "alice", "expected_version": turn.payload["draft_version"]},
        )

    assert accepted.status_code == 200
    assert len(jarvis.rows("user_tasks", user_id="alice")) == len(DSA_TOPICS)


@pytest.mark.asyncio
async def test_chat__turn_that_hands_over_no_draft__no_draft_version_in_complete(jarvis, llm):
    planned = await _plan(jarvis, llm)

    turn = await jarvis.say("move the mock problems later", session=planned.conversation_id)

    assert turn.draft_id is None
    assert "draft_version" not in turn.payload


@pytest.mark.asyncio
async def test_chat__looks_good_after_a_draft__locks_it_in_without_a_reply_model_call(jarvis, llm):
    planned = await _plan(jarvis, llm)
    draft_task_ids = [t["task_id"] for t in jarvis.draft(planned.draft_id)["tasks"]]
    calls_before = len(llm.calls)

    turn = await jarvis.say("looks good", session=planned.conversation_id)

    assert "Locked in" in turn.reply
    assert f"{len(DSA_TOPICS)} tasks" in turn.reply
    rows = jarvis.rows("user_tasks", user_id="alice")
    assert sorted(r["task_id"] for r in rows) == sorted(draft_task_ids)
    assert all(r["status"] == "pending" for r in rows)
    # Every accepted task carries the wall-clock slot the solver gave it.
    assert all(r.get("scheduled_start") for r in rows)
    assert jarvis.draft(planned.draft_id)["status"] == "accepted"
    # The verdict is a regex and the reply a template: no extraction, no synthesis.
    reply_path = {"brain_dump_extraction", "voice_of_jarvis", "voice_of_jarvis_synthesis"}
    assert not reply_path & set(llm.tasks(since=calls_before))


@pytest.mark.asyncio
async def test_chat__plan_already_accepted_in_the_review_ui__looks_good_says_already_locked_in(
    jarvis, llm
):
    planned = await _plan(jarvis, llm)
    # The user clicks Accept in the review panel; the chat thread still shows the draft.
    review_ui = DraftService(SupabaseDraftRepository(jarvis.db), NullEventBus())
    await review_ui.accept("alice", planned.draft_id)
    rows_after_ui_accept = jarvis.rows("user_tasks", user_id="alice")

    turn = await jarvis.say("looks good", session=planned.conversation_id)

    assert "already locked in" in turn.reply
    assert jarvis.rows("user_tasks", user_id="alice") == rows_after_ui_accept
    task_ids = [r["task_id"] for r in jarvis.rows("user_tasks", user_id="alice")]
    assert len(task_ids) == len(set(task_ids)) == len(DSA_TOPICS)


@pytest.mark.asyncio
async def test_chat__scrap_it_on_an_open_draft__discards_it_and_says_so(jarvis, llm):
    planned = await _plan(jarvis, llm)

    turn = await jarvis.say("scrap it", session=planned.conversation_id)

    assert "Draft discarded" in turn.reply
    draft = jarvis.draft(planned.draft_id)
    assert draft["status"] == "rejected"
    assert draft["rejection_reason"] == "scrap it"  # the signal PEARL learns from
    assert jarvis.rows("user_tasks", user_id="alice") == []


@pytest.mark.asyncio
async def test_chat__scrap_it_after_accepting_in_the_review_ui__never_claims_a_discard(
    jarvis, llm
):
    planned = await _plan(jarvis, llm)
    # Accepted from the review panel; the chat thread's checkpoint still says REVIEWING.
    review_ui = DraftService(SupabaseDraftRepository(jarvis.db), NullEventBus())
    await review_ui.accept("alice", planned.draft_id)
    rows_after_ui_accept = jarvis.rows("user_tasks", user_id="alice")

    turn = await jarvis.say("scrap it", session=planned.conversation_id)

    assert "discarded" not in turn.reply.lower()
    assert "already locked in" in turn.reply
    assert jarvis.draft(planned.draft_id)["status"] == "accepted"
    assert jarvis.rows("user_tasks", user_id="alice") == rows_after_ui_accept
    # The negotiation is over: the thread no longer reads the next message as a draft verb.
    thread = {"configurable": {"thread_id": f"alice:{planned.conversation_id}"}}
    checkpoint = (await jarvis.state.jarvis_graph.aget_state(thread)).values
    assert checkpoint["negotiation_state"] not in (NegotiationPhase.REVIEWING, NegotiationPhase.EDITING)


@pytest.mark.asyncio
async def test_chat__looks_good_sent_twice__second_one_writes_and_proposes_nothing(jarvis, llm):
    planned = await _plan(jarvis, llm)
    await jarvis.say("looks good", session=planned.conversation_id)
    rows_after_accept = jarvis.rows("user_tasks", user_id="alice")
    drafts_after_accept = jarvis.rows("draft_schedules", user_id="alice")

    turn = await jarvis.say("looks good", session=planned.conversation_id)

    assert jarvis.rows("user_tasks", user_id="alice") == rows_after_accept
    assert jarvis.rows("draft_schedules", user_id="alice") == drafts_after_accept
    assert "Locked in, sir" not in turn.reply  # no second claim of a fresh commit
    assert "already locked in" in turn.reply
    assert turn.draft_id is None


@pytest.mark.asyncio
async def test_chat__looks_good_twice_with_another_draft_pending__the_other_draft_stays_open(
    jarvis, llm
):
    planned = await _plan(jarvis, llm)
    await jarvis.say("looks good", session=planned.conversation_id)
    other = await _plan(jarvis, llm)  # a fresh conversation proposes a second draft
    rows_before = jarvis.rows("user_tasks", user_id="alice")

    turn = await jarvis.say("looks good", session=planned.conversation_id)

    # "looks good" here is about the plan this conversation already locked in.
    assert "already locked in" in turn.reply
    assert jarvis.draft(other.draft_id)["status"] == "pending"
    assert jarvis.rows("user_tasks", user_id="alice") == rows_before


@pytest.mark.asyncio
async def test_chat__solve_outlasts_its_step_timeout__no_plan_claimed_and_no_draft(
    jarvis, llm, monkeypatch
):
    import threading

    from app.modules.planning_graph import planning_module

    release = threading.Event()

    def stuck_solver(*_args, **_kwargs):
        release.wait(timeout=5)
        raise AssertionError("the solve should have been abandoned at the step timeout")

    solve_step = next(s for s in planning_module.steps if s.name == "solve_schedule")
    monkeypatch.setattr(solve_step, "timeout_ms", 50)
    monkeypatch.setattr(scheduling, "run_schedule", stuck_solver)
    _script_dsa_plan(llm)
    calls_before = len(llm.calls)

    try:
        turn = await jarvis.say(DSA_DUMP)
    finally:
        release.set()

    assert turn.draft_id is None
    assert turn.payload["schedule"] is None
    assert jarvis.rows("draft_schedules") == []
    assert "try again" in turn.reply
    # The honest path is a template: no model gets to narrate this turn as a plan.
    assert "voice_of_jarvis_synthesis" not in llm.tasks(since=calls_before)


@pytest.mark.asyncio
async def test_chat__turn_that_proposes_no_draft__reports_no_draft_id(jarvis, llm):
    planned = await _plan(jarvis, llm)

    turn = await jarvis.say("move the mock problems later", session=planned.conversation_id)

    assert turn.intent == "REARRANGE"
    assert turn.draft_id is None
    (saved,) = [
        m for m in jarvis.rows("chat_messages", session_id=planned.conversation_id)
        if m["role"] == "assistant" and m["content"] == turn.reply
    ]
    assert saved["metadata"]["draft_id"] is None
    # The planning turn itself still reported its draft.
    assert planned.draft_id is not None


@pytest.mark.asyncio
async def test_chat__greeting__skips_extraction_and_costs_one_model_call(jarvis, llm):
    llm.script("voice_of_jarvis", "Good evening, sir. All systems are nominal.")

    turn = await jarvis.say("hi")

    assert turn.reply == "Good evening, sir. All systems are nominal."
    # The rule-based fast path decides "this is a greeting" without a model; the
    # only call is the reply itself. No extraction, no memory mining of "hi".
    assert llm.tasks() == ["voice_of_jarvis"]


@pytest.mark.asyncio
async def test_chat__resumed_conversation__accepts_the_draft_it_proposed_not_the_newest(
    jarvis, llm
):
    first = await _plan(jarvis, llm)
    first_task_ids = sorted(t["task_id"] for t in jarvis.draft(first.draft_id)["tasks"])
    # Later, in another conversation, the user plans something else.
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Write the thesis intro")
        if "thesis" in req.prompt else "{}",
    )
    llm.script("socratic_chunker", decomposition(["Outline", "Draft para 1", "Draft para 2", "Cite", "Edit"]))
    other = await jarvis.say("I need to write my thesis intro this week")
    assert other.draft_id and other.conversation_id != first.conversation_id

    # Back in the first conversation, "looks good" means the draft shown THERE.
    await jarvis.say("looks good", session=first.conversation_id)

    assert jarvis.draft(first.draft_id)["status"] == "accepted"
    # Not accepted: replaced by the accepted plan, as the accept_draft function does.
    assert jarvis.draft(other.draft_id)["status"] == "superseded"
    assert sorted(r["task_id"] for r in jarvis.rows("user_tasks", user_id="alice")) == first_task_ids


@pytest.mark.asyncio
async def test_chat__two_users__neither_sees_nor_accepts_the_others_draft(jarvis, llm):
    alice = await _plan(jarvis, llm, user="alice")
    # Bob even reuses Alice's conversation id: he still gets his own conversation.
    bob = await _plan(jarvis, llm, user="bob", session=alice.conversation_id)
    assert bob.conversation_id != alice.conversation_id

    await jarvis.say("looks good", user="bob", session=bob.conversation_id)

    assert jarvis.draft(bob.draft_id)["status"] == "accepted"
    assert jarvis.draft(alice.draft_id)["status"] == "pending"
    assert jarvis.rows("user_tasks", user_id="alice") == []
    assert {r["user_id"] for r in jarvis.rows("user_tasks")} == {"bob"}
    # Bob's draft lookups cannot reach Alice's draft at all.
    assert await jarvis.state.container.repos.drafts.get("bob", alice.draft_id) is None


@pytest.mark.asyncio
async def test_chat__research_while_web_search_is_down__says_so_plainly_with_no_links(
    jarvis, llm, monkeypatch
):
    import app.core.config as config
    import app.models.brain.litellm_conf as transport

    async def search_down(**_kwargs):
        raise RuntimeError("HTTP 402: prepaid credits depleted")

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(transport.litellm, "acompletion", search_down)
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(search_queries=["Dijkstra's algorithm tutorial"])
        if "look up" in req.prompt else "{}",
    )

    turn = await jarvis.say("Can you look up a good Dijkstra's algorithm tutorial?")

    assert turn.intent == "RESEARCH"
    assert "web results are unavailable" in turn.reply.lower()
    assert "http" not in turn.reply.lower() and "www." not in turn.reply.lower()
    assert turn.payload["schedule"] is None


# --- which model answered ----------------------------------------------------------


def _metered_as(llm, name: str, location, model: str) -> None:
    """Serve every call from ``llm``, metered as the provider ``name`` running ``model``."""
    from app.infrastructure.llm.chain import FallbackChainProvider
    from app.infrastructure.llm.decorators import MeteredProvider
    from app.infrastructure.llm.factory import set_chain

    llm.name = name
    llm.location = location
    set_chain(FallbackChainProvider([MeteredProvider(llm, NullEventBus(), lambda _role: model)]))


@pytest.mark.asyncio
async def test_chat__claude_answers__reply_names_claude_not_gemini(jarvis, llm, monkeypatch):
    import app.core.config as config
    from app.domain.entities import LLMLocation

    monkeypatch.setattr(config, "GEMINI_PRIMARY", True)  # LM Studio down: cloud-first
    _metered_as(llm, "claude", LLMLocation.CLOUD, "claude-sonnet-5-5")

    turn = await jarvis.say("What do you think about learning Rust this year?")

    assert turn.payload["generation_metrics"]["model"] == "claude-sonnet-5-5"
    (done,) = [d for e, d in turn.events if e == "step" and d.get("stage") == "pipeline_done"]
    assert done["synthesis_model"] == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_chat__local_model_answers__reply_names_the_local_model(jarvis, llm):
    from app.domain.entities import LLMLocation

    _metered_as(llm, "local", LLMLocation.LOCAL, "openai/google/gemma-4-e2b")

    turn = await jarvis.say("What do you think about learning Rust this year?")

    assert turn.payload["generation_metrics"]["model"] == "openai/google/gemma-4-e2b"


@pytest.mark.asyncio
async def test_chat__plan_accepted_from_its_card__next_message_is_a_conversation_not_a_replan(
    jarvis, llm
):
    """Accept on the card goes over REST and never passes through the conversation, whose
    checkpoint still says the draft is under review. A question asked next must be
    answered, not read as "plan again"."""
    planned = await _plan(jarvis, llm)
    review_ui = DraftService(SupabaseDraftRepository(jarvis.db), NullEventBus())
    await review_ui.accept("alice", planned.draft_id)
    tasks_after_accept = len(jarvis.rows("user_tasks", user_id="alice"))
    drafts_after_accept = len(jarvis.rows("draft_schedules"))
    llm.script("brain_dump_extraction", "{}")  # a question: nothing to plan
    calls_before = len(llm.calls)

    turn = await jarvis.say("thanks, what do you know about how I work?",
                            session=planned.conversation_id)

    assert turn.intent != "PLAN_DAY"
    assert turn.draft_id is None
    assert "socratic_chunker" not in llm.tasks(since=calls_before)  # nothing was re-planned
    assert len(jarvis.rows("draft_schedules")) == drafts_after_accept
    assert len(jarvis.rows("user_tasks", user_id="alice")) == tasks_after_accept


@pytest.mark.asyncio
async def test_chat__plan_discarded_from_its_card__next_message_is_a_conversation_not_a_replan(
    jarvis, llm
):
    planned = await _plan(jarvis, llm)
    review_ui = DraftService(SupabaseDraftRepository(jarvis.db), NullEventBus())
    await review_ui.discard("alice", planned.draft_id)
    llm.script("brain_dump_extraction", "{}")
    calls_before = len(llm.calls)

    turn = await jarvis.say("never mind, thanks", session=planned.conversation_id)

    assert turn.intent != "PLAN_DAY"
    assert "socratic_chunker" not in llm.tasks(since=calls_before)
    assert jarvis.rows("user_tasks", user_id="alice") == []


@pytest.mark.asyncio
async def test_chat__question_that_mentions_my_notes_with_no_file__answered_not_ingested(jarvis, llm):
    """ "According to my uploaded notes, what…?" has nothing to ingest: it is a question
    about documents already kept, and must reach the conversation path."""
    llm.script("brain_dump_extraction", brain_dump(has_knowledge=True))

    turn = await jarvis.say("According to my uploaded notes, what is the rule of thumb?")

    assert turn.intent == "CHAT"
    assert "Standing by" not in turn.reply


@pytest.mark.asyncio
async def test_chat__long_notes_pasted_as_text__still_read_as_material_to_keep(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(has_knowledge=True))
    notes = "Operating systems notes. " + "Paging replaces segments with fixed frames. " * 12

    turn = await jarvis.say(notes)

    assert turn.intent == "KNOWLEDGE_INGESTION"
