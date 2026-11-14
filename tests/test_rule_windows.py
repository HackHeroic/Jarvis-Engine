"""What a stated rule keeps free, read off its words with no model call.

"Never work after 10 PM" blocks 22:00 onwards, through the night: not the day before
22:00. Negation blocks the window the words name; "only" blocks the rest of the day;
availability ("I'm free after 5 PM") and vague words ("after lunch") block nothing
here. One function reads every rule (``blocked_windows``): the habit translator, the
memory bridge and the anticipation consent check all use it.

Windows are horizon minutes: 0 is 08:00 local, 840 is 22:00, 960 midnight, 1440 is
08:00 the next morning (the end of the day, night included).
"""

from uuid import uuid4

import pytest

from app.services.planning.rule_windows import blocked_windows

DAY_END = 1440  # 08:00 the next morning: "to the end of the day" runs through the night

SEMANTICS = [
    # Negation: the window the words name is blocked.
    ("Never work after 10 PM", [(840, DAY_END)]),
    ("no work after 10 PM", [(840, DAY_END)]),
    ("Don't schedule anything after 10pm", [(840, DAY_END)]),
    ("Avoid work after 10 p.m.", [(840, DAY_END)]),
    ("Nothing after 22:00", [(840, DAY_END)]),
    ("Not after 10:30 PM, please", [(870, DAY_END)]),
    ("never work past midnight", [(960, DAY_END)]),
    ("No work after 12 AM", [(960, DAY_END)]),
    ("never before 11 AM", [(0, 180)]),
    ("Never schedule work before 11am", [(0, 180)]),
    ("no meetings until noon", [(0, 240)]),
    ("never before noon", [(0, 240)]),
    ("Never schedules anything before 7 AM", []),  # nothing schedulable before 08:00
    ("no work between 1 and 2 PM", [(300, 360)]),
    ("No work from 1 PM to 2 PM", [(300, 360)]),
    ("No tasks between 14:00 and 15:00", [(360, 420)]),
    ("No work before 9 AM or after 10 PM", [(0, 60), (840, DAY_END)]),
    ("Keep evenings after 9 PM free", [(780, DAY_END)]),
    ("I'm not available before 10 AM", [(0, 120)]),
    # Restriction: the window is allowed, the rest of the day blocked.
    ("only after 6 PM", [(0, 600)]),
    ("I work only after 6 PM", [(0, 600)]),
    ("I just work after 18:00", [(0, 600)]),
    ("only after 12 PM", [(0, 240)]),
    ("only before noon", [(240, DAY_END)]),
    ("I work only before 12 PM", [(240, DAY_END)]),
    ("only before midnight", [(960, DAY_END)]),
    ("I only work between 9 AM and 5 PM", [(0, 60), (540, DAY_END)]),
    ("I'm only free after 5 PM", [(0, 540)]),
    # Availability, a statement of when work goes well, or no rule word: no block.
    ("I'm free after 5 PM", []),
    ("available after 5", []),
    ("I'm a night owl and do my best work after 9 pm", []),
    ("I study after 10 PM", []),
    # Vague: no clock time, no deterministic window (the model reads these).
    ("after lunch", []),
    ("Only after lunch", []),
    ("I hate mornings", []),
    ("Revise chapters 2-4 this week", []),
    # A bare hour is read the way that blocks less.
    ("no deep work after 6", [(600, DAY_END)]),
    ("never work after 10", [(840, DAY_END)]),
    ("never before 9", [(0, 60)]),
    ("never before 6", []),
    # A fixed activity occupies its range.
    ("Gym from 6 to 7 pm", [(600, 660)]),
    ("Meeting from 2 PM to 3 PM", [(360, 420)]),
]


@pytest.mark.parametrize(("text", "expected"), SEMANTICS, ids=[t for t, _ in SEMANTICS])
def test_blocked_windows__stated_rule__blocks_what_the_words_mean(text, expected):
    assert blocked_windows(text) == expected


# --- the habit translator: a stated rule's slots, deterministic where the words are clear


@pytest.fixture
def translator(monkeypatch):
    """The habit translator with an empty cache and a scripted model (no reply scripted:
    any model call fails the test unless the test scripts one)."""
    import app.services.analytical.habit_translator as module
    from app.infrastructure.llm.chain import FallbackChainProvider
    from app.infrastructure.llm.factory import set_chain
    from tests.behaviour.conftest import ScriptedLLM

    monkeypatch.setattr(module, "_habit_cache", {})
    model = ScriptedLLM()
    set_chain(FallbackChainProvider([model]))  # conftest's _fresh_llm_chain resets it
    yield module, model


def _windows(slots) -> list[tuple[int, int, str, str]]:
    return [(s.start_min, s.end_min, s.availability, s.recurrence) for s in slots]


@pytest.mark.parametrize(
    ("text", "expected"),
    [(t, e) for t, e in SEMANTICS if e],
    ids=[t for t, e in SEMANTICS if e],
)
@pytest.mark.asyncio
async def test_translate_habits__clear_rule__its_windows_blocked_daily_without_a_model(
    translator, text, expected
):
    module, model = translator

    slots = await module.translate_habits_to_slots(text)

    assert _windows(slots) == [(s, e, "blocked", "daily") for s, e in expected]
    assert model.calls == []


@pytest.mark.asyncio
async def test_translate_habits__never_after_10_pm__the_night_not_the_day(translator):
    """The reported bug: the rule the You page suggests blocked 08:00-22:00."""
    module, model = translator

    (slot,) = await module.translate_habits_to_slots("no work after 10 PM")

    assert (slot.start_min, slot.end_min) == (840, DAY_END)
    assert model.calls == []


@pytest.mark.parametrize(
    "text",
    ["I'm free after 5 PM", "after lunch", "No work after 10 PM on weekdays", "I hate mornings"],
)
@pytest.mark.asyncio
async def test_translate_habits__availability_vague_or_its_own_days__left_to_the_model(
    translator, text
):
    module, model = translator
    model.script("habit_translation", {"semantic_slots": []})

    assert await module.translate_habits_to_slots(text) == []
    assert model.tasks()[0] == "habit_translation"
    assert model.calls[0].prompt == text


@pytest.mark.asyncio
async def test_translate_habits__one_clear_rule_and_one_vague__only_the_vague_one_to_the_model(
    translator,
):
    module, model = translator
    swim = {"name": "swim", "start_min": 600, "end_min": 660, "availability": "blocked",
            "recurrence": "weekly", "weekday": 5}
    model.script("habit_translation", {"semantic_slots": [swim]})

    slots = await module.translate_habits_to_slots("Never work after 10 PM\nI swim on Saturdays")

    assert [c.prompt for c in model.calls] == ["I swim on Saturdays"]
    assert sorted((s.start_min, s.end_min) for s in slots) == [(600, 660), (840, DAY_END)]
    assert "swim" in {s.name for s in slots}


# --- the memory bridge and the consent check read the same words the same way ---------


def _constraint_memory(content: str):
    from datetime import datetime, timezone

    from app.domain.entities import Memory, MemorySource

    now = datetime(2026, 11, 16, 2, 30, tzinfo=timezone.utc)
    return Memory(
        id=str(uuid4()), user_id="u1", memory_type="constraint", content=content,
        provenance=MemorySource.SAID, strength_as_of=now, created_at=now, confidence=0.9,
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Never work after 10 PM", [(840, DAY_END)]),
        ("Only works after 6 PM", [(0, 600)]),
        ("Is free after 5 PM", []),
        ("No work before 9 AM or after 10 PM", [(0, 60), (840, DAY_END)]),
    ],
)
def test_memory_slots__constraint_memory__blocks_what_the_rule_means(content, expected):
    from app.services.memory.constraint_bridge import memory_slots

    slots = memory_slots([_constraint_memory(content)])

    assert [(s.start_min, s.end_min) for s in slots] == expected
    assert {s.availability for s in slots} <= {"blocked"}
    assert len({s.name for s in slots}) == len(slots)


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        ("No work after 10 PM", [(22, 8)]),
        ("I work only after 6 PM", [(8, 18)]),
        ("I'm free after 5 PM", []),
    ],
)
def test_stated_blocks__said_rule__the_hours_it_keeps_free(text, hours):
    from app.domain.entities import BehavioralConstraint
    from app.services.anticipation.service import stated_blocks

    rule = BehavioralConstraint(
        id=str(uuid4()), user_id="u1", raw_text=text, constraint_type="habit", source="said"
    )

    assert stated_blocks([rule]) == [(start, end, text) for start, end in hours]


# --- the demo's own words: the live check's taught rule and the seeded memories ------


@pytest.mark.asyncio
async def test_live_check_rule__translates_to_08_to_11_without_a_model(translator):
    import scripts.live_e2e as live_e2e

    module, model = translator

    slots = await module.translate_habits_to_slots(live_e2e.RULE_PROMPT)

    assert _windows(slots) == [(0, 180, "blocked", "daily")]
    assert model.calls == []


def test_seed_said_memories__block_no_hours():
    """The seed states no rule; what it says the user said keeps no hour free."""
    import scripts.seed_demo_user as seed
    from app.services.memory.constraint_bridge import memory_slots

    said = [text for _days, _kind, text in seed._SAID]

    assert [blocked_windows(text) for text in said] == [[] for _ in said]
    assert memory_slots([_constraint_memory(text) for text in said]) == []
