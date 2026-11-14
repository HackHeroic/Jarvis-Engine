#!/usr/bin/env python3
"""Seed about three weeks of history for the ``demo`` profile: seeded history, live inference.

Every row is written by a live service, run under a simulated clock, so the seed can
only write the row and event shapes the app itself writes:

- each past day's plan is a draft (``DraftRepository.create``) accepted by
  ``DraftService.accept`` at 07:30, which writes ``user_tasks`` and the ``scheduled``
  (or ``rescheduled``) events;
- outcomes go through ``TaskService.complete`` / ``skip`` just after each slot, and at
  23:59 ``TaskService.sweep_passed_slots`` sets aside whatever was left untouched;
- three things the user said are remembered by ``MemoryService.remember`` on their
  own dates;
- at the end PEARL (``PatternService.observe``) looks at the history once, as the
  app does after outcomes, and remembers what it finds.

Nothing is computed here that the app infers: PEARL, the focus profile and the
morning brief read this history for real. The regularities (rng seed 42) are what
those readers should find: weekday 09:00 DSA practice mostly set aside, evenings
mostly done, a weekly OS assignment finished in a Friday-morning crunch, afternoons
about half done. Mornings are seeded with set-aside study blocks as well as the DSA
slot, because hours with no history keep the prior's high morning scores.

No model is ever called. Times are on the demo user's clock (Asia/Kolkata).

Usage, from the Jarvis-Engine directory, on the demo day, with the backend stopped:
    .venv/bin/python scripts/seed_demo_user.py [--user demo] [--days 21] [--reset]
        [--dry-run] [--base-url http://localhost:8000] [--allow-running-backend]

It refuses any user but ``demo``, and refuses to run while a backend answers at
``--base-url`` (its slot sweep runs on the real clock and would set half-seeded
tasks aside; ``JARVIS_ANTICIPATION=false`` keeps it from starting). Before anything
is deleted it warms the embedder and probes the schema the seed needs. A demo user
who is already seeded is left alone (exit 0); a partial seed, or demo rows with no
seeded history, is never merged into. ``--reset`` deletes the demo user's rows, every
one scoped by user_id. After seeding, a self-check confirms the history is the seed's
alone. ``--dry-run`` seeds an in-memory store with no network. Credentials come from
.env and are never printed.

Exit codes: 0 seeded or already seeded; 1 a partial seed, a failed self-check or a
reset that left rows; 2 a refused user, an unmigrated database or no embedder
(nothing deleted); 3 a backend is running.
"""

import argparse
import asyncio
import hashlib
import math
import os
import random
import re
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import app.core.config as config  # loads .env the way the app does
from app.api.v1.endpoints.reasoning import TaskChunk
from app.container import AppContainer
from app.domain.entities import (
    AutonomyMode,
    MemorySource,
    MemoryStatus,
    TaskEvent,
    TaskEventType,
)
from app.domain.events import EventBus
from app.domain.ports import (
    DraftRepository,
    ProfileRepository,
    TaskEventRepository,
    TextEmbedder,
)
from app.infrastructure.embedding import MiniLMEmbedder
from app.infrastructure.persistence.factory import Repositories
from app.orchestrator.hooks import ActionHooks
from app.services.draft_service import DraftService
from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from app.services.memory.patterns import PatternService
from app.services.planning.local_time import local_today
from app.services.task_service import SLOT_OUTCOMES, TaskService, as_aware

DEMO_USER_ID = "demo"
DEMO_TIMEZONE = "Asia/Kolkata"
DEFAULT_DAYS = 21
MIN_DAYS = 7  # fewer days than a week give the detectors too little to find
SEED = 42
DEFAULT_BASE_URL = "http://localhost:8000"  # where a running backend would answer
BACKEND_PROBE_TIMEOUT_S = 1.0

ACCEPT_AT = time(7, 30)  # each day's plan is accepted before the day starts
CRUNCH_AT = time(8, 0)  # Friday morning: the week's OS chunks, one after another
CRUNCH_STEP = timedelta(minutes=10)
SAID_AT = time(21, 30)  # when the user told Jarvis something, on its day
SWEEP_AT = time(23, 59)  # the slot sweep sets aside what was left untouched
OUTCOME_LAG = timedelta(minutes=5)  # an outcome is recorded just after its slot ends
RESLOT_AT = time(18, 0)  # a 09:00 DSA slot left untouched finds a new slot next evening

DONE, SKIPPED, UNTOUCHED = "done", "skipped", "untouched"
WEEKDAYS = frozenset(range(5))
EVERY_DAY = frozenset(range(7))
MON_TO_WED = frozenset({0, 1, 2})
FRIDAY = 4

# Tables reset_user clears, in order. Usage rows go first: deleting the profile only
# sets their user to null (ON DELETE SET NULL). The tables this script writes follow,
# by user_id. The profile goes last; on the live database its ON DELETE CASCADE takes
# every other user table (chat, rules, suggestions, goals...).
_RESET_ORDER = (
    "llm_usage_events",
    "task_events",
    "user_tasks",
    "draft_schedules",
    "user_memories",
    "user_profiles",
)

# Only the profile's cascade reaches these; the check after a reset reads them too.
_CASCADE_ONLY = (
    "goals",
    "proactive_suggestions",
    "behavioral_constraints",
    "chat_messages",
    "chat_sessions",
)
# The preflight's schema probes: what migrations 20261115000000-0004 add, as
# (table, columns) the seed's own writes need.
_SCHEMA_PROBES = (
    ("task_events", "id,task_id,event_type,occurred_at,scheduled_start,scheduled_end,metadata"),
    ("user_memories", "status,pattern_key,provenance,strength_as_of"),
    ("proactive_suggestions", "consent_kind"),
)
# Bounds for "any scheduled task" in the already-seeded check.
_ALL_TIME = (datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc))
# An outcome stamped between the run's real start and end (give or take this) was
# written on the real clock, not the seed's.
_CLOCK_SLACK = timedelta(minutes=1)

# (days before today, memory type, what the user said). Recent enough to stay above
# the retention floor today, so the You page shows them.
_SAID = (
    (12, "preference", "I think most clearly in the evenings, after about 7 PM."),
    (7, "fact", "My OS assignment is due every Friday."),
    (2, "goal", "I'm preparing for placement interviews, so DSA matters most this semester."),
)


@dataclass(frozen=True)
class _Routine:
    """One recurring piece of the demo user's week, and how it tends to go.

    ``mix`` is (outcome, percent) in tie-break order. ``starts`` rotates, one per
    occurrence; outcomes are dealt per start time, so every hour gets its share.
    ``titles`` rotates too, for variety in what the You page shows.
    """

    goal_id: str
    titles: tuple[str, ...]
    difficulty: float
    minutes: int
    criteria: str
    obstacle: str
    response: str
    days: frozenset[int]
    starts: tuple[time, ...]
    mix: tuple[tuple[str, int], ...]


_DSA = _Routine(
    goal_id="dsa_practice",
    titles=("DSA practice: arrays", "DSA practice: linked lists", "DSA practice: trees",
            "DSA practice: graphs", "DSA practice: dynamic programming"),
    difficulty=0.8, minutes=25,
    criteria="Solve two problems without looking at the solutions",
    obstacle="If I reach for my phone before the first problem",
    response="then put it in the other room and read the problem aloud",
    days=WEEKDAYS, starts=(time(9, 0),),
    mix=((SKIPPED, 80), (UNTOUCHED, 10), (DONE, 10)),
)
_LECTURES = _Routine(
    goal_id="lecture_review",
    titles=("Revise DBMS lecture notes", "Revise Computer Networks lecture notes",
            "Revise Operating Systems lecture notes"),
    difficulty=0.5, minutes=25,
    criteria="Write three recall questions and answer them from memory",
    obstacle="If the notes feel too long to start",
    response="then revise only the first heading and stop there if I want",
    days=WEEKDAYS, starts=(time(10, 0), time(11, 0), time(12, 0)),
    mix=((DONE, 40), (SKIPPED, 35), (UNTOUCHED, 25)),
)
_LAB = _Routine(
    goal_id="lab_report",
    titles=("Write the networks lab report: results", "Write the DBMS lab report: queries"),
    difficulty=0.4, minutes=25,
    criteria="Finish one section of the report with its table or figure",
    obstacle="If I get stuck on the write-up",
    response="then fill in the results table first and come back to the prose",
    days=WEEKDAYS, starts=(time(14, 0), time(15, 0), time(16, 0), time(17, 0)),
    mix=((DONE, 50), (SKIPPED, 25), (UNTOUCHED, 25)),
)
_PROJECT = _Routine(
    goal_id="course_project",
    titles=("Course project: build the search API", "Course project: write the tests",
            "Course project: fix the open bugs"),
    difficulty=0.7, minutes=25,
    criteria="Commit one working change with a test",
    obstacle="If the build breaks and I want to stop",
    response="then write down the error and try one fix for five minutes",
    days=EVERY_DAY, starts=(time(19, 0),),
    mix=((DONE, 90), (SKIPPED, 5), (UNTOUCHED, 5)),
)
_FLASHCARDS = _Routine(
    goal_id="flashcards",
    titles=("Flashcards: OS and DBMS",),
    difficulty=0.3, minutes=20,
    criteria="Review every due card once",
    obstacle="If I feel like skipping the review tonight",
    response="then do ten cards before deciding",
    days=WEEKDAYS, starts=(time(20, 0),),
    mix=((DONE, 90), (SKIPPED, 10)),
)
# The weekly OS assignment: due Friday; its Mon-Wed chunks go untouched, and the
# whole week's share is done on Friday morning ("I actually did that").
_OS = _Routine(
    goal_id="os_assignment",
    titles=("OS assignment, part 1: process scheduling",
            "OS assignment, part 2: memory management",
            "OS assignment, part 3: file systems"),
    difficulty=0.6, minutes=25,
    criteria="Finish the part and run its test cases",
    obstacle="If the assignment feels too big to start",
    response="then open the template and write the first function signature",
    days=MON_TO_WED, starts=(time(19, 30),),
    mix=((UNTOUCHED, 100),),
)
_ROUTINES = (_DSA, _LECTURES, _LAB, _PROJECT, _FLASHCARDS, _OS)


class SeedProblem(Exception):
    """Why the seed stopped, in words the operator can act on, and the exit code to return."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class SimClock:
    """The seed's clock. Every service reads "now" from it; the seed moves it forward.

    Instants are aware and read back in UTC, as the services' own clock gives them.
    It never goes back: the history is written in the order it happened.
    """

    def __init__(self, start: datetime) -> None:
        self._now = _utc(start)

    def __call__(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        moment = _utc(moment)
        if moment < self._now:
            raise ValueError(f"The seed's clock only moves forward ({moment} < {self._now}).")
        self._now = moment


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("The seed's clock takes aware instants only.")
    return moment.astimezone(timezone.utc)


@dataclass
class SeedServices:
    """The live services the seed writes through, all on one simulated clock."""

    drafts: DraftService
    draft_repo: DraftRepository
    tasks: TaskService
    profiles: ProfileRepository
    memory: MemoryService
    clock: SimClock
    patterns: PatternService
    task_events: TaskEventRepository


def build_seed_services(
    repos: Repositories, embedder: TextEmbedder, clock: SimClock
) -> SeedServices:
    """The services the app runs, over ``repos``, reading ``clock``.

    Nothing listens on their bus: PEARL looks once, explicitly, at the end.
    """
    bus = EventBus()
    return SeedServices(
        drafts=DraftService(repos.drafts, bus, task_events=repos.task_events, clock=clock),
        draft_repo=repos.drafts,
        tasks=TaskService(repos.tasks, repos.task_events, bus, clock=clock),
        profiles=repos.profiles,
        memory=MemoryService(repos.memories, embedder, clock=clock),
        clock=clock,
        patterns=PatternService(
            repos.task_events, repos.memories, repos.profiles, bus, clock=clock
        ),
        task_events=repos.task_events,
    )


def _require_demo(user_id: str) -> None:
    if user_id != DEMO_USER_ID:
        raise ValueError(f"This script only touches the {DEMO_USER_ID!r} profile.")


@dataclass
class _Slot:
    """A task in one day's plan: the chunk the draft carries, its start, what happens."""

    chunk: dict
    start: datetime
    outcome: str

    @property
    def task_id(self) -> str:
        return self.chunk["task_id"]

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.chunk["duration_minutes"])


def _deal(rng: random.Random, n: int, mix: tuple[tuple[str, int], ...]) -> list[str]:
    """``n`` outcomes in the mix's proportions (largest remainder, ties in mix order), shuffled."""
    counts = {outcome: percent * n // 100 for outcome, percent in mix}
    by_remainder = sorted(mix, key=lambda item: -(item[1] * n % 100))  # stable: mix order
    for outcome, _ in by_remainder[: n - sum(counts.values())]:
        counts[outcome] += 1
    hand = [outcome for outcome, _ in mix for _ in range(counts[outcome])]
    rng.shuffle(hand)
    return hand


def _chunk(routine: _Routine, title: str, task_id: str, deadline: date | None) -> dict:
    """A planner-shaped task: a valid TaskChunk with its WOOP if-then, plus its goal_id."""
    chunk = TaskChunk(
        task_id=task_id,
        title=title,
        duration_minutes=routine.minutes,
        difficulty_weight=routine.difficulty,
        completion_criteria=routine.criteria,
        implementation_intention={
            "obstacle_trigger": routine.obstacle,
            "behavioral_response": routine.response,
        },
        deadline_hint=deadline.isoformat() if deadline is not None else None,
    ).model_dump()
    chunk["goal_id"] = routine.goal_id
    return chunk


def _plan_days(
    days: list[date], rng: random.Random
) -> dict[date, list[tuple[_Routine, str, time, str]]]:
    """Each day's (routine, title, start, outcome), outcomes dealt per routine and hour."""
    plan: dict[date, list[tuple[_Routine, str, time, str]]] = {day: [] for day in days}
    for routine in _ROUTINES:
        occurrences = [day for day in days if day.weekday() in routine.days]
        by_start: dict[time, list[tuple[int, date]]] = {}
        for k, day in enumerate(occurrences):
            by_start.setdefault(routine.starts[k % len(routine.starts)], []).append((k, day))
        for start, group in by_start.items():
            for (k, day), outcome in zip(group, _deal(rng, len(group), routine.mix)):
                title = routine.titles[k % len(routine.titles)]
                plan[day].append((routine, title, start, outcome))
    return plan


def _run_token(rng: random.Random, used: set[str]) -> str:
    """Six hex characters naming one planning run, as ``mint_run_token`` does; never reused."""
    while True:
        token = f"{rng.getrandbits(24):06x}"
        if token not in used:
            used.add(token)
            return token


def _new_slots(
    day: date,
    planned: list[tuple[_Routine, str, time, str]],
    run: str,
    zone: ZoneInfo,
    due_friday: dict[date, list[str]],
) -> list[_Slot]:
    """The day's new tasks in start order, ids ``{goal_id}_{run}_task_{n}`` as the planner
    names them. An OS chunk is due that week's Friday and is filed under it."""
    slots = []
    for routine, title, start, outcome in sorted(planned, key=lambda item: item[2]):
        deadline = day + timedelta(days=FRIDAY - day.weekday()) if routine is _OS else None
        task_id = f"{routine.goal_id}_{run}_task_{len(slots) + 1}"
        chunk = _chunk(routine, title, task_id, deadline)
        slots.append(_Slot(chunk, datetime.combine(day, start, zone), outcome))
        if deadline is not None:
            due_friday.setdefault(deadline, []).append(task_id)
    return slots


async def _accept_plan(
    s: SeedServices, user_id: str, slots: list[_Slot], horizon: datetime, *, at: datetime
) -> None:
    """Propose the day as a draft (minute 0 = ``horizon``) and accept it at ``at``."""
    draft = await s.draft_repo.create(
        user_id,
        tasks=[slot.chunk for slot in slots],
        horizon_start=horizon.isoformat(),
        goal_id=None,
        schedule={
            slot.task_id: {
                "start_min": int((slot.start - horizon).total_seconds() // 60),
                "end_min": int((slot.end - horizon).total_seconds() // 60),
            }
            for slot in slots
        },
    )
    s.clock.set(at)
    await s.drafts.accept(user_id, draft.id)


async def _recorded(write: Awaitable[TaskEvent | None], task_id: str) -> None:
    """Await an outcome; one that wrote nothing means the history is not what was planned."""
    if await write is None:
        raise SeedProblem(
            f"The outcome for {task_id} could not be recorded: something else changed the"
            " demo user's tasks while seeding (a running backend's slot sweep?). Stop it,"
            " then rerun with --reset."
        )


def _outcomes(
    s: SeedServices, user_id: str, slots: list[_Slot]
) -> list[tuple[datetime, Callable[[], Awaitable[None]]]]:
    """Each done or skipped slot's outcome, to record just after the slot ends."""
    agenda: list[tuple[datetime, Callable[[], Awaitable[None]]]] = []
    for slot in slots:
        if slot.outcome == DONE:
            write = lambda t=slot.task_id: _recorded(s.tasks.complete(user_id, t), t)  # noqa: E731
        elif slot.outcome == SKIPPED:
            write = lambda t=slot.task_id: _recorded(s.tasks.skip(user_id, t), t)  # noqa: E731
        else:  # untouched: the slot sweep sets it aside at the end of the day
            continue
        agenda.append((slot.end + OUTCOME_LAG, write))
    return agenda


async def seed_history(
    s: SeedServices,
    user_id: str,
    *,
    days: int,
    today: date,
    zone: ZoneInfo,
    rng: random.Random,
) -> dict:
    """Write ``days`` days of history before ``today`` through the live services.

    Returns ``{"days", "tasks", "events_by_type", "memories", "patterns"}``: tasks
    written, the stored events counted by type, memories created and the keys of the
    patterns PEARL remembered.
    """
    _require_demo(user_id)
    await s.profiles.get_or_create(user_id)
    await s.profiles.update(
        user_id,
        timezone=zone.key,
        wake_time=time(7, 0),
        display_name="Demo",
        autonomy_mode=AutonomyMode.ASK_FIRST,
    )

    calendar = [today - timedelta(days=days - i) for i in range(days)]
    plan = _plan_days(calendar, rng)
    said = {today - timedelta(days=back): (kind, text) for back, kind, text in _SAID}
    used_tokens: set[str] = set()
    due_friday: dict[date, list[str]] = {}  # OS chunk task ids, by their Friday deadline
    reslot: list[dict] = []  # DSA chunks the sweep set aside, for a new slot the next day
    tasks_written = 0
    memories = 0

    def local(day: date, at: time) -> datetime:
        return datetime.combine(day, at, zone)

    async def say(kind: str, text: str) -> None:
        nonlocal memories
        outcome = await s.memory.remember(
            user_id, MemoryCandidate(memory_type=kind, content=text, confidence=0.9),
            provenance=MemorySource.SAID, origin="conversation",
        )
        if outcome.action == "created":
            memories += 1

    for day in calendar:
        slots = _new_slots(day, plan[day], _run_token(rng, used_tokens), zone, due_friday)
        tasks_written += len(slots)
        # The last day's untouched DSA slot, given a new slot under its own task id:
        # the accept records it as "rescheduled".
        slots += [_Slot(chunk, local(day, RESLOT_AT) + timedelta(minutes=30 * k), DONE)
                  for k, chunk in enumerate(reslot)]
        await _accept_plan(s, user_id, slots, local(day, time(config.DAY_START_HOUR, 0)),
                           at=local(day, ACCEPT_AT))

        agenda = _outcomes(s, user_id, slots)
        if day.weekday() == FRIDAY:  # "I actually did that": the week's OS chunks, done now
            agenda += [
                (local(day, CRUNCH_AT) + CRUNCH_STEP * k,
                 lambda t=task_id: _recorded(s.tasks.complete(user_id, t), t))
                for k, task_id in enumerate(due_friday.pop(day, []))
            ]
        if day in said:
            agenda.append((local(day, SAID_AT), lambda said_today=said[day]: say(*said_today)))
        for at, act in sorted(agenda, key=lambda item: item[0]):
            s.clock.set(at)
            await act()

        s.clock.set(local(day, SWEEP_AT))
        set_aside = await s.tasks.sweep_passed_slots(user_id, s.clock())
        chunks = {slot.task_id: slot.chunk for slot in slots}
        reslot = [chunks[event.task_id] for event in set_aside
                  if event.metadata.get("goal_id") == _DSA.goal_id and event.task_id in chunks]

    s.clock.set(local(today, time(0, 0)))
    changes = await s.patterns.observe(user_id)

    events = await s.task_events.list_for_user(user_id)
    return {
        "days": days,
        "tasks": tasks_written,
        "events_by_type": dict(sorted(Counter(e.event_type.value for e in events).items())),
        "memories": memories,
        "patterns": sorted(change.pattern.pattern_key for change in changes),
    }


async def run(
    repos: Repositories,
    embedder: TextEmbedder,
    user_id: str,
    *,
    days: int,
    today: date,
    zone: ZoneInfo,
) -> dict | None:
    """Seed the user, then check the history is what was planned; the report (with the
    focus profile's ``learned_peak_hour``), or None when the user is already seeded and
    is left alone.

    Raises SeedProblem (exit 1) for a partial seed found before starting, an outcome
    that could not be recorded, or a history that fails the self-check afterwards.
    """
    _require_demo(user_id)
    if await _already_seeded(repos, user_id):
        return None
    started = datetime.now(timezone.utc)
    clock = SimClock(datetime.combine(today - timedelta(days=days), time(0, 0), zone))
    report = await seed_history(
        build_seed_services(repos, embedder, clock), user_id,
        days=days, today=today, zone=zone, rng=random.Random(SEED),
    )
    problems = await _self_check(repos, user_id, report, today=today, zone=zone,
                                 started=started)
    if problems:
        raise SeedProblem(
            "The seeded history is not what was planned:\n"
            + "".join(f"  - {problem}\n" for problem in problems)
            + "Something else may have written to the demo user meanwhile (a running backend?)."
            " Stop it, then rerun with --reset."
        )
    profile = await HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator()).estimate(
        user_id, zone, now=datetime.combine(today, time(0, 0), zone)
    )
    scores = profile.scores()
    report["learned_peak_hour"] = max(range(len(scores)), key=lambda h: (scores[h], -h))
    return report


async def _closing_memory_exists(repos: Repositories, user_id: str) -> bool:
    """Whether PEARL's closing look left a pattern memory, in any state: the seed's last write."""
    patterns = await repos.memories.list(
        user_id, statuses=frozenset(MemoryStatus), types=frozenset({"behavioral_pattern"})
    )
    return any(m.origin == "behavior" and m.pattern_key for m in patterns)


async def _already_seeded(repos: Repositories, user_id: str) -> bool:
    """True (and says so) when the user holds a whole seed. A partial one, or demo rows
    with no seeded history, is a SeedProblem: never merged into, never seeded over."""
    events = await repos.task_events.list_for_user(user_id)
    if events and await _closing_memory_exists(repos, user_id):
        print(f"{user_id} already seeded ({len(events)} events) — pass --reset to rebuild")
        return True
    tasks = {t.task_id for t in await repos.tasks.list_pending(user_id)}
    tasks |= {t.task_id for t in await repos.tasks.list_scheduled_between(user_id, *_ALL_TIME)}
    found = {
        "task events": len(events),
        "tasks": len(tasks),
        "memories": len(await repos.memories.list(user_id, statuses=frozenset(MemoryStatus))),
        "suggestions": len(await repos.suggestions.list(user_id)),
    }
    if any(found.values()):
        what = ", ".join(f"{name}: {count}" for name, count in found.items() if count)
        why = "no closing pattern memory" if events else "no seeded history"
        raise SeedProblem(f"partial seed found — rerun with --reset ({what}; {why}).")
    return False


async def _self_check(
    repos: Repositories, user_id: str, report: dict, *, today: date, zone: ZoneInfo,
    started: datetime,
) -> list[str]:
    """What is off in the seeded history, in plain words; empty when it is as planned.

    Every event happened before today (on the user's clock), and none at a real time
    while this run was writing (``started`` until now: what a backend's sweep would
    stamp); one scheduled event per seeded task; and PEARL's closing look left its
    pattern memory.
    """
    midnight = datetime.combine(today, time(0, 0), zone)
    while_seeding = (started - _CLOCK_SLACK, datetime.now(timezone.utc) + _CLOCK_SLACK)
    events = await repos.task_events.list_for_user(user_id)
    problems = []
    late = [e for e in events if as_aware(e.occurred_at) >= midnight]
    if late:
        problems.append(f"{len(late)} event(s) dated today or later; the seed ends yesterday")
    scheduled = sum(1 for e in events if e.event_type is TaskEventType.SCHEDULED)
    if scheduled != report["tasks"]:
        problems.append(f"{scheduled} scheduled events for {report['tasks']} seeded tasks")
    on_the_real_clock = [
        e for e in events
        if e.event_type in SLOT_OUTCOMES
        and while_seeding[0] <= as_aware(e.occurred_at) <= while_seeding[1]
    ]
    if on_the_real_clock:
        problems.append(
            f"{len(on_the_real_clock)} outcome(s) carry the real clock's time, not the seed's"
        )
    if not await _closing_memory_exists(repos, user_id):
        problems.append("PEARL's closing pattern memory (origin 'behavior') is missing")
    return problems


def reset_user(client: Any, user_id: str) -> None:
    """Delete the demo user's rows: usage first, the profile last (its cascade takes the rest).

    Every delete is scoped by user_id. Raises ValueError for any other user, before
    anything is touched. Afterwards no row of the user may be left in the tables
    deleted here or in those only the cascade reaches; a SeedProblem lists any that is.
    """
    _require_demo(user_id)
    for table in _RESET_ORDER:
        client.table(table).delete().eq("user_id", user_id).execute()
    left = {
        table: len(client.table(table).select("user_id").eq("user_id", user_id).execute().data)
        for table in _RESET_ORDER + _CASCADE_ONLY
    }
    if any(left.values()):
        rows = ", ".join(f"{table}: {count}" for table, count in left.items() if count)
        raise SeedProblem(f"Rows of {user_id!r} were left behind after the reset ({rows}).")


def _backend_answers(base_url: str) -> bool:
    """Whether a backend answers at ``base_url``/health (any HTTP answer, healthy or not)."""
    try:
        httpx.get(f"{base_url.rstrip('/')}/health", timeout=BACKEND_PROBE_TIMEOUT_S,
                  trust_env=False)
    except httpx.HTTPError:
        return False
    return True


def _error_text(exc: Exception) -> str:
    """The text a database error carries: postgrest's APIError keeps it in attributes."""
    return " ".join([str(exc), *(str(getattr(exc, a, "") or "") for a in ("message", "details"))])


async def _preflight(
    client: Any | None, repos: Repositories, embedder: TextEmbedder, user_id: str
) -> None:
    """Everything the seed needs, checked before anything is deleted or written.

    The embedder is warmed (memories are embedded mid-seed). With a database, its
    schema is probed: the tables and columns migrations 20261115000000-0004 add, and
    the accept_draft and match_memories functions. Every probe reads only the user's
    rows. A gap is a SeedProblem (exit 2).
    """
    try:
        (vector,) = await asyncio.to_thread(embedder.embed, ["warm-up"])
    except Exception as exc:
        raise SeedProblem(
            f"The text embedder is not working ({type(exc).__name__}), so memories cannot"
            " be stored. Nothing was deleted.", exit_code=2,
        ) from exc
    if client is None:
        return
    missing = []
    for table, columns in _SCHEMA_PROBES:
        try:
            client.table(table).select(columns).eq("user_id", user_id).limit(1).execute()
        except Exception:
            missing.append(f"{table} ({columns})")
    try:  # a draft that cannot exist: the function answers DRAFT_NOT_FOUND, writing nothing
        client.rpc("accept_draft", {"p_draft_id": str(uuid4()), "p_user_id": user_id,
                                    "p_rows": []}).execute()
    except Exception as exc:
        if "DRAFT_NOT_FOUND" not in _error_text(exc):
            missing.append("function accept_draft")
    try:
        await repos.memories.nearest(
            user_id, vector, statuses=frozenset({MemoryStatus.ACTIVE}), limit=1
        )
    except Exception:
        missing.append("function match_memories")
    if missing:
        raise SeedProblem(
            f"The database lacks what the seed needs ({'; '.join(missing)}):"
            " apply the migrations first (supabase/migrations through 20261115000004)."
            " Nothing was deleted.", exit_code=2,
        )


def _connect() -> Any | None:
    """A Supabase client from .env, or None when it is not configured."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client

    return create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)


class _OfflineEmbedder(TextEmbedder):
    """Hashed bag of words, 384 wide: lets a dry run remember without loading a model."""

    dimensions = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for word in re.findall(r"\w+", text.lower()):
                digest = hashlib.blake2b(word.encode(), digest_size=4).digest()
                vector[int.from_bytes(digest, "big") % self.dimensions] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


def _clock_hour(hour: int) -> str:
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'} ({hour:02d}:00)"


def _format(report: dict) -> str:
    events = ", ".join(f"{kind} {count}" for kind, count in report["events_by_type"].items())
    return "\n".join([
        f"Seeded {DEMO_USER_ID}: {report['days']} days, {report['tasks']} tasks,"
        f" {report['memories']} memories",
        f"  events: {events}",
    ])


def _operator_note(report: dict, today: date) -> str:
    """What the operator needs before the demo: how fresh it is and what the app will show."""
    return "\n".join([
        f"Seeded through yesterday ({today - timedelta(days=1):%a %d %b}).",
        f"Patterns the detectors found: {', '.join(report['patterns']) or 'none'}.",
        f"Learned peak hour: {_clock_hour(report['learned_peak_hour'])}.",
        "Seed on the demo day; rerun with --reset to refresh.",
    ])


async def _seed_demo(args: argparse.Namespace, client: Any | None, embedder: TextEmbedder,
                     zone: ZoneInfo) -> dict | None:
    """Preflight, reset when asked, seed and check: the report, or None if already seeded."""
    container = AppContainer.build(
        supabase_client=client, cfg=config, hooks=ActionHooks(), embedder=embedder,
        install_chain=False,
    )
    await _preflight(client, container.repos, embedder, args.user)
    if args.reset and client is not None:
        reset_user(client, args.user)
        print(f"Deleted every row of {args.user!r}.")
    return await run(
        container.repos, embedder, args.user,
        days=args.days, today=local_today(zone), zone=zone,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", default=DEMO_USER_ID, help="must be 'demo'")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="days of history")
    parser.add_argument("--reset", action="store_true", help="delete the demo user's rows first")
    parser.add_argument("--dry-run", action="store_true",
                        help="seed an in-memory store instead (no network)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="where a running backend would answer (its /health is probed)")
    parser.add_argument("--allow-running-backend", action="store_true",
                        help="seed even though a backend answers (its sweep may corrupt the seed)")
    args = parser.parse_args(argv)

    if args.user != DEMO_USER_ID:
        print(f"Refusing to touch user {args.user!r}: this script only seeds {DEMO_USER_ID!r}.")
        return 2
    if not MIN_DAYS <= args.days <= config.FOCUS_WINDOW_DAYS:
        print(f"--days must be between {MIN_DAYS} and {config.FOCUS_WINDOW_DAYS}.")
        return 2

    zone = ZoneInfo(DEMO_TIMEZONE)
    if args.dry_run:  # in memory: nothing a backend does can reach it
        client, embedder = None, _OfflineEmbedder()
    else:
        if not args.allow_running_backend and _backend_answers(args.base_url):
            print(
                f"A Jarvis backend is answering at {args.base_url}. Its slot sweep runs every"
                " minute on the real clock and would set half-seeded tasks aside. Stop it, or"
                " restart it with JARVIS_ANTICIPATION=false, then run this again"
                " (or pass --allow-running-backend)."
            )
            return 3
        client = _connect()
        if client is None:
            print("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set in .env; nothing to seed.")
            return 1
        embedder = MiniLMEmbedder()

    try:
        report = asyncio.run(_seed_demo(args, client, embedder, zone))
    except SeedProblem as problem:
        print(problem)
        return problem.exit_code
    if report is not None:
        print(_format(report))
        print(_operator_note(report, local_today(zone)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
