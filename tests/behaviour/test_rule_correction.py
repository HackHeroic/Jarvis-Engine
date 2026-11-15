"""Correcting a rule replaces it.

"I go to the gym every weekday at 6 PM" stores a rule and the memory of saying it, linked.
"Actually I moved my gym to 7 AM" supersedes that memory; the rule linked to it goes with
it (and its check-ins), so one gym rule remains: the new one, in the memory's cleaned
words, not the user's raw sentence. A correction of a plain fact deletes no rule.

The real chat turn over the fake database and a scripted model. The extraction cites the
handle of the memory a correction replaces, read off the known memories in its prompt,
as a real extractor does; the correction's wording does not need to name the old value.
"""

from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.domain.clock as utc_clock
import app.services.planning.scheduling as scheduling
from app.services.memory.stated_rules import rule_memory_id
from tests.behaviour.conftest import brain_dump, decomposition, said

IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
GYM_6PM_SAID = "I go to the gym every weekday at 6 PM"
GYM_6PM = "Goes to the gym every weekday at 6 PM"
GYM_7AM = "Goes to the gym at 7 AM on weekdays"
MOVED = "Actually I moved my gym to 7 AM on weekdays, not 6 PM anymore."
MOVED_NO_OLD_VALUE = "My gym is at 7 AM on weekdays now"
RAO = "Dr. Rao teaches Computer Networks"
IYER = "Dr. Iyer teaches Computer Networks now"
# Horizon minutes (0 = 08:00 local): 07:00-08:00 and 18:00-19:00.
SEVEN_AM = (1380, 1440)
SIX_PM = (600, 660)


def _handle_of(prompt: str, content: str) -> str | None:
    import re

    match = re.search(rf"^\[(m\d+)\] \(\w+\) {re.escape(content)}$", prompt, flags=re.MULTILINE)
    return match.group(1) if match else None


def _extract(req) -> str:
    message = said(req)
    if message == GYM_6PM_SAID:
        return brain_dump(
            inline_habits=[GYM_6PM_SAID],
            memories=[{"type": "constraint", "content": GYM_6PM, "confidence": 0.95}],
        )
    if message in (MOVED, MOVED_NO_OLD_VALUE):
        return brain_dump(
            inline_habits=[message],
            memories=[{
                "type": "constraint", "content": GYM_7AM, "confidence": 0.95,
                "contradicts": _handle_of(req.prompt, GYM_6PM),
            }],
        )
    if message == "Networks is taught by Dr. Rao":
        return brain_dump(memories=[{"type": "fact", "content": RAO, "confidence": 0.9}])
    if message == "Correction: Dr. Iyer teaches Networks now":
        return brain_dump(memories=[{
            "type": "fact", "content": IYER, "confidence": 0.9,
            "contradicts": _handle_of(req.prompt, RAO),
        }])
    if "plan" in message.lower():
        return brain_dump(planning_goal="Revise DSA")
    return "{}"


def _translate(req) -> dict:
    """The habit translator, reading each rule's clock words (the words name their days,
    so planning asks the model)."""
    slots = []
    for line in req.prompt.splitlines():
        if "7 AM" in line:
            slots.append({"name": "gym_7am", "start_min": SEVEN_AM[0], "end_min": SEVEN_AM[1],
                          "availability": "blocked"})
        if "6 PM" in line:
            slots.append({"name": "gym_6pm", "start_min": SIX_PM[0], "end_min": SIX_PM[1],
                          "availability": "blocked"})
    return {"semantic_slots": slots}


@pytest.fixture
def scripted(llm):
    llm.script("brain_dump_extraction", _extract)
    llm.script("habit_translation", _translate)
    llm.script("socratic_chunker", decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))
    return llm


@asynccontextmanager
async def _api(jarvis, monkeypatch):
    from app.main import app

    monkeypatch.setattr(app.state, "container", jarvis.state.container, raising=False)
    monkeypatch.setattr(app.state, "db_client", jarvis.state.db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _rules(jarvis, monkeypatch) -> list[str]:
    """The rules the You page lists."""
    async with _api(jarvis, monkeypatch) as api:
        response = await api.get("/api/v1/you", params={"user_id": "alice"})
    assert response.status_code == 200, response.text
    return [rule["text"] for rule in response.json()["rules"]]


def _memory_id(jarvis, content: str) -> str:
    (row,) = jarvis.rows("user_memories", user_id="alice", content=content)
    return row["id"]


@pytest.mark.asyncio
async def test_chat__user_moves_gym_from_6pm_to_7am__one_gym_rule_remains_and_it_is_the_new_one(
    jarvis, scripted, monkeypatch
):
    await jarvis.say(GYM_6PM_SAID)
    (old_rule,) = jarvis.rows("behavioral_constraints", user_id="alice")
    assert len(jarvis.rows("habit_trackers", user_id="alice")) == 1

    turn = await jarvis.say(MOVED)

    rules = await _rules(jarvis, monkeypatch)
    assert [text for text in rules if "gym" in text.lower()] == [GYM_7AM]
    (new_rule,) = jarvis.rows("behavioral_constraints", user_id="alice")
    assert new_rule["id"] != old_rule["id"]
    assert rule_memory_id(new_rule) == _memory_id(jarvis, GYM_7AM)  # linked to the new memory
    (tracker,) = jarvis.rows("habit_trackers", user_id="alice")  # the old rule's check-ins went
    assert tracker["constraint_id"] == new_rule["id"]
    assert turn.payload["saved_constraints"] == [MOVED]  # the reply keeps the user's words


@pytest.mark.asyncio
async def test_chat__correction_worded_without_the_old_value__old_rule_still_goes(
    jarvis, scripted, monkeypatch
):
    await jarvis.say(GYM_6PM_SAID)

    await jarvis.say(MOVED_NO_OLD_VALUE)

    assert await _rules(jarvis, monkeypatch) == [GYM_7AM]


@pytest.mark.asyncio
async def test_chat__correction_of_a_plain_fact__no_rule_deleted(jarvis, scripted, monkeypatch):
    await jarvis.say(GYM_6PM_SAID)
    await jarvis.say("Networks is taught by Dr. Rao")
    before = jarvis.rows("behavioral_constraints", user_id="alice")

    await jarvis.say("Correction: Dr. Iyer teaches Networks now")

    (rao,) = jarvis.rows("user_memories", user_id="alice", content=RAO)
    assert rao["superseded_by"] == _memory_id(jarvis, IYER)  # the fact was corrected
    assert jarvis.rows("behavioral_constraints", user_id="alice") == before
    assert await _rules(jarvis, monkeypatch) == [GYM_6PM]


def _covers(slot, window: tuple[int, int]) -> bool:
    """Whether a time slot keeps any of ``window`` (horizon minutes, on any day) from work."""
    start, end = slot.start_min % 1440, slot.start_min % 1440 + (slot.end_min - slot.start_min)
    return slot.availability != "free" and start < window[1] and window[0] < end


@pytest.mark.asyncio
async def test_chat__after_the_correction__a_new_plan_keeps_7am_blocked_and_6pm_free(
    jarvis, scripted, clock, monkeypatch
):
    """The real planning path and CP-SAT. The assertion is on the slots the solver is
    given: 06:00-08:00 is the night anyway, and the solver need not use a free 18:00."""
    clock.freeze(MONDAY_8AM, scheduling, utc_clock)
    solver_input: list = []
    real_run_schedule = scheduling.run_schedule

    def spy(graph, daily_context, *args, **kwargs):
        solver_input.append(list(daily_context))
        return real_run_schedule(graph, daily_context, *args, **kwargs)

    monkeypatch.setattr(scheduling, "run_schedule", spy)
    await jarvis.say(GYM_6PM_SAID)
    await jarvis.say(MOVED)
    since = len(scripted.calls)

    plan = await jarvis.say("Plan my DSA revision")

    assert plan.payload["schedule"], plan.reply
    (translation,) = [c for c in scripted.calls[since:] if c.task == "habit_translation"]
    assert GYM_7AM in translation.prompt and "6 PM" not in translation.prompt
    slots = solver_input[-1]
    assert any(_covers(slot, SEVEN_AM) for slot in slots), slots
    assert not [slot for slot in slots if _covers(slot, SIX_PM)], slots
