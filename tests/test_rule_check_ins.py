"""Rule check-ins: Jarvis asks, spaced out, whether each rule it learned still holds.

Every rule gets one SM-2 tracker. A rule just stated is first checked on the next day;
a rule older than its tracker is already due. Each answer spaces the next check-in out
(1 day, 6 days, longer) while the rule holds, and a low rating brings it back the next
day. Deleting a rule, by any control, takes its tracker; a tracker whose rule is gone
is never listed. Over the real ``RuleCheckIns`` and both repository families.
"""

import asyncio
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.domain import errors
from app.domain.entities import BehavioralConstraint, HabitTracker, Memory, MemorySource
from app.infrastructure.persistence.factory import RepositoryFactory
from app.infrastructure.persistence.in_memory import InMemoryHabitTrackerRepository, InMemoryStore
from app.services.analytical.rule_check_ins import RuleCheckIns
from app.services.extraction.behavioral_store import store_behavioral_constraint
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from app.services.memory.stated_rules import StatedRules
from tests.fakes import BagOfWordsEmbedder, FakeSupabase

IST = ZoneInfo("Asia/Kolkata")
U = "alice"
MONDAY_9PM = datetime(2026, 11, 16, 21, 0, tzinfo=IST)
NIGHT_RULE = "No work after 10 PM"
LUNCH_RULE = "Keep 1 to 2 PM free for lunch"


class _SimClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture(params=["in_memory", "supabase_fake"])
def repos(request, store):
    if request.param == "in_memory":
        return RepositoryFactory.in_memory(store)
    return RepositoryFactory.supabase(FakeSupabase())


@pytest.fixture
def sim() -> _SimClock:
    return _SimClock(MONDAY_9PM)


@pytest.fixture
def check_ins(repos, sim) -> RuleCheckIns:
    return RuleCheckIns(repos.habit_trackers, repos.constraints, repos.profiles, clock=sim)


async def _teach(repos, check_ins: RuleCheckIns, text: str = NIGHT_RULE) -> str:
    stored = await store_behavioral_constraint(
        repos.constraints, text, user_id=U, check_ins=check_ins
    )
    assert stored["status"] == "stored"
    return stored["id"]


# --- teaching a rule starts its check-ins -------------------------------------------


@pytest.mark.asyncio
async def test_check_ins__rule_stated__first_check_in_due_the_next_day(repos, sim, check_ins):
    rule_id = await _teach(repos, check_ins)

    tonight = await check_ins.list_for_user(U)
    sim.advance(days=1)
    tomorrow = await check_ins.list_for_user(U)

    assert tonight.due_trackers == []
    (later,) = tonight.upcoming_trackers
    assert (later.constraint_id, later.rule_text, later.source) == (
        rule_id, NIGHT_RULE, MemorySource.SAID
    )
    assert (later.next_due_on, later.interval_days, later.last_reviewed_at) == (
        date(2026, 11, 17), 1, None
    )
    assert (tonight.timezone, tonight.today) == ("Asia/Kolkata", date(2026, 11, 16))
    assert [c.id for c in tomorrow.due_trackers] == [later.id]
    assert tomorrow.upcoming_trackers == []
    assert len(await repos.habit_trackers.list_for_user(U)) == 1  # read twice, started once


@pytest.mark.asyncio
async def test_check_ins__tracker_cannot_be_started__rule_still_stored_and_the_next_read_starts_it(
    sim,
):
    class _Down(InMemoryHabitTrackerRepository):
        down = True

        async def add_for_constraint(self, user_id, constraint_id, *, started_at):
            if self.down:
                raise errors.PersistenceError("Could not reach the database.")
            return await super().add_for_constraint(user_id, constraint_id, started_at=started_at)

    store = InMemoryStore()
    repos = RepositoryFactory.in_memory(store)
    trackers = _Down(store)
    check_ins = RuleCheckIns(trackers, repos.constraints, repos.profiles, clock=sim)

    rule_id = await _teach(repos, check_ins)
    trackers.down = False
    listed = await check_ins.list_for_user(U)

    assert [r.id for r in await repos.constraints.list_active(U)] == [rule_id]
    # Counted from the rule's stored stamp (the store's own clock): listed either way.
    assert [c.constraint_id for c in listed.due_trackers + listed.upcoming_trackers] == [rule_id]


@pytest.mark.asyncio
async def test_check_ins__rule_adopted_from_a_pattern__checked_in_on_like_a_stated_one(
    repos, sim, check_ins
):
    from app.services.anticipation.actions import AdoptRuleAction

    class _NoPlan:
        async def replan(self, user_id, *, reslot_task_ids):
            return None

    memory = MemoryService(repos.memories, BagOfWordsEmbedder(), clock=sim)
    pattern = await repos.memories.add(U, Memory(
        user_id=U, memory_type="behavioral_pattern",
        content="You tend to set work aside around 9 AM", provenance=MemorySource.INFERRED,
        origin="behavior", pattern_key="skip_time_window:9", confidence=0.8,
        strength_as_of=sim.now, created_at=sim.now,
    ))
    action = AdoptRuleAction(
        "skip_time_window:9", 9, pattern.id, confidence=0.8, occurrences=5,
        constraints=repos.constraints, memory=memory, planner=_NoPlan(), check_ins=check_ins,
    )

    await action.execute(U)

    (rule,) = await repos.constraints.list_active(U)
    (later,) = (await check_ins.list_for_user(U)).upcoming_trackers
    assert (later.constraint_id, later.source) == (rule.id, MemorySource.INFERRED)
    assert later.next_due_on == date(2026, 11, 17)


# --- rules older than their trackers --------------------------------------------------


@pytest.mark.asyncio
async def test_check_ins__rule_without_a_tracker__started_on_read_counting_from_when_learned(
    repos, sim, check_ins
):
    week_ago = sim.now - timedelta(days=7)
    rule = await repos.constraints.add(U, BehavioralConstraint(
        id=str(uuid4()), user_id=U, raw_text=LUNCH_RULE, constraint_type="habit",
        created_at=week_ago,
    ))

    first = await check_ins.list_for_user(U)
    again = await check_ins.list_for_user(U)

    (due,) = first.due_trackers
    assert (due.constraint_id, due.learned_at) == (rule.id, week_ago)
    assert due.next_due_on == date(2026, 11, 10)  # the day after it was learned: waiting since
    assert again == first
    assert len(await repos.habit_trackers.list_for_user(U)) == 1


@pytest.mark.asyncio
async def test_check_ins__two_reads_at_once__one_tracker_per_rule(repos, check_ins):
    for text in (NIGHT_RULE, LUNCH_RULE):
        await repos.constraints.add(U, BehavioralConstraint(
            id=str(uuid4()), user_id=U, raw_text=text, constraint_type="habit",
        ))

    first, second = await asyncio.gather(check_ins.list_for_user(U), check_ins.list_for_user(U))

    trackers = await repos.habit_trackers.list_for_user(U)
    assert sorted(t.constraint_id for t in trackers) == sorted(
        c.constraint_id for c in first.due_trackers + first.upcoming_trackers
    )
    assert len(trackers) == 2
    assert first == second


@pytest.mark.asyncio
async def test_check_ins__two_trackers_for_one_rule__listed_once_the_one_answered_last(sim):
    """Two server processes racing can each start a tracker for one rule (the table has
    no unique key): the rule is listed once, under the tracker answered last."""
    store = InMemoryStore()
    repos = RepositoryFactory.in_memory(store)
    check_ins = RuleCheckIns(repos.habit_trackers, repos.constraints, repos.profiles, clock=sim)
    rule_id = await _teach(repos, check_ins)
    answered = HabitTracker(
        id=str(uuid4()), user_id=U, constraint_id=rule_id, repetitions=1, quality_last=5,
        last_done_at=sim.now + timedelta(hours=1),
    )
    store.habit_trackers.insert(0, answered)  # stored first: listed last (oldest)

    listed = await check_ins.list_for_user(U)

    assert [c.id for c in listed.due_trackers + listed.upcoming_trackers] == [answered.id]


# --- answering spaces the next check-in out -------------------------------------------


@pytest.mark.asyncio
async def test_check_ins__answers__spaced_out_while_it_holds_and_back_tomorrow_after_a_low_one(
    repos, sim, check_ins
):
    await _teach(repos, check_ins)
    sim.advance(days=1)  # Tuesday 21:00: due
    (due,) = (await check_ins.list_for_user(U)).due_trackers

    first = await check_ins.review(U, due.id, 5)
    sim.advance(days=1)  # Wednesday
    second = await check_ins.review(U, due.id, 4)
    after = await check_ins.list_for_user(U)
    sim.advance(days=6)  # the next Tuesday
    lapse = await check_ins.review(U, due.id, 1)

    assert (first.next_interval_days, first.next_due_on) == (1, date(2026, 11, 18))
    assert (second.next_interval_days, second.next_due_on) == (6, date(2026, 11, 24))
    assert second.next_review_date == datetime(2026, 11, 24, 21, 0, tzinfo=IST)
    assert after.due_trackers == []
    (later,) = after.upcoming_trackers
    assert (later.next_due_on, later.interval_days) == (date(2026, 11, 24), 6)
    assert later.last_reviewed_at == datetime(2026, 11, 18, 21, 0, tzinfo=IST)
    assert (lapse.next_interval_days, lapse.next_due_on) == (1, date(2026, 11, 25))


@pytest.mark.asyncio
async def test_check_ins__answer_for_another_users_tracker__not_found_and_untouched(
    repos, check_ins
):
    await _teach(repos, check_ins)
    (mine,) = (await check_ins.list_for_user(U)).upcoming_trackers

    with pytest.raises(errors.NotFoundError):
        await check_ins.review("bob", mine.id, 5)

    assert (await repos.habit_trackers.get(U, mine.id)).quality_last is None


@pytest.mark.asyncio
async def test_check_ins__on_the_users_clock__a_small_hours_answer_counts_for_that_local_day(
    repos, sim, check_ins
):
    """01:30 IST on Wednesday is still Tuesday in UTC: the answer is Wednesday's, so the
    next check-in a day later is Thursday on the user's clock, not Wednesday."""
    await _teach(repos, check_ins)
    sim.now = datetime(2026, 11, 18, 1, 30, tzinfo=IST)
    (due,) = (await check_ins.list_for_user(U)).due_trackers

    answered = await check_ins.review(U, due.id, 5)

    assert answered.next_due_on == date(2026, 11, 19)
    assert (await check_ins.list_for_user(U)).today == date(2026, 11, 18)


# --- letting go of a rule takes its check-ins ------------------------------------------


@pytest.mark.asyncio
async def test_check_ins__rule_deleted__its_tracker_goes_and_only_the_other_is_listed(
    repos, sim, check_ins
):
    rules = StatedRules(
        repos.constraints, MemoryService(repos.memories, BagOfWordsEmbedder(), clock=sim),
        check_ins=check_ins,
    )
    kept = await _teach(repos, check_ins, NIGHT_RULE)
    gone = await _teach(repos, check_ins, LUNCH_RULE)

    assert await rules.delete_rule(U, gone)

    listed = await check_ins.list_for_user(U)
    assert [c.constraint_id for c in listed.upcoming_trackers] == [kept]
    assert [t.constraint_id for t in await repos.habit_trackers.list_for_user(U)] == [kept]


@pytest.mark.asyncio
async def test_check_ins__memory_of_a_rule_forgotten__the_rule_and_its_tracker_go(
    repos, sim, check_ins
):
    memory = MemoryService(repos.memories, BagOfWordsEmbedder(), clock=sim)
    rules = StatedRules(repos.constraints, memory, check_ins=check_ins)
    rule_id = await _teach(repos, check_ins)
    said = await memory.remember(
        U, MemoryCandidate(memory_type="constraint", content=NIGHT_RULE, confidence=0.8),
        provenance=MemorySource.SAID,
    )
    assert await rules.link(U, rule_id, said.memory.id)

    assert await rules.forget_memory(U, said.memory.id) == [rule_id]

    assert await repos.habit_trackers.list_for_user(U) == []
    listed = await check_ins.list_for_user(U)
    assert listed.due_trackers == listed.upcoming_trackers == []


@pytest.mark.asyncio
async def test_check_ins__tracker_whose_rule_is_gone__never_listed(repos, sim, check_ins):
    await repos.habit_trackers.add_for_constraint(
        U, str(uuid4()), started_at=sim.now - timedelta(days=3)
    )

    listed = await check_ins.list_for_user(U)

    assert listed.due_trackers == listed.upcoming_trackers == []
