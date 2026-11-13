"""Chat turns: a brain dump becomes a draft, "looks good" locks it in, and nothing leaks.

Every test drives the real ``/chat/v2/stream`` handler through the real
orchestrator, planning sub-graph and CP-SAT solver, with a scripted LLM and an
in-memory database (see ``conftest.py``). The assertions are what the user would
notice: the reply, whether a draft is waiting, what landed in their task list,
and how many model calls the turn cost.
"""

from datetime import datetime, timezone

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
from app.domain.events import NullEventBus
from app.infrastructure.persistence.supabase_repos import SupabaseDraftRepository
from app.services.draft_service import DraftService
from tests.behaviour.conftest import brain_dump, decomposition

DSA_DUMP = "DSA exam on Friday. I still need to revise graphs, DP and trees, and do some mock problems."
DSA_TOPICS = ["Graph traversal", "Shortest paths", "DP on sequences", "Tree recursion", "Mock problems"]


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    """Plans are made at 07:30 UTC on a Monday, so the 08:00 horizon is still ahead."""
    clock.freeze(datetime(2026, 11, 16, 7, 30, tzinfo=timezone.utc), schedule_endpoint)


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
async def test_chat__looks_good_after_a_draft__locks_it_in_without_a_model_call(jarvis, llm):
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
async def test_chat__looks_good_sent_twice__second_one_writes_and_proposes_nothing(jarvis, llm):
    planned = await _plan(jarvis, llm)
    await jarvis.say("looks good", session=planned.conversation_id)
    rows_after_accept = jarvis.rows("user_tasks", user_id="alice")
    drafts_after_accept = jarvis.rows("draft_schedules", user_id="alice")

    turn = await jarvis.say("looks good", session=planned.conversation_id)

    assert jarvis.rows("user_tasks", user_id="alice") == rows_after_accept
    assert jarvis.rows("draft_schedules", user_id="alice") == drafts_after_accept
    assert "Locked in, sir" not in turn.reply  # no second claim of a fresh commit


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
    assert jarvis.draft(other.draft_id)["status"] == "pending"
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
    # Bob's store lookups cannot reach Alice's draft at all.
    assert jarvis.draft_store.get_draft(alice.draft_id, "bob") is None
