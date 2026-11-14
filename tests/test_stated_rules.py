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
from app.core import runtime
from app.domain import errors
from app.domain.entities import BehavioralConstraint, MemorySource, MemoryStatus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.orchestrator.hooks import ActionHooks
from app.services.memory.constraint_bridge import memory_slots
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from app.services.memory.stated_rules import StatedRules, rule_memory_id
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

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

    user_model = UserModel(user_id="alice", db=FakeDBClient(fake))
    state = {"user_id": "alice", "user_model": user_model}
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
    runtime.set_container(container)  # conftest resets the registry afterwards
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
