"""Unit tests for OR-Tools JarvisScheduler and INFEASIBLE states."""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.core.or_tools.solver import JarvisScheduler


def test_hard_block_and_single_task():
    """Task should be scheduled after the hard block (no overlap)."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(0, 60, "early_block")
    scheduler.add_task("task_1", 30, 5, [])
    result, status = scheduler.solve()
    assert result != "INFEASIBLE"
    assert result["task_1"]["start"] >= 60


def test_dependency_a_before_b():
    """Task B must start after Task A ends."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_task("A", 30, 1, [])
    scheduler.add_task("B", 20, 2, ["A"])
    result, _ = scheduler.solve()
    assert result != "INFEASIBLE"
    assert result["B"]["start"] >= result["A"]["end"]


def test_sleep_block_tasks_avoid_night():
    """Tasks should avoid the sleep block (1380-1860)."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(1380, 1860, "sleep")
    scheduler.add_task("t1", 25, 3, [])
    scheduler.add_task("t2", 25, 2, ["t1"])
    result, _ = scheduler.solve()
    assert result != "INFEASIBLE"
    for tid, slot in result.items():
        assert slot["end"] <= 1380 or slot["start"] >= 1860


class _RecordingSolver:
    """Stands in for ``cp_model.CpSolver`` so the wall-clock cap can be read
    back without building a model slow enough to actually hit it."""

    instances: list["_RecordingSolver"] = []

    def __init__(self, status=None):
        from ortools.sat.python import cp_model

        self.parameters = SimpleNamespace(max_time_in_seconds=None)
        self._status = cp_model.UNKNOWN if status is None else status
        _RecordingSolver.instances.append(self)

    def solve(self, _model, _callback=None):
        return self._status

    def value(self, _var):  # pragma: no cover - only reached on FEASIBLE
        return 0


@pytest.fixture
def recording_solver(monkeypatch):
    """Swap CpSolver for the recorder; returns the list of instances built."""
    from app.core.or_tools import solver as solver_mod

    _RecordingSolver.instances = []
    monkeypatch.setattr(solver_mod.cp_model, "CpSolver", _RecordingSolver)
    return _RecordingSolver.instances


def test_solve_caps_wall_clock_time(recording_solver):
    """An uncapped CP-SAT solve has no upper bound on how long it runs.

    On the streaming path the solve sits between the user and their plan, so an
    unbounded search is an unbounded stall. The cap turns "hangs forever" into
    "returns the best schedule found, or INFEASIBLE" — the case the horizon
    ladder and the Socratic recalibration path already handle.
    """
    from app.core.config import SOLVER_MAX_TIME_SECONDS

    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_task("t1", 25, 1, [])
    scheduler.solve()

    assert len(recording_solver) == 1
    assert recording_solver[0].parameters.max_time_in_seconds == SOLVER_MAX_TIME_SECONDS
    assert SOLVER_MAX_TIME_SECONDS > 0


def test_solve_timeout_without_a_solution_reads_as_infeasible(recording_solver):
    """CP-SAT answers UNKNOWN when the cap expires before any solution exists.

    'I ran out of time' and 'there is no such schedule' are different facts, but
    the caller's options are identical — widen the horizon, cut scope — so
    UNKNOWN takes the INFEASIBLE path rather than crashing on a status the
    unpacking never expected.
    """
    from ortools.sat.python import cp_model

    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_task("t1", 25, 1, [])

    result, status = scheduler.solve()

    # The stand-in returns UNKNOWN, exactly what an expired cap looks like.
    assert recording_solver[-1]._status == cp_model.UNKNOWN
    assert result == "INFEASIBLE"
    assert status == ""


def test_infeasible_too_much_work():
    """Impossible constraint (50 hours of tasks in 24) should return INFEASIBLE."""
    scheduler = JarvisScheduler(horizon_minutes=1440)  # 24 hours
    scheduler.add_hard_block(0, 480, "sleep")  # 8h sleep
    # 50 hours of tasks in 24h horizon with 8h blocked
    for i in range(60):  # 60 tasks x 50 min = 3000 min > 1440
        scheduler.add_task(f"task_{i}", 50, 1, [])
    result, status = scheduler.solve()
    assert result == "INFEASIBLE"


def test_adaptive_cap__never_below_longest_task():
    """A cap smaller than the longest single task guarantees INFEASIBLE at
    every horizon — the ladder then *lowers* it further. Floor it."""
    from app.utils.pacing import compute_adaptive_daily_cap

    cap = compute_adaptive_daily_cap(
        horizon_minutes=43200,          # 30 days — worst slack ratio
        total_task_minutes=120,         # two hours of work
        longest_task_minutes=25,
    )
    assert cap >= 25


def test_adaptive_cap__composable_from_task_atoms():
    """cap=63 with 25-min tasks means a day fits 2 tasks (50) but never 63 —
    5x25 over 2 days (50+75) is unschedulable. Round the cap up to whole tasks."""
    from app.utils.pacing import compute_adaptive_daily_cap

    cap = compute_adaptive_daily_cap(
        horizon_minutes=2880, total_task_minutes=125, longest_task_minutes=25,
    )
    assert cap % 25 == 0 and cap >= 75, f"cap {cap} not composable from 25-min atoms"


# Plans run on the user's clock (DEFAULT_TIMEZONE, pinned to Asia/Kolkata by
# conftest); these tests froze UTC times when the horizon was UTC-anchored, and
# now freeze the same local times in India.
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def before_the_horizon(clock):
    """Plans made at 08:00 local, the start of the user's day: none of the horizon is past.

    (Before 08:00 the logical day is still yesterday's, most of it past.)
    """
    from datetime import datetime

    from app.services.planning import scheduling as sep

    clock.freeze(datetime(2026, 11, 16, 8, 0, tzinfo=IST), sep)


def _five_task_graph():
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk

    chunks = [TaskChunk(task_id=f"t{i}", title=f"part {i}", duration_minutes=25,
                        difficulty_weight=0.5, dependencies=[], completion_criteria="done")
              for i in range(5)]
    return ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                          cognitive_load_estimate={"intrinsic_load": 0.5})


def test_run_schedule__horizon_from_now__nothing_starts_before_now_rounded_up(clock):
    from datetime import datetime

    from app.services.planning import scheduling as sep

    clock.freeze(datetime(2026, 11, 16, 14, 2, tzinfo=IST), sep)

    resp = sep.run_schedule(_five_task_graph(), [], horizon_minutes=2880)

    assert resp.horizon_start == datetime(2026, 11, 16, 8, 0, tzinfo=IST)
    # 14:02 is 362 minutes in; the past is blocked to the next whole 5 minutes.
    assert min(t.start_min for t in resp.schedule.values()) >= 365


def test_run_schedule__explicit_horizon_start__is_left_as_given(clock):
    from datetime import datetime

    from app.services.planning import scheduling as sep

    clock.freeze(datetime(2026, 11, 16, 14, 2, tzinfo=IST), sep)
    eight_am = datetime(2026, 11, 16, 8, 0, tzinfo=IST)

    resp = sep.run_schedule(_five_task_graph(), [], horizon_minutes=2880, horizon_start=eight_am)

    assert resp.horizon_start == eight_am
    assert min(t.start_min for t in resp.schedule.values()) < 362


def test_run_schedule__five_atoms_two_days__feasible(before_the_horizon):
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk
    from app.services.planning import scheduling as sep

    chunks = [TaskChunk(task_id=f"t{i}", title=f"part {i}", duration_minutes=25,
                        difficulty_weight=0.5, dependencies=[], completion_criteria="done")
              for i in range(5)]
    graph = ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                           cognitive_load_estimate={"intrinsic_load": 0.5})
    resp = sep.run_schedule(graph, [], horizon_minutes=2880)
    assert len(resp.schedule) == 5


def test_hard_blocks__overlapping_windows__do_not_self_conflict():
    """Two overlapping *fixed* hard blocks must not make the model INFEASIBLE.

    ``add_hard_block`` builds a constant interval and ``solve`` drops every block
    into one ``add_no_overlap`` set alongside the tasks. Two constant intervals
    that overlap each other are unsatisfiable on their own — before a single task
    exists. Real inputs overlap constantly: a habit block ("no study before 10am"
    -> 0-120) and a memory constraint (0-600) both start at day zero. Blocked time
    is a *union*, not a renewable resource, so overlapping blocks are coalesced
    before the NoOverlap rather than being asked to avoid one another.
    """
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(0, 120, "habit_morning")
    scheduler.add_hard_block(0, 600, "memory_constraint")
    scheduler.add_task("t1", 25, 5, [])

    result, _ = scheduler.solve()

    assert result != "INFEASIBLE"
    # Merging must not weaken the block: the union 0-600 is still off-limits.
    assert result["t1"]["start"] >= 600


def test_hard_blocks__adjacent_and_nested__union_still_enforced():
    """Coalescing keeps every blocked minute blocked, including nested blocks."""
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_hard_block(0, 600, "outer")
    scheduler.add_hard_block(200, 300, "nested")
    scheduler.add_hard_block(600, 900, "adjacent")
    scheduler.add_task("t1", 25, 5, [])

    result, _ = scheduler.solve()

    assert result != "INFEASIBLE"
    assert result["t1"]["start"] >= 900


def test_free_minutes_per_day__overlapping_blocks__counted_once():
    """Overlapping blocks are summed today, so blocked time is double counted.

    A day blocked 0-120 and 0-600 has 600 blocked minutes, not 720; the summing
    version reports 840 free instead of 960 and narrows the daily cap on a number
    that was never real.
    """
    from app.schemas.context import Availability, TimeSlot
    from app.utils.pacing import compute_free_minutes_per_day

    ctx = [
        TimeSlot(name="a", start_min=0, end_min=120, availability=Availability.BLOCKED),
        TimeSlot(name="b", start_min=0, end_min=600, availability=Availability.BLOCKED),
    ]

    assert compute_free_minutes_per_day(ctx, 2880)[0] == 1440 - 600


def test_adaptive_cap__one_heavily_blocked_day__never_below_one_task_atom():
    """The free-per-day narrowing must not undo the task-atom floor.

    The cap is *global* — ``_build_daily_load_constraints`` applies it to every
    day — but ``min(free_per_day)`` comes from the single *worst* day. One almost
    fully blocked day therefore drags the cap under one task atom, and a cap below
    the longest task makes every day unschedulable, at every rung of the horizon
    ladder, even when later days are wide open.

    The floor is re-applied *after* the narrowing rather than the narrowing being
    dropped: narrowing is still the right pacing signal on a busy horizon, but the
    cap is a pacing heuristic, not a feasibility mechanism. Per-day blocked time is
    already enforced exactly by the hard-block NoOverlap, so the cap must never be
    the constraint that turns a solvable model INFEASIBLE.
    """
    from app.schemas.context import Availability, TimeSlot
    from app.utils.pacing import compute_adaptive_daily_cap

    ctx = [
        # Day 0 is booked solid bar 20 minutes; day 1 is wide open.
        TimeSlot(name="allday", start_min=0, end_min=1420, availability=Availability.BLOCKED),
        TimeSlot(name="sleep_d1", start_min=2400, end_min=2880, availability=Availability.BLOCKED),
    ]

    cap = compute_adaptive_daily_cap(
        horizon_minutes=2880,
        total_task_minutes=125,
        daily_context=ctx,
        longest_task_minutes=25,
    )

    assert cap >= 25, f"cap {cap} cannot fit a single 25-min task on any day"


def test_run_schedule__memory_block_overlapping_habit_block__feasible(before_the_horizon):
    """The live repro: a memory-bridge block overlapping an expanded habit block.

    A "no study before 10am" habit expands to a daily 0-120 block; the memory
    constraint bridge adds its own block starting at day zero. Both are hard, both
    are fixed, and they overlap — which used to be INFEASIBLE regardless of how
    much free time the horizon actually had.
    """
    from datetime import datetime, timezone

    from app.services.planning import scheduling as sep
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk
    from app.schemas.context import Availability, SemanticTimeSlot, TimeSlot
    from app.services.analytical.horizon_expander import expand_semantic_slots_to_time_slots

    slots = [SemanticTimeSlot(name="morning_restriction", start_min=0, end_min=120,
                              availability="blocked", recurrence="daily")]
    ctx = expand_semantic_slots_to_time_slots(slots, 2880, datetime.now(timezone.utc))
    # What the fixed bridge emits for "never studies before 11am": 8 AM -> 11 AM.
    ctx.append(TimeSlot(name="memory_constraint_x", start_min=0, end_min=180,
                        availability=Availability.BLOCKED, recurring=True))
    chunks = [TaskChunk(task_id=f"t{i}", title=f"p{i}", duration_minutes=25,
                        difficulty_weight=0.5, dependencies=[], completion_criteria="d")
              for i in range(5)]
    graph = ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                           cognitive_load_estimate={"intrinsic_load": 0.5})

    resp = sep.run_schedule(graph, ctx, horizon_minutes=2880)

    assert len(resp.schedule) == 5
    for task in resp.schedule.values():
        assert task.start_min >= 180, "task placed inside the blocked morning"


# --- the energy term: a learned focus profile leans hard tasks toward the best hours ---

# Minute 0 is 08:00 local: 09:00 is minute 60, 19:00 is minute 660.
NINE_AND_SEVEN_PM = [(60, 90), (660, 690)]
TODAY = "2026-11-16"  # the day before_the_horizon freezes
IN_3_DAYS = "2026-11-19"


def _blocked_except(free: list[tuple[int, int]], horizon: int = 1440) -> list:
    """Hard blocks over the whole horizon except the ``free`` windows."""
    from app.schemas.context import Availability, TimeSlot

    slots, cursor = [], 0
    for i, (start, end) in enumerate(sorted(free)):
        if start > cursor:
            slots.append(TimeSlot(name=f"busy_{i}", start_min=cursor, end_min=start,
                                  availability=Availability.BLOCKED))
        cursor = end
    if cursor < horizon:
        slots.append(TimeSlot(name="busy_rest", start_min=cursor, end_min=horizon,
                              availability=Availability.BLOCKED))
    return slots


def _graph(*tasks: tuple[str, int, float, str | None]):
    """An ExecutionGraph of (task_id, minutes, difficulty, deadline_hint) tasks."""
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk

    chunks = [TaskChunk(task_id=tid, title=tid, duration_minutes=minutes,
                        difficulty_weight=difficulty, dependencies=[],
                        completion_criteria="done", deadline_hint=deadline)
              for tid, minutes, difficulty, deadline in tasks]
    return ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                          cognitive_load_estimate={"intrinsic_load": 0.5})


def _peak_at(hour: int, *, rest: float = 0.2) -> list[float]:
    """A focus profile: full focus at ``hour``, ``rest`` everywhere else."""
    return [1.0 if h == hour else rest for h in range(24)]


def _overlaps(task, start: int, end: int) -> bool:
    return task.start_min < end and task.end_min > start


def test_energy_term__two_free_slots__hard_task_takes_high_focus_slot(
    before_the_horizon, deterministic_solver
):
    from app.services.planning import scheduling as sep

    def starts(peak_hour: int) -> tuple[int, int]:
        graph = _graph(("A", 25, 0.9, IN_3_DAYS), ("B", 25, 0.1, IN_3_DAYS))
        resp = sep.run_schedule(graph, _blocked_except(NINE_AND_SEVEN_PM), horizon_minutes=1440,
                                focus_by_hour=_peak_at(peak_hour))
        return resp.schedule["A"].start_min, resp.schedule["B"].start_min

    # On TMT alone the two slots tie; the hard task follows the peak, either way round.
    assert starts(19) == (660, 60)
    assert starts(9) == (60, 660)


def test_energy_term__flag_off__identical_to_no_profile(
    before_the_horizon, deterministic_solver, monkeypatch
):
    import app.core.config as config
    from app.services.planning import scheduling as sep

    def plan(focus):
        # B is shorter, so without a profile the hard task takes 09:00 (a smaller makespan).
        graph = _graph(("A", 25, 0.9, IN_3_DAYS), ("B", 10, 0.1, IN_3_DAYS))
        return sep.run_schedule(graph, _blocked_except(NINE_AND_SEVEN_PM), horizon_minutes=1440,
                                focus_by_hour=focus).schedule

    without_profile = plan(None)
    with_profile = plan(_peak_at(19))
    monkeypatch.setattr(config, "ENERGY_OBJECTIVE_ENABLED", False)
    flag_off = plan(_peak_at(19))

    assert flag_off == without_profile
    assert with_profile != without_profile  # the profile would have moved it; the flag held it


def test_energy_term__task_due_today__tmt_order_kept(
    before_the_horizon, deterministic_solver, monkeypatch
):
    import app.core.config as config
    from app.services.planning import scheduling as sep

    weights: dict[str, int] = {}

    class _Recording(JarvisScheduler):
        def add_task(self, task_id, *args, energy_weight=0, **kwargs):
            weights[task_id] = energy_weight
            super().add_task(task_id, *args, energy_weight=energy_weight, **kwargs)

    monkeypatch.setattr(sep, "JarvisScheduler", _Recording)
    # Free 09:00-09:25 and 10:00-10:25; the profile peaks at 10:00, where the hard,
    # due-today task would go if the energy term could outweigh its urgency.
    graph = _graph(("later", 25, 0.1, IN_3_DAYS), ("today", 25, 0.9, TODAY))

    resp = sep.run_schedule(graph, _blocked_except([(60, 85), (120, 145)]), horizon_minutes=1440,
                            focus_by_hour=_peak_at(10, rest=0.0))

    assert resp.schedule["today"].start_min < resp.schedule["later"].start_min
    assert weights == {"later": round(config.ENERGY_OBJECTIVE_WEIGHT * 0.1), "today": 0}


@pytest.mark.parametrize(
    "focus",
    [_peak_at(9), _peak_at(18), _peak_at(2), _peak_at(13, rest=0.6), [0.0] * 24],
    ids=["peak_in_lecture", "peak_in_gym", "peak_in_the_night", "mild_peak", "flat_zero"],
)
def test_energy_term__hard_blocks_never_violated(before_the_horizon, focus):
    from app.services.planning import scheduling as sep
    from app.schemas.context import Availability, TimeSlot

    blocked = Availability.BLOCKED
    ctx = [
        TimeSlot(name="lecture_d0", start_min=60, end_min=180, availability=blocked),
        TimeSlot(name="gym_d0", start_min=600, end_min=690, availability=blocked),
        TimeSlot(name="lecture_d1", start_min=1500, end_min=1620, availability=blocked),
        TimeSlot(name="gym_d1", start_min=2040, end_min=2130, availability=blocked),
    ]
    graph = _graph(*[(f"t{i}", 25, d, None) for i, d in enumerate([0.9, 0.8, 0.5, 0.3, 0.9, 0.1])])

    resp = sep.run_schedule(graph, ctx, horizon_minutes=2880, focus_by_hour=focus)

    # run_schedule appended the default night to ctx: every block it solved against.
    blocks = [(s.start_min, s.end_min) for s in ctx if s.availability == blocked]
    assert len(blocks) > 4
    assert len(resp.schedule) == 6
    for tid, task in resp.schedule.items():
        for start, end in blocks:
            assert not _overlaps(task, start, end), (tid, task, (start, end))


def test_energy_term__minimal_work_window_at_peak__hard_task_still_kept_out(
    before_the_horizon, deterministic_solver
):
    from app.services.planning import scheduling as sep
    from app.schemas.context import Availability, TimeSlot

    # 19:00-19:30 is the peak hour, but a minimal-work window (a commute): only short,
    # easy tasks may sit in it. Without it the energy term puts the hard task there.
    ctx = _blocked_except(NINE_AND_SEVEN_PM) + [
        TimeSlot(name="commute", start_min=660, end_min=690, availability=Availability.MINIMAL_WORK)
    ]
    graph = _graph(("hard", 25, 0.9, IN_3_DAYS), ("easy", 10, 0.1, IN_3_DAYS))

    resp = sep.run_schedule(graph, ctx, horizon_minutes=1440, focus_by_hour=_peak_at(19))

    assert not _overlaps(resp.schedule["hard"], 660, 690)
    assert resp.schedule["hard"].start_min == 60
    assert resp.schedule["easy"].start_min == 660


# --- a learned evening person: what the estimator makes of their history -------------


def _learned_evening_person() -> list[float]:
    """The focus profile HistoryEnergyEstimator learns from an evening person.

    Over four weeks, their 18:00-20:00 slots were mostly done (19:00 most of all) and
    their daytime slots, 08:00-17:00, mostly set aside. The peak is 19:00.
    """
    import asyncio
    from datetime import datetime, timedelta

    from app.domain.entities import TaskEvent, TaskEventType
    from app.infrastructure.persistence.factory import RepositoryFactory
    from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator

    now = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
    outcomes = [(TaskEventType.COMPLETED, h) for h, n in ((18, 4), (19, 6), (20, 4))
                for _ in range(n)]
    outcomes += [(TaskEventType.SKIPPED, h) for h in range(8, 18) for _ in range(2)]

    async def build() -> list[float]:
        repo = RepositoryFactory.in_memory().task_events
        for i, (kind, hour) in enumerate(outcomes):
            slot = (now - timedelta(days=1 + i % 27)).replace(hour=hour)
            await repo.append("u1", TaskEvent(
                user_id="u1", task_id=f"past_{i}", event_type=kind,
                occurred_at=slot + timedelta(minutes=25), scheduled_start=slot,
                scheduled_end=slot + timedelta(minutes=25),
            ))
        profile = await HistoryEnergyEstimator(repo, HeuristicEnergyEstimator()).estimate(
            "u1", IST, now=now
        )
        assert profile.source == "learned"
        return profile.scores()

    return asyncio.run(build())


def _eight_tasks():
    """Eight 25-minute tasks, hardest first, none due within a day."""
    difficulties = [0.9, 0.8, 0.7, 0.6, 0.5, 0.3, 0.2, 0.1]
    return _graph(*[(f"t{i}", 25, d, None) for i, d in enumerate(difficulties)])


def test_solve__idle_stop_on__feasible_plan_within_the_cap_and_blocks_hold(
    before_the_horizon, monkeypatch
):
    """The idle stop itself, asserting only what holds on a busy machine.

    Twelve tasks over three days with a learned profile: CP-SAT finds its best schedule
    early but cannot prove it within the 10 s cap, so without the idle stop this solve
    takes the whole cap. With it the search ends once nothing better turns up for a
    while. No placement or objective is asserted: when the search stops depends on how
    busy the machine is.
    """
    import time

    import app.core.config as config
    from app.services.planning import scheduling as sep
    from app.schemas.context import Availability, TimeSlot

    monkeypatch.setattr(config, "SOLVER_IDLE_STOP_SECONDS", 1.0)
    lectures = [TimeSlot(name=f"lecture_d{d}", start_min=d * 1440 + 60, end_min=d * 1440 + 240,
                         availability=Availability.BLOCKED) for d in range(3)]
    difficulties = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.85, 0.65, 0.45, 0.25]
    graph = _graph(*[(f"t{i}", 25, d, None) for i, d in enumerate(difficulties)])

    started = time.perf_counter()
    resp = sep.run_schedule(graph, lectures, horizon_minutes=4320,
                            focus_by_hour=_learned_evening_person())
    elapsed = time.perf_counter() - started

    assert len(resp.schedule) == 12
    blocks = [(s.start_min, s.end_min) for s in lectures if s.availability == Availability.BLOCKED]
    for tid, task in resp.schedule.items():
        for start, end in blocks:
            assert not _overlaps(task, start, end), (tid, task, (start, end))
    assert elapsed < config.SOLVER_MAX_TIME_SECONDS


def _local_minute(start_min: int) -> int:
    """Minute of the local day a plan minute falls on (minute 0 is 08:00)."""
    return (8 * 60 + start_min) % 1440


def _in_peak_window(start_min: int, peak_hour: int = 19) -> bool:
    """Starts within peak_hour ± 1.5 h, on whichever day."""
    return peak_hour * 60 - 90 <= _local_minute(start_min) <= peak_hour * 60 + 90


def test_schedule__learned_evening_peak__hard_tasks_land_in_peak_window(
    before_the_horizon, deterministic_solver
):
    from app.services.planning import scheduling as sep

    graph = _eight_tasks()
    hard = {c.task_id for c in graph.decomposition if c.difficulty_weight >= 0.7}

    resp = sep.run_schedule(graph, [], horizon_minutes=2880, focus_by_hour=_learned_evening_person())

    assert len(hard) == 3
    off_peak = {tid: _local_minute(resp.schedule[tid].start_min) for tid in hard
                if not _in_peak_window(resp.schedule[tid].start_min)}
    assert off_peak == {}, off_peak


def test_schedule__learned_evening_peak__easy_tasks_not_dragged_there_at_the_cost_of_an_idle_day(
    before_the_horizon, deterministic_solver
):
    """Easy tasks keep their daytime places: pulling them into the evening as well
    would leave the user's day idle until the peak. (A medium task may tie between
    morning and evening; only the easy ones are pinned.)"""
    from app.services.planning import scheduling as sep

    graph = _eight_tasks()
    easy = {c.task_id for c in graph.decomposition if c.difficulty_weight <= 0.3}

    resp = sep.run_schedule(graph, [], horizon_minutes=2880, focus_by_hour=_learned_evening_person())

    window_opens = 19 * 60 - 90
    late = {tid: _local_minute(resp.schedule[tid].start_min) for tid in easy
            if _local_minute(resp.schedule[tid].start_min) >= window_opens}
    assert late == {}, late


def test_energy_term__learned_profile__dependencies_still_hold(before_the_horizon):
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk
    from app.services.planning import scheduling as sep

    # Each step needs the one before; the hard steps would rather be in the evening.
    difficulties = [0.2, 0.9, 0.3, 0.8, 0.1, 0.7]
    chunks = [TaskChunk(task_id=f"s{i}", title=f"s{i}", duration_minutes=25, difficulty_weight=d,
                        dependencies=[f"s{i - 1}"] if i else [], completion_criteria="done")
              for i, d in enumerate(difficulties)]
    graph = ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                           cognitive_load_estimate={"intrinsic_load": 0.5})

    resp = sep.run_schedule(graph, [], horizon_minutes=2880, focus_by_hour=_learned_evening_person())

    for i in range(1, len(difficulties)):
        assert resp.schedule[f"s{i}"].start_min >= resp.schedule[f"s{i - 1}"].end_min


def _too_much_work_graph(tasks: int = 3) -> dict:
    return {
        "goal_metadata": {"objective": "Revise"},
        "decomposition": [
            {"task_id": f"t{i}", "title": f"Task {i}", "duration_minutes": 25,
             "difficulty_weight": 0.5, "completion_criteria": "done"}
            for i in range(tasks)
        ],
        "cognitive_load_estimate": {"intrinsic_load": 0.5},
    }


def test_run_schedule__infeasible__raises_schedule_infeasible_error():
    """INFEASIBLE is a scope problem the domain names (spec section 10), not an HTTP detail."""
    from app.schemas.decomposition import ExecutionGraph
    from app.services.planning.scheduling import run_schedule
    from app.domain.errors import ScheduleInfeasibleError
    from app.schemas.context import TimeSlot

    blocked = [TimeSlot(name="Lectures", start_min=0, end_min=60, availability="blocked")]

    with pytest.raises(ScheduleInfeasibleError) as caught:
        run_schedule(ExecutionGraph.model_validate(_too_much_work_graph()), blocked,
                     horizon_minutes=60)

    assert "scope" in caught.value.message


def test_generate_schedule__infeasible__422_schedule_infeasible_envelope():
    from fastapi.testclient import TestClient

    from app.main import app

    body = {
        "graph": _too_much_work_graph(),
        "horizon_minutes": 60,
        "plan_start": "2026-11-16T08:00:00+05:30",
        "daily_context": [
            {"name": "Lectures", "start_min": 0, "end_min": 60, "availability": "blocked"}
        ],
    }

    response = TestClient(app).post("/api/v1/schedule/generate-schedule", json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "schedule_infeasible"
    assert error["message"] == (
        "Schedule infeasible; consider reducing scope or extending deadline."
    )


# --- not before: a task's earliest start ------------------------------------------------
# Planned at 08:00 on Monday 16 Nov (IST), minute 0 is that 08:00: tomorrow 18:00 is
# minute 1440 + 600.

TOMORROW_6PM_MINUTE = 1440 + 600


def _not_before_graph(*tasks: tuple[str, str | None]):
    """An ExecutionGraph of 25-minute tasks, each (task_id, not_before)."""
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk

    chunks = [TaskChunk(task_id=tid, title=tid, duration_minutes=25, difficulty_weight=0.5,
                        dependencies=[], completion_criteria="done", not_before=not_before)
              for tid, not_before in tasks]
    return ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                          cognitive_load_estimate={"intrinsic_load": 0.5})


def test_scheduler__earliest_start__task_never_starts_before_it():
    scheduler = JarvisScheduler(horizon_minutes=2880)
    scheduler.add_task("held", 25, 50, [], earliest_start=600)
    scheduler.add_task("free", 25, 50, [])

    result, _ = scheduler.solve()

    assert result != "INFEASIBLE"
    assert result["held"]["start"] == 600  # as early as it may, never earlier
    assert result["free"]["start"] == 0


def test_scheduler__earliest_start_past_the_horizon__infeasible():
    scheduler = JarvisScheduler(horizon_minutes=1440)
    scheduler.add_task("held", 25, 50, [], earliest_start=1430)

    result, _ = scheduler.solve()

    assert result == "INFEASIBLE"


def test_run_schedule__not_before_tomorrow_evening__starts_at_or_after_it(before_the_horizon):
    from app.services.planning import scheduling as sep

    graph = _not_before_graph(
        ("sql_1", "2026-11-17T18:00"), ("sql_2", "2026-11-17T18:00"), ("other", None),
    )

    resp = sep.run_schedule(graph, [], horizon_minutes=2880)

    assert resp.schedule["sql_1"].start_min >= TOMORROW_6PM_MINUTE
    assert resp.schedule["sql_2"].start_min >= TOMORROW_6PM_MINUTE
    assert resp.schedule["other"].start_min == 0  # a task with no not_before is not held


def test_run_schedule__not_before_with_an_offset__read_as_that_instant(before_the_horizon):
    from app.services.planning import scheduling as sep

    # 12:30 UTC is 18:00 in India: a stored timestamptz, as a carried-over task has it.
    graph = _not_before_graph(("stored", "2026-11-17T12:30:00+00:00"))

    resp = sep.run_schedule(graph, [], horizon_minutes=2880)

    assert resp.schedule["stored"].start_min == TOMORROW_6PM_MINUTE


def test_run_schedule__not_before_a_date__from_the_start_of_that_day(before_the_horizon):
    from app.services.planning import scheduling as sep

    resp = sep.run_schedule(_not_before_graph(("wed", "2026-11-18")), [], horizon_minutes=4320)

    assert resp.schedule["wed"].start_min == 2 * 1440  # 08:00 on Wednesday


def test_run_schedule__not_before_already_past__no_constraint(clock):
    from datetime import datetime

    from app.services.planning import scheduling as sep

    clock.freeze(datetime(2026, 11, 16, 14, 2, tzinfo=IST), sep)
    graph = _not_before_graph(("past", "2026-11-16T09:00"), ("long_past", "2026-11-10"))
    unheld = _not_before_graph(("past", None), ("long_past", None))

    resp = sep.run_schedule(graph, [], horizon_minutes=2880)

    # Only now (14:02, minute 362, blocked to 365) holds them: the same plan as none.
    assert resp.schedule == sep.run_schedule(unheld, [], horizon_minutes=2880).schedule
    assert min(t.start_min for t in resp.schedule.values()) == 365


def test_run_schedule__not_before_past_the_horizon__infeasible_until_it_widens(
    before_the_horizon,
):
    from app.domain.errors import ScheduleInfeasibleError
    from app.services.planning import scheduling as sep

    friday_evening = "2026-11-20T18:00"  # minute 4 * 1440 + 600

    with pytest.raises(ScheduleInfeasibleError):
        sep.run_schedule(_not_before_graph(("fri", friday_evening)), [], horizon_minutes=2880)
    resp = sep.run_schedule(_not_before_graph(("fri", friday_evening)), [], horizon_minutes=7200)

    assert resp.schedule["fri"].start_min == 4 * 1440 + 600


def test_run_schedule__unreadable_not_before__ignored_and_logged(before_the_horizon, caplog):
    from app.services.planning import scheduling as sep

    with caplog.at_level("WARNING", logger="app.services.planning.local_time"):
        resp = sep.run_schedule(
            _not_before_graph(("vague", "tomorrow evening")), [], horizon_minutes=2880
        )

    assert resp.schedule["vague"].start_min == 0
    assert "not_before" in caplog.text


def test_adaptive_cap__work_held_to_the_horizons_last_day__raised_to_fit_it():
    from app.utils.pacing import compute_adaptive_daily_cap

    friday_6pm = 4 * 1440 + 600  # the fifth and last day of a 5-day horizon
    held = [(friday_6pm, 25)] * 3

    plain = compute_adaptive_daily_cap(
        horizon_minutes=7200, total_task_minutes=75, longest_task_minutes=25,
    )
    raised = compute_adaptive_daily_cap(
        horizon_minutes=7200, total_task_minutes=75, longest_task_minutes=25, held_work=held,
    )

    assert plain == 50  # spread over two days: never all three on Friday
    assert raised == 75


def test_held_work_minimum_cap__work_held_later__counts_against_earlier_holds_too():
    from app.utils.pacing import held_work_minimum_cap

    # Day 1 and day 2 of a 3-day horizon: 50 held from day 1 on (two days left) and
    # 60 from day 2 on (one day left), so day 2 alone must take 60.
    held = [(1440 + 600, 25), (1440 + 600, 25), (2880 + 60, 30), (2880 + 60, 30)]

    assert held_work_minimum_cap(held, 4320) == 60
    assert held_work_minimum_cap([], 4320) == 0
    assert held_work_minimum_cap(held, 1440) == 0  # one day: no daily cap at all
    assert held_work_minimum_cap([(5000, 25)], 4320) == 0  # past the horizon: infeasible anyway


def test_run_schedule__one_hour_held_to_tomorrow_evening__all_of_it_that_evening(
    before_the_horizon,
):
    """The bug's request, at the solver: three 20-minute tasks for tomorrow evening are
    placed tomorrow evening, none today and none split off to another day."""
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk
    from app.services.planning import scheduling as sep

    chunks = [TaskChunk(task_id=f"sql_{i}", title=f"SQL {i}", duration_minutes=20,
                        difficulty_weight=0.5, dependencies=[], completion_criteria="done",
                        not_before="2026-11-17T18:00")
              for i in range(3)]
    graph = ExecutionGraph(goal_metadata=GoalMetadata(), decomposition=chunks,
                           cognitive_load_estimate={"intrinsic_load": 0.5})

    resp = sep.run_schedule(graph, [], horizon_minutes=2880)

    midnight = 1440 + 960
    assert all(TOMORROW_6PM_MINUTE <= t.start_min and t.end_min <= midnight
               for t in resp.schedule.values())
