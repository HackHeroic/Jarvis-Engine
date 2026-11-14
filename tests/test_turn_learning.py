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
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.core.observation as observation
import app.domain.clock as utc_clock
from app.domain.entities import TaskEvent, TaskEventType
from app.schemas.context import BrainDumpExtraction
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
    clock.freeze(MONDAY_8AM, schedule_endpoint, utc_clock)


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


async def _seed_skips_at_nine(jarvis, user: str = "alice", days: int = 3) -> None:
    """``days`` 09:00 slots in a row the user set aside, as TaskService records them."""
    events = []
    for i in range(days):
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
    started: list[str] = []

    async def slow_database(user_id, candidate, **kwargs):
        started.append(candidate.content)
        await asyncio.sleep(0.5)  # slower than the whole cap, but it answers
        return await remember(user_id, candidate, **kwargs)

    monkeypatch.setattr(service, "remember", slow_database)
    monkeypatch.setattr(observation, "MEMORY_PERSIST_TIMEOUT_S", 0.3)

    turn = await jarvis.say("I'm training for the Mumbai marathon in March, and I live in Pune")

    # The write in flight when the cap passed finished and is told; the next never began.
    assert started == [MARATHON["content"]]
    assert [f["content"] for f in _frames(turn, "memory_extracted")] == [MARATHON["content"]]
    assert [m["content"] for m in _memories(jarvis)] == [MARATHON["content"]]
    assert turn.reply == "Very good, sir."


@pytest.mark.asyncio
async def test_turn__contradiction_in_flight_at_the_cap__supersede_is_never_half_written(
    jarvis, llm, monkeypatch
):
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
    repository = jarvis.state.container.repos.memories
    update = repository.update

    async def slow_link(user_id, memory_id, **fields):
        if "superseded_by" in fields:  # between storing the new fact and retiring the old
            await asyncio.sleep(0.5)
        return await update(user_id, memory_id, **fields)

    monkeypatch.setattr(repository, "update", slow_link)
    monkeypatch.setattr(observation, "MEMORY_PERSIST_TIMEOUT_S", 0.2)

    turn = await jarvis.say("Actually I've switched: I study best late at night now")

    (frame,) = _frames(turn, "memory_extracted")
    assert (frame["action"], frame["content"]) == ("superseded", NIGHTS)
    in_use = [m for m in _memories(jarvis) if m["superseded_by"] is None]
    assert [m["content"] for m in in_use] == [NIGHTS]  # never both the old and the new
    (old_now,) = [m for m in _memories(jarvis) if m["id"] == old["id"]]
    assert old_now["superseded_by"] == frame["memory_id"]


@pytest.mark.asyncio
async def test_turn__write_outlasts_its_bound__turn_goes_on_and_the_write_still_lands(
    jarvis, llm, monkeypatch
):
    llm.script("brain_dump_extraction", brain_dump(memories=[MARATHON]))
    service = jarvis.state.container.memory_service
    remember = service.remember

    async def stalled_database(user_id, candidate, **kwargs):
        await asyncio.sleep(1.0)
        return await remember(user_id, candidate, **kwargs)

    monkeypatch.setattr(service, "remember", stalled_database)
    monkeypatch.setattr(observation, "MEMORY_WRITE_TIMEOUT_S", 0.2)

    turn = await jarvis.say("I'm training for the Mumbai marathon in March")

    # The turn stopped waiting, so nothing was announced; the write was never cut short.
    assert _frames(turn, "memory_extracted") == []
    assert turn.reply == "Very good, sir."
    assert [m["content"] for m in _memories(jarvis)] == [MARATHON["content"]]


@pytest.mark.asyncio
async def test_turn__one_invalid_memory__plan_kept_and_the_valid_memory_learned(jarvis, llm):
    memories = [
        {"type": "goal", "content": "Ace the DSA exam on Friday", "confidence": 1.7},
        {"type": "habit", "content": "Not a memory type at all"},
    ]
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA", memories=memories)
        if "DSA" in said(req) else "{}",
    )
    llm.script("socratic_chunker", decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))

    turn = await jarvis.say("Plan my DSA revision, I want to ace the exam on Friday")

    assert turn.payload["schedule_status"] == "draft"
    (frame,) = _frames(turn, "memory_extracted")
    assert (frame["content"], frame["confidence"]) == ("Ace the DSA exam on Friday", 1.0)


@pytest.mark.asyncio
async def test_turn__pattern_learned_from_a_skip_between_turns__moment_on_the_next_turn(
    jarvis, llm
):
    container = jarvis.state.container
    await _seed_skips_at_nine(jarvis, days=2)
    slot = datetime(2026, 11, 7, 9, 0, tzinfo=IST)
    jarvis.db.table("user_tasks").insert({
        "id": "row-3", "user_id": "alice", "task_id": "past_2", "goal_id": "past",
        "title": "Morning task 2", "status": "pending", "duration_minutes": 25,
        "scheduled_start": slot.isoformat(),
        "scheduled_end": (slot + timedelta(minutes=25)).isoformat(),
    }).execute()

    await container.task_service.skip("alice", "past_2")  # PEARL runs behind the outcome
    await container.pattern_observer.settle()
    assert [m["content"] for m in _memories(jarvis)]  # learned between turns...

    turn = await jarvis.say("Anything I should know about my week?")
    after = await jarvis.say("Anything else I should know about my week?")

    # ...and told on the next turn, once, with the numbers the toast gate reads.
    (frame,) = _frames(turn, "pattern_detected")
    assert (frame["pattern_key"], frame["action"]) == ("skip_time_window:9", "created")
    assert isinstance(frame["confidence"], float) and frame["confidence"] >= 0.7
    assert isinstance(frame["occurrence_count"], int) and frame["occurrence_count"] >= 3
    assert _frames(after, "pattern_detected") == []


# --- the extraction schema: one bad memory never costs the turn its plan -------------


def test_brain_dump__memory_confidence_out_of_range__clamped_and_goal_kept():
    extraction = BrainDumpExtraction.model_validate_json(json.dumps({
        "planning_goal": "Revise DSA",
        "memories": [
            {"type": "goal", "content": "Ace the exam", "confidence": 1.4},
            {"type": "fact", "content": "Lives in Pune", "confidence": -0.2},
        ],
    }))

    assert extraction.planning_goal == "Revise DSA"
    assert [m.confidence for m in extraction.memories] == [1.0, 0.0]


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "habit", "content": "Not a memory type"},
        {"type": "fact"},
        {"type": "fact", "content": "Confidence in words", "confidence": "very"},
        "just a string",
    ],
    ids=["unknown_type", "no_content", "confidence_not_a_number", "not_an_object"],
)
def test_brain_dump__invalid_memory__dropped_alone_and_goal_kept(bad):
    extraction = BrainDumpExtraction.model_validate_json(json.dumps({
        "planning_goal": "Revise DSA",
        "memories": [bad, {"type": "goal", "content": "Ace the exam", "confidence": 0.8}],
    }))

    assert extraction.planning_goal == "Revise DSA"
    assert [m.content for m in extraction.memories] == ["Ace the exam"]


def test_brain_dump__memories_not_a_list__no_memories_and_goal_kept():
    extraction = BrainDumpExtraction.model_validate_json(
        json.dumps({"planning_goal": "Revise DSA", "memories": {"type": "goal"}})
    )

    assert (extraction.planning_goal, extraction.memories) == ("Revise DSA", [])


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


# --- a durable fact said as a habit is remembered too -----------------------------------
#
# The model may file "never schedule study before 11 AM" or "I'm a night owl" under
# inline_habits alone. Remembering never depends on how it splits the fields: every habit
# no extracted memory already covers is remembered as a said memory.

RULE = "From now on never schedule any study before 11 AM, I'm useless in the mornings"
NIGHT_OWL = "I'm a night owl and do my best work after 9 pm"


def _context_of_reply(llm, since: int) -> str:
    """What the reply was told about the user: its system notes after the history."""
    (reply_call,) = [c for c in llm.calls[since:] if c.task == "voice_of_jarvis"]
    return "\n".join(
        m["content"] for m in reply_call.conversation_history if m["role"] == "system"
    )


@pytest.mark.asyncio
async def test_turn__rule_only_in_inline_habits__remembered_as_a_said_constraint(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[RULE]))

    turn = await jarvis.say(RULE)

    (row,) = _memories(jarvis)
    assert (row["memory_type"], row["content"], row["provenance"]) == ("constraint", RULE, "said")
    assert row["source_session_id"] == turn.conversation_id
    (frame,) = _frames(turn, "memory_extracted")
    assert frame == {
        "memory_id": row["id"], "type": "constraint", "content": RULE, "confidence": 0.8,
        "provenance": "said", "action": "created",
    }
    assert _position(turn, "memory_extracted") < _position(turn, "complete")


@pytest.mark.asyncio
async def test_turn__self_description_only_in_inline_habits__remembered_as_a_preference(
    jarvis, llm
):
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[NIGHT_OWL]))

    turn = await jarvis.say(f"Something about me: {NIGHT_OWL}.")

    (row,) = _memories(jarvis)
    assert (row["memory_type"], row["content"]) == ("preference", NIGHT_OWL)
    (frame,) = _frames(turn, "memory_extracted")
    assert (frame["type"], frame["action"]) == ("preference", "created")


@pytest.mark.asyncio
async def test_turn__habit_also_extracted_as_a_memory__one_frame_and_one_row(jarvis, llm):
    rule = {"type": "constraint", "content": "Never schedule study before 11 AM", "confidence": 0.9}
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[RULE], memories=[rule]))

    turn = await jarvis.say(RULE)

    assert [m["content"] for m in _memories(jarvis)] == [rule["content"]]
    assert [f["content"] for f in _frames(turn, "memory_extracted")] == [rule["content"]]


def _rules(jarvis, user: str = "alice") -> list[dict]:
    return jarvis.rows("behavioral_constraints", user_id=user)


@pytest.mark.asyncio
async def test_turn__rule_stated__the_rule_carries_the_id_of_its_memory(jarvis, llm):
    """The turn stores a rule and a constraint memory: linked, so planning plans from the
    rule alone and letting go of either lets go of both."""
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[RULE]))

    await jarvis.say(RULE)

    (memory,) = _memories(jarvis)
    (rule,) = _rules(jarvis)
    assert rule["raw_text"] == RULE
    assert rule["structured_semantics"]["memory_id"] == memory["id"]


@pytest.mark.asyncio
async def test_turn__rule_also_extracted_as_a_memory__linked_to_that_memory(jarvis, llm):
    rule = {"type": "constraint", "content": "Never schedule study before 11 AM", "confidence": 0.9}
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[RULE], memories=[rule]))

    await jarvis.say(RULE)

    (memory,) = _memories(jarvis)
    (stored,) = _rules(jarvis)
    assert stored["structured_semantics"]["memory_id"] == memory["id"]


@pytest.mark.asyncio
async def test_turn__rule_said_twice__both_rules_linked_to_the_one_memory(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[RULE]))
    await jarvis.say(RULE)

    await jarvis.say(RULE)

    (memory,) = _memories(jarvis)
    assert [r["structured_semantics"]["memory_id"] for r in _rules(jarvis)] == [memory["id"]] * 2


@pytest.mark.asyncio
async def test_turn__rule_said_again_as_a_habit__reinforced_not_stored_twice(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[RULE]))
    await jarvis.say(RULE)

    again = await jarvis.say(RULE)

    (row,) = _memories(jarvis)
    (frame,) = _frames(again, "memory_extracted")
    assert (frame["memory_id"], frame["action"]) == (row["id"], "reinforced")


@pytest.mark.asyncio
async def test_turn__forgotten_rule_said_again_as_a_habit__no_frame_no_row(jarvis, llm):
    llm.script("brain_dump_extraction", brain_dump(inline_habits=[RULE]))
    await jarvis.say(RULE)
    (memory,) = _memories(jarvis)
    await jarvis.state.container.memory_service.forget("alice", memory["id"])

    turn = await jarvis.say(RULE)

    assert _frames(turn, "memory_extracted") == []
    (tombstone,) = _memories(jarvis)
    assert (tombstone["id"], tombstone["status"], tombstone["content"]) == (
        memory["id"], "forgotten", ""
    )


@pytest.mark.asyncio
async def test_turn__rule_from_inline_habits__recalled_in_a_new_conversation(jarvis, llm):
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(inline_habits=[RULE]) if "never schedule" in said(req) else "{}",
    )
    first = await jarvis.say(RULE)
    calls_before = len(llm.calls)

    second = await jarvis.say("What should I focus on this week?")

    assert second.conversation_id != first.conversation_id
    assert RULE in _context_of_reply(llm, calls_before)
    assert RULE in [m["content"] for m in second.payload["memories"]]


def test_brain_dump_prompt__memories_first__never_exclusive_of_inline_habits():
    from app.services.analytical.control_policy import BRAIN_DUMP_EXTRACTION_PROMPT as prompt

    assert isinstance(prompt, str)
    # Read before the field list a low-effort model skims, with worked examples that fill
    # inline_habits AND memories from one sentence (a self-description, a standing rule).
    assert prompt.index("memories") < prompt.index("planning_goal:")
    both = [
        line for line in prompt.splitlines()
        if line.startswith("- '") and "inline_habits: [" in line and "memories: [{" in line
    ]
    assert len(both) >= 2
    # Nothing tells the model a habit cannot also be a memory.
    for text in (prompt, BrainDumpExtraction.model_fields["memories"].description):
        assert "repeat of inline_habits" not in text


# --- what one turn costs the store ------------------------------------------------------


@pytest.mark.asyncio
async def test_turn__store_reads__memories_listed_once_pearl_looks_once_profile_read_once(
    jarvis, llm, monkeypatch
):
    """A turn reads the user's memories once (recall, the extraction's known memories and
    the suppression check share it), and PEARL looks once (the learning moments and the
    complete frame's insights share it), on the profile the turn already read."""
    from collections import Counter

    await _seed_skips_at_nine(jarvis)
    first = await jarvis.say("Anything I should know about my week?")
    patterns = jarvis.state.container.patterns
    detections: list[str] = []
    detect = patterns._detect

    async def counted(user_id, *args, **kwargs):
        detections.append(user_id)
        return await detect(user_id, *args, **kwargs)

    monkeypatch.setattr(patterns, "_detect", counted)
    jarvis.db.selects.clear()

    second = await jarvis.say("And how is next week looking?", session=first.conversation_id)

    reads = Counter(table for table, _ in jarvis.db.selects)
    assert reads["user_memories"] == 2, "the turn's one read, and PEARL's pattern memories"
    assert reads["user_profiles"] == 1
    assert reads["task_events"] == 1
    assert detections == ["alice"]
    (insight,) = second.payload["pearl_insights"]
    assert "9 AM" in insight["insight"]
