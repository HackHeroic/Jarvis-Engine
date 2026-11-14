"""Learning inside the turn: memories from the one extraction call, persisted inline,
and every learning moment on the stream before ``complete``.

The brain-dump extraction also returns the lasting facts the user states about
themselves (``memories``), so a turn costs no separate memory-extraction call. They
are remembered through ``MemoryService`` while the turn runs: a fact the user asked
Jarvis to forget is recognised and dropped, and a cited ``[mN]`` handle supersedes the
memory it names. PEARL looks next. The user sees one ``memory_extracted`` frame per
memory created, reinforced or superseded and one ``pattern_detected`` frame per
pattern change, all before ``complete``. ``complete`` also carries PEARL's insights,
a re-plan hint and the honest status of a plan that could not be made.

These drive the real ``/chat/v2/stream`` handler through the behaviour harness
(``tests/behaviour/conftest.py``): a scripted model, ``FakeSupabase`` and the
composition root.
"""

import asyncio
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.core.observation as observation
import app.services.task_service as task_service
from app.domain.entities import TaskEvent, TaskEventType
from tests.behaviour.conftest import (  # noqa: F401 - embeddings, jarvis and llm are fixtures
    brain_dump,
    decomposition,
    embeddings,
    jarvis,
    llm,
    said,
)

IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
MARATHON = {"type": "goal", "content": "Training for the Mumbai marathon in March", "confidence": 0.8}
MORNINGS = {"type": "preference", "content": "Prefers studying in the mornings", "confidence": 0.8}
NIGHTS = "Now studies best late at night"
PATTERN_FRAME_KEYS = {
    "memory_id", "pattern_key", "type", "content", "confidence", "occurrence_count", "action",
}


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    """Planning, PEARL and memory writes read one frozen clock: Monday 08:00 in India."""
    clock.freeze(MONDAY_8AM, schedule_endpoint, task_service)


def _frames(turn, event: str) -> list[dict]:
    return [data for name, data in turn.events if name == event]


def _position(turn, event: str) -> int:
    return [name for name, _ in turn.events].index(event)


def _remember_on(llm, rules: dict) -> None:
    """When what the user said contains a trigger, the extraction returns that memory."""

    def reply(req) -> str:
        message = said(req)
        memories = [memory for trigger, memory in rules.items() if trigger in message]
        return brain_dump(memories=memories) if memories else "{}"

    llm.script("brain_dump_extraction", reply)


async def _seed_skips_at_nine(jarvis, user: str = "alice") -> None:
    """Three 09:00 slots in a row the user set aside, as TaskService records them."""
    events = []
    for i in range(3):
        slot = datetime(2026, 11, 5 + i, 9, 0, tzinfo=IST)
        events.append(TaskEvent(
            user_id=user, task_id=f"past_{i}", event_type=TaskEventType.SKIPPED,
            occurred_at=slot + timedelta(minutes=25),
            scheduled_start=slot, scheduled_end=slot + timedelta(minutes=25),
            metadata={"title": f"Morning task {i}", "goal_id": "past", "deadline_hint": None,
                      "reason": None},
        ))
    await jarvis.state.container.repos.task_events.append_many(user, events)


def _memories(jarvis, user: str = "alice") -> list[dict]:
    return jarvis.rows("user_memories", user_id=user)


# --- one model call fewer ------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn__non_trivial_message__no_separate_memory_extraction_call(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(memories=[MARATHON]))

    await jarvis.say("I'm training for the Mumbai marathon in March")

    assert llm.tasks() == ["brain_dump_extraction", "voice_of_jarvis"]


# --- learning moments, before complete ----------------------------------------------


@pytest.mark.asyncio
async def test_turn__stated_preference__memory_extracted_frame_before_complete(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(memories=[MORNINGS]))

    turn = await jarvis.say("I prefer studying in the mornings")

    (row,) = _memories(jarvis)
    (frame,) = _frames(turn, "memory_extracted")
    assert frame == {
        "memory_id": row["id"],
        "type": "preference",
        "content": MORNINGS["content"],
        "confidence": 0.8,
        "provenance": "said",
        "action": "created",
    }
    assert _position(turn, "memory_extracted") < _position(turn, "complete")
    # Nothing learned from behaviour, nothing to re-plan for.
    assert turn.payload["pearl_insights"] is None
    assert turn.payload["suggested_action"] is None


@pytest.mark.asyncio
async def test_turn__pattern_change__pattern_detected_frame_before_complete(jarvis, llm):
    await _seed_skips_at_nine(jarvis)

    turn = await jarvis.say("Anything I should know about my week?")

    (pattern,) = _memories(jarvis)
    (frame,) = _frames(turn, "pattern_detected")
    assert set(frame) == PATTERN_FRAME_KEYS
    assert (frame["memory_id"], frame["pattern_key"], frame["action"]) == (
        pattern["id"], "skip_time_window:9", "created"
    )
    assert frame["content"]
    assert _position(turn, "pattern_detected") < _position(turn, "complete")


@pytest.mark.asyncio
async def test_turn__memory__provenance_said_with_session_and_message_ids(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(memories=[MARATHON]))

    turn = await jarvis.say("I'm training for the Mumbai marathon in March")

    (row,) = _memories(jarvis)
    (message,) = [
        m for m in jarvis.rows("chat_messages", session_id=turn.conversation_id)
        if m["role"] == "user"
    ]
    assert row["provenance"] == "said"
    assert row["source_session_id"] == turn.conversation_id
    assert row["source_message_id"] == message["id"]


# --- the memory lifecycle, from the turn ---------------------------------------------


@pytest.mark.asyncio
async def test_turn__forgotten_fact_said_again__no_frame_no_row(jarvis, llm):
    _remember_on(llm, {"marathon": MARATHON})
    await jarvis.say("I'm training for the Mumbai marathon in March")
    (memory,) = _memories(jarvis)
    await jarvis.state.container.memory_service.forget("alice", memory["id"])

    turn = await jarvis.say("I'm training for the Mumbai marathon in March")

    assert _frames(turn, "memory_extracted") == []
    (tombstone,) = _memories(jarvis)
    assert (tombstone["id"], tombstone["status"], tombstone["content"]) == (
        memory["id"], "forgotten", ""
    )
    # Not offered back to the model as something Jarvis knows, either.
    last_extraction = llm.calls_for("brain_dump_extraction")[-1]
    assert last_extraction.prompt == "I'm training for the Mumbai marathon in March"


def _handle_of(prompt: str, memory: dict) -> str | None:
    match = re.search(
        rf"^\[(m\d+)\] \({memory['type']}\) {re.escape(memory['content'])}$",
        prompt,
        flags=re.MULTILINE,
    )
    return match.group(1) if match else None


@pytest.mark.asyncio
async def test_turn__contradiction_handle__supersedes_that_memory(jarvis, llm):
    def extract(req) -> str:
        message = said(req)
        if "late at night" in message:
            replacement = {
                "type": "preference", "content": NIGHTS, "confidence": 0.8,
                "contradicts": _handle_of(req.prompt, MORNINGS),
            }
            return brain_dump(memories=[replacement])
        return brain_dump(memories=[MORNINGS]) if "mornings" in message else "{}"

    llm.script("brain_dump_extraction", extract)
    await jarvis.say("I prefer studying in the mornings")
    (old,) = _memories(jarvis)

    turn = await jarvis.say("Actually I've switched: I study best late at night now")

    (frame,) = _frames(turn, "memory_extracted")
    assert (frame["action"], frame["content"]) == ("superseded", NIGHTS)
    (old_now,) = [m for m in _memories(jarvis) if m["id"] == old["id"]]
    assert old_now["superseded_by"] == frame["memory_id"]


@pytest.mark.parametrize("citation", ["m9", "the old memory's own id"])
@pytest.mark.asyncio
async def test_turn__unknown_handle__ignored(jarvis, llm, citation):
    ids: list[str] = []

    def extract(req) -> str:
        message = said(req)
        if "late at night" in message:
            cited = ids[0] if citation == "the old memory's own id" else citation
            replacement = {
                "type": "preference", "content": NIGHTS, "confidence": 0.8, "contradicts": cited,
            }
            return brain_dump(memories=[replacement])
        return brain_dump(memories=[MORNINGS]) if "mornings" in message else "{}"

    llm.script("brain_dump_extraction", extract)
    await jarvis.say("I prefer studying in the mornings")
    (old,) = _memories(jarvis)
    ids.append(old["id"])

    turn = await jarvis.say("Also, I study best late at night")

    (frame,) = _frames(turn, "memory_extracted")
    assert frame["action"] == "created"
    (old_now,) = [m for m in _memories(jarvis) if m["id"] == old["id"]]
    assert old_now["superseded_by"] is None


@pytest.mark.asyncio
async def test_turn__persist_past_cap__frames_for_what_landed_and_turn_completes(
    jarvis, llm, monkeypatch
):
    pune = {"type": "fact", "content": "Lives in Pune with two cats", "confidence": 0.7}
    llm.script("brain_dump_extraction", brain_dump(memories=[MARATHON, pune]))
    service = jarvis.state.container.memory_service
    remember = service.remember

    async def stalls_on_pune(user_id, candidate, **kwargs):
        if candidate.content == pune["content"]:
            await asyncio.sleep(30)  # a database that stopped answering
        return await remember(user_id, candidate, **kwargs)

    monkeypatch.setattr(service, "remember", stalls_on_pune)
    monkeypatch.setattr(observation, "MEMORY_PERSIST_TIMEOUT_S", 0.3)
    started = time.monotonic()

    turn = await jarvis.say("I'm training for the Mumbai marathon in March, and I live in Pune")

    assert time.monotonic() - started < 5
    assert [f["content"] for f in _frames(turn, "memory_extracted")] == [MARATHON["content"]]
    assert [m["content"] for m in _memories(jarvis)] == [MARATHON["content"]]
    assert turn.reply == "Very good, sir."


@pytest.mark.asyncio
async def test_turn__greeting__no_extraction_and_no_learning_frames(jarvis, llm):
    await _seed_skips_at_nine(jarvis)  # PEARL would have something to say on a real turn

    turn = await jarvis.say("hi")

    assert llm.tasks() == ["voice_of_jarvis"]
    assert _frames(turn, "memory_extracted") == []
    assert _frames(turn, "pattern_detected") == []
    assert _memories(jarvis) == []


# --- what complete adds -------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete__saved_constraint__suggested_action_replan(jarvis, llm):
    rule = "never schedule work before 11 AM"
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[rule]))

    turn = await jarvis.say(f"Please {rule}")

    assert turn.payload["saved_constraints"] == [rule]
    assert turn.payload["suggested_action"] == "replan"


@pytest.mark.asyncio
async def test_complete__infeasible_exhausted__schedule_status_and_conflict_summary(jarvis, llm):
    # Two tasks that each wait for the other: infeasible at every rung of the ladder.
    cyclic = decomposition(["Write intro", "Write conclusion"]).replace(
        '"dependencies": []', '"dependencies": ["task_2"]', 1
    ).replace('"dependencies": []', '"dependencies": ["task_1"]', 1)
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Write my essay") if "Plan" in said(req) else "{}",
    )
    llm.script("socratic_chunker", cyclic)

    turn = await jarvis.say("Plan my essay")

    assert turn.payload["schedule_status"] == "infeasible"
    assert "scope problem, not a you problem" in turn.payload["conflict_summary"]
    assert turn.payload["conflict_summary"] == turn.reply
    assert turn.draft_id is None


@pytest.mark.asyncio
async def test_complete__confirmed_pattern__pearl_insights_listed(jarvis, llm):
    await _seed_skips_at_nine(jarvis)

    turn = await jarvis.say("Anything I should know about my week?")

    (pattern,) = _memories(jarvis)
    (insight,) = turn.payload["pearl_insights"]
    assert set(insight) == {"insight", "confidence", "occurrences", "memory_id"}
    assert (insight["memory_id"], insight["confidence"], insight["occurrences"]) == (
        pattern["id"], 1.0, 3
    )
    assert "9 AM" in insight["insight"]
