"""A rule the user states is one thing, whichever control lets go of it.

Saying "never schedule before 11 AM" stores a rule (a behavioural constraint, which
planning reads) and a constraint memory (which the Memory panel shows). The two are
linked when the turn writes them: the rule carries its memory's id. Planning plans
from the rule only, and skips the memory it covers. Forgetting the memory deletes the
rule; deleting the rule forgets the memory. Both user-scoped, both safe to repeat.

InMemory repositories and the real constraint bridge; the re-plans run the planning
module's own constraint steps over the Supabase family on ``FakeSupabase``.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

import app.core.config as cfg
from app.container import AppContainer
from app.domain import errors
from app.domain.entities import BehavioralConstraint, MemorySource, MemoryStatus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.orchestrator.hooks import ActionHooks
from app.services.memory.constraint_bridge import memory_slots
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from app.services.memory.stated_rules import StatedRules, rule_memory_id
from tests.fakes import BagOfWordsEmbedder, FakeSupabase

RULE = "Never schedule anything before 11 AM"
NOW = datetime(2026, 11, 16, 2, 30, tzinfo=timezone.utc)


def _rule(user_id: str = "alice", text: str = RULE) -> BehavioralConstraint:
    return BehavioralConstraint(
        id=str(uuid4()), user_id=user_id, raw_text=text, constraint_type="habit",
        recurrence="daily",
    )


@pytest.fixture
def repos():
    return RepositoryFactory.in_memory()


@pytest.fixture
def memory(repos) -> MemoryService:
    return MemoryService(repos.memories, BagOfWordsEmbedder(), clock=lambda: NOW)


@pytest.fixture
def rules(repos, memory) -> StatedRules:
    return StatedRules(repos.constraints, memory)


async def _said(memory: MemoryService, user_id: str = "alice", text: str = RULE):
    outcome = await memory.remember(
        user_id, MemoryCandidate(memory_type="constraint", content=text, confidence=0.8),
        provenance=MemorySource.SAID,
    )
    return outcome.memory


async def _linked(repos, rules, memory, user_id: str = "alice"):
    rule = await repos.constraints.add(user_id, _rule(user_id))
    remembered = await _said(memory, user_id)
    assert await rules.link(user_id, rule.id, remembered.id) is True
    return rule, remembered


# --- the link ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link__rule_carries_its_memory_id__other_semantics_kept(repos, rules, memory):
    rule = _rule().model_copy(update={"structured_semantics": {"note": "kept"}})
    rule = await repos.constraints.add("alice", rule)
    remembered = await _said(memory)

    assert await rules.link("alice", rule.id, remembered.id) is True

    (stored,) = await repos.constraints.list_active("alice")
    assert stored.structured_semantics == {"note": "kept", "memory_id": remembered.id}
    assert rule_memory_id(stored) == remembered.id
    assert rule_memory_id(stored.model_dump()) == remembered.id  # a planning row reads the same
    assert await rules.link("bob", rule.id, remembered.id) is False  # another user's rule


# --- forgetting the memory -----------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_memory__linked_rule__rule_deleted_and_memory_tombstoned(
    repos, rules, memory
):
    rule, remembered = await _linked(repos, rules, memory)
    unlinked = await repos.constraints.add("alice", _rule(text="Gym at 6 PM on Mondays"))

    await rules.forget_memory("alice", remembered.id)

    assert await repos.constraints.list_active("alice") == [unlinked]
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.FORGOTTEN
    await rules.forget_memory("alice", remembered.id)  # again: nothing more to do
    assert await repos.constraints.list_active("alice") == [unlinked]


@pytest.mark.asyncio
async def test_forget_memory__another_users_memory__not_found_and_nothing_deleted(
    repos, rules, memory
):
    rule, remembered = await _linked(repos, rules, memory)

    with pytest.raises(errors.NotFoundError):
        await rules.forget_memory("bob", remembered.id)

    assert await repos.constraints.list_active("alice") == [
        rule.model_copy(update={"structured_semantics": {"memory_id": remembered.id}})
    ]
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.ACTIVE


# --- deleting the rule ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_rule__linked_memory__memory_forgotten_and_the_same_rule_said_twice_gone(
    repos, rules, memory
):
    rule, remembered = await _linked(repos, rules, memory)
    again = await repos.constraints.add("alice", _rule())  # the same rule, said again
    await rules.link("alice", again.id, remembered.id)

    assert await rules.delete_rule("alice", rule.id) is True

    assert await repos.constraints.list_active("alice") == []
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.FORGOTTEN
    assert await rules.delete_rule("alice", rule.id) is False  # again: nothing to delete


@pytest.mark.asyncio
async def test_delete_rule__unlinked_rule__memories_untouched(repos, rules, memory):
    rule = await repos.constraints.add("alice", _rule())
    remembered = await _said(memory)

    assert await rules.delete_rule("alice", rule.id) is True

    assert await repos.constraints.list_active("alice") == []
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_delete_rule__another_users_rule__false_and_nothing_changed(repos, rules, memory):
    rule, remembered = await _linked(repos, rules, memory)

    assert await rules.delete_rule("bob", rule.id) is False

    assert [r.id for r in await repos.constraints.list_active("alice")] == [rule.id]
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_delete_rule__memory_already_gone__rule_still_deleted(repos, rules, memory):
    rule = await repos.constraints.add("alice", _rule())
    await rules.link("alice", rule.id, str(uuid4()))  # a memory that no longer exists

    assert await rules.delete_rule("alice", rule.id) is True
    assert await repos.constraints.list_active("alice") == []


# --- planning: from the rule only --------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_slots__constraint_memory_a_rule_covers__no_second_block(memory):
    covered = await _said(memory)
    other = await _said(memory, text="No calls between 14:00 and 15:00")

    slots = memory_slots([covered, other], covered=frozenset({covered.id}))

    assert [(s.name, s.start_min, s.end_min) for s in slots] == [
        (f"memory_constraint_{other.id}", 360, 420)
    ]


async def _morning_blocks(container, fake) -> tuple[str, list[tuple[int, int]]]:
    """What a re-plan reads: the habit text it translates, and the memory blocks it adds."""
    from app.core.user_model import UserModel
    from app.modules.planning_graph import fetch_constraints, memory_to_constraints

    user_model = UserModel.for_planning("alice", container.planning)
    state = {"user_id": "alice", "user_model": user_model, "planning": container.planning}
    state.update(await fetch_constraints(state))
    slots = (await memory_to_constraints(state)).get("time_slots") or []
    return state["habits_text"], sorted({(s["start_min"] % 1440, s["end_min"] % 1440) for s in slots})


@pytest.fixture
def wired():
    fake = FakeSupabase()
    container = AppContainer.build(
        supabase_client=fake, cfg=cfg, hooks=ActionHooks(), embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )
    return container, fake


async def _taught(container) -> tuple[BehavioralConstraint, str]:
    """The rule as a BEHAVIOURAL_CONSTRAINT turn leaves it: a rule row and its memory, linked."""
    rule = await container.repos.constraints.add("alice", _rule())
    remembered = await _said(container.memory_service)
    await container.stated_rules.link("alice", rule.id, remembered.id)
    return rule, remembered.id


@pytest.mark.asyncio
async def test_replan__taught_rule__planned_from_the_rule_alone(wired):
    container, fake = wired
    await _taught(container)

    habits_text, blocks = await _morning_blocks(container, fake)

    assert habits_text == RULE  # the rule is translated, as every habit is
    assert blocks == []  # its memory adds no second 08:00-11:00 block


@pytest.mark.asyncio
async def test_replan__after_forgetting_the_memory__morning_free_again(wired):
    container, fake = wired
    rule, memory_id = await _taught(container)

    await container.stated_rules.forget_memory("alice", memory_id)

    assert await _morning_blocks(container, fake) == ("", [])


@pytest.mark.asyncio
async def test_replan__after_deleting_the_rule__morning_free_again(wired):
    container, fake = wired
    rule, memory_id = await _taught(container)

    assert await container.stated_rules.delete_rule("alice", rule.id) is True

    assert await _morning_blocks(container, fake) == ("", [])
    assert await container.memory_service.list_active("alice") == []


# --- one memory, differently worded rules: deleting one rule never removes another -----

STUDY = "Never study before 11 AM"
WORK = "Never work after 10 PM"
COMPOUND = "Never study before 11 AM and never work after 10 PM"


async def _compound(repos, rules, memory):
    """One memory of saying two rules at once, each rule linked to it."""
    study = await repos.constraints.add("alice", _rule(text=STUDY))
    work = await repos.constraints.add("alice", _rule(text=WORK))
    remembered = await _said(memory, text=COMPOUND)
    for rule in (study, work):
        assert await rules.link("alice", rule.id, remembered.id)
    return study, work, remembered


@pytest.mark.asyncio
async def test_delete_rule__compound_memory__the_other_rule_and_the_memory_stay(
    repos, rules, memory
):
    study, work, remembered = await _compound(repos, rules, memory)

    assert await rules.delete_rule("alice", study.id) is True

    assert [r.raw_text for r in await repos.constraints.list_active("alice")] == [WORK]
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.ACTIVE

    assert await rules.delete_rule("alice", work.id) is True  # the last rule saying it
    assert await repos.constraints.list_active("alice") == []
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.FORGOTTEN


@pytest.mark.asyncio
async def test_forget_memory__compound_memory__every_rule_it_holds_goes_and_is_named(
    repos, rules, memory
):
    study, work, remembered = await _compound(repos, rules, memory)

    gone = await rules.forget_memory("alice", remembered.id)

    assert sorted(gone) == sorted([study.id, work.id])
    assert await repos.constraints.list_active("alice") == []
    assert await rules.forget_memory("alice", remembered.id) == []  # again: nothing left


@pytest.mark.asyncio
async def test_delete_rule__a_later_rule_merged_into_the_same_memory__the_first_stays(
    repos, rules, memory
):
    first = await repos.constraints.add("alice", _rule(text="Never schedule before 11 AM"))
    remembered = await _said(memory)
    await rules.link("alice", first.id, remembered.id)
    later = await repos.constraints.add("alice", _rule(text="No meetings before 11 AM, please"))
    await rules.link("alice", later.id, remembered.id)  # reinforced into the same memory

    assert await rules.delete_rule("alice", later.id) is True

    assert [r.id for r in await repos.constraints.list_active("alice")] == [first.id]
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_delete_rule__same_words_said_twice__both_copies_go_whatever_the_spacing(
    repos, rules, memory
):
    rule = await repos.constraints.add("alice", _rule(text="Never schedule before 11 AM"))
    copy = await repos.constraints.add("alice", _rule(text="  never schedule before 11 am. "))
    remembered = await _said(memory)
    for each in (rule, copy):
        await rules.link("alice", each.id, remembered.id)

    assert await rules.delete_rule("alice", rule.id) is True

    assert await repos.constraints.list_active("alice") == []
    assert (await repos.memories.get("alice", remembered.id)).status is MemoryStatus.FORGOTTEN


# --- a rule adopted from a learned pattern is linked to the pattern ----------------------


class _NoPlan:
    async def replan(self, user_id, *, reslot_task_ids):
        return None


async def _adopted(repos, memory):
    """A skip window PEARL learned at 9 AM, and the rule the user adopted from it."""
    from app.domain.entities import Memory
    from app.services.anticipation.actions import AdoptRuleAction

    pattern = await repos.memories.add("alice", Memory(
        user_id="alice", memory_type="behavioral_pattern",
        content="You tend to set work aside around 9 AM", provenance=MemorySource.INFERRED,
        origin="behavior", pattern_key="skip_time_window:9", confidence=0.8,
        strength_as_of=NOW, created_at=NOW,
    ))
    action = AdoptRuleAction(
        "skip_time_window:9", 9, pattern.id, confidence=0.8, occurrences=5,
        constraints=repos.constraints, memory=memory, planner=_NoPlan(),
    )
    await action.execute("alice")
    (rule,) = await repos.constraints.list_active("alice")
    return pattern, rule


@pytest.mark.asyncio
async def test_adopted_rule__linked_to_the_patterns_memory(repos, memory):
    pattern, rule = await _adopted(repos, memory)

    assert rule_memory_id(rule) == pattern.id
    assert rule.structured_semantics["pattern_key"] == "skip_time_window:9"  # still there


@pytest.mark.asyncio
@pytest.mark.parametrize("let_go", ["forget_memory", "dismiss_memory"])
async def test_pattern_let_go__its_adopted_rule_goes_too(repos, rules, memory, let_go):
    pattern, rule = await _adopted(repos, memory)

    gone = await getattr(rules, let_go)("alice", pattern.id)
    if let_go == "dismiss_memory":
        dismissed, gone = gone
        assert dismissed.id == pattern.id

    assert gone == [rule.id]
    assert await repos.constraints.list_active("alice") == []
    expected = MemoryStatus.FORGOTTEN if let_go == "forget_memory" else MemoryStatus.DISMISSED
    assert (await repos.memories.get("alice", pattern.id)).status is expected


@pytest.mark.asyncio
async def test_delete_adopted_rule__pattern_dismissed_not_forgotten_never_proposed_again(
    repos, rules, memory
):
    pattern, rule = await _adopted(repos, memory)

    assert await rules.delete_rule("alice", rule.id) is True

    stored = await repos.memories.get("alice", pattern.id)
    assert stored.status is MemoryStatus.DISMISSED  # PEARL never re-learns or re-proposes it
    assert stored.content == pattern.content  # dismissed, not erased
    assert await repos.constraints.list_active("alice") == []


# --- a corrected rule: the memory it rested on was superseded -------------------------

GYM_6PM = "Goes to the gym every weekday at 6 PM"
GYM_7AM = "Goes to the gym at 7 AM on weekdays"


async def _superseded(memory: MemoryService, old_id: str, text: str = GYM_7AM):
    """The user corrects the memory ``old_id``: the new memory, which supersedes it."""
    outcome = await memory.remember(
        "alice",
        MemoryCandidate(memory_type="constraint", content=text, confidence=0.9, contradicts=old_id),
    )
    assert outcome.action == "superseded"
    return outcome.memory


@pytest.mark.asyncio
async def test_replace_for_superseded__old_memory_had_a_rule__rule_and_its_check_in_deleted(
    repos, memory
):
    from app.services.analytical.rule_check_ins import RuleCheckIns
    from app.services.extraction.behavioral_store import store_behavioral_constraint

    check_ins = RuleCheckIns(repos.habit_trackers, repos.constraints, repos.profiles,
                             clock=lambda: NOW)
    rules = StatedRules(repos.constraints, memory, check_ins=check_ins)
    stored = await store_behavioral_constraint(
        repos.constraints, GYM_6PM, user_id="alice", check_ins=check_ins
    )
    old = await _said(memory, text=GYM_6PM)
    assert await rules.link("alice", stored["id"], old.id)
    kept, _ = await _linked(repos, rules, memory)  # another rule, on another memory
    assert len(await repos.habit_trackers.list_for_user("alice")) == 1
    await _superseded(memory, old.id)

    assert await rules.replace_for_superseded("alice", old.id) == [stored["id"]]

    assert [r.id for r in await repos.constraints.list_active("alice")] == [kept.id]
    assert await repos.habit_trackers.list_for_user("alice") == []
    untouched = await repos.memories.get("alice", old.id)  # the memory is left as it was
    assert untouched.status is MemoryStatus.ACTIVE and untouched.content == GYM_6PM
    assert await rules.replace_for_superseded("alice", old.id) == []  # again: nothing left


@pytest.mark.asyncio
async def test_replace_for_superseded__old_memory_had_no_rule__nothing_deleted(
    repos, rules, memory
):
    rule, _ = await _linked(repos, rules, memory)
    unlinked = await repos.constraints.add("alice", _rule(text="Gym at 6 PM on Mondays"))
    fact = (await memory.remember(
        "alice", MemoryCandidate(memory_type="fact", content="Dr. Rao teaches Networks"),
    )).memory
    await _superseded(memory, fact.id, "Dr. Iyer teaches Networks now")

    assert await rules.replace_for_superseded("alice", fact.id) == []

    assert {r.id for r in await repos.constraints.list_active("alice")} == {rule.id, unlinked.id}


@pytest.mark.asyncio
async def test_replace_for_superseded__another_users_memory_id__nothing_deleted(
    repos, rules, memory
):
    rule, remembered = await _linked(repos, rules, memory)
    await _superseded(memory, remembered.id)

    assert await rules.replace_for_superseded("bob", remembered.id) == []

    assert [r.id for r in await repos.constraints.list_active("alice")] == [rule.id]
