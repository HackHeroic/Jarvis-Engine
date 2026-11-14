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

Usage, from the Jarvis-Engine directory:
    .venv/bin/python scripts/seed_demo_user.py [--user demo] [--days 21] [--reset] [--dry-run]

It refuses any user but ``demo``. Without ``--reset`` a demo user who already has
task events is left alone (exit 0). ``--reset`` first deletes the demo user's rows,
every one scoped by user_id. ``--dry-run`` seeds an in-memory store with no network
and prints the same report. Credentials come from .env and are never printed.
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
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.core.config as config  # loads .env the way the app does
from app.api.v1.endpoints.reasoning import TaskChunk
from app.container import AppContainer
from app.domain.entities import AutonomyMode, MemorySource, TaskEvent
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
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from app.services.memory.patterns import PatternService
from app.services.planning.local_time import local_today
from app.services.task_service import TaskService

DEMO_USER_ID = "demo"
DEMO_TIMEZONE = "Asia/Kolkata"
DEFAULT_DAYS = 21
SEED = 42

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
        raise RuntimeError(f"The seed's outcome for {task_id} was not recorded.")


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
    """Seed the user unless they already have history; the report, or None when left alone."""
    _require_demo(user_id)
    existing = await repos.task_events.list_for_user(user_id)
    if existing:
        print(f"{user_id} already seeded ({len(existing)} events) — pass --reset to rebuild")
        return None
    clock = SimClock(datetime.combine(today - timedelta(days=days), time(0, 0), zone))
    return await seed_history(
        build_seed_services(repos, embedder, clock), user_id,
        days=days, today=today, zone=zone, rng=random.Random(SEED),
    )


def reset_user(client: Any, user_id: str) -> None:
    """Delete the demo user's rows: usage first, the profile last (its cascade takes the rest).

    Every delete is scoped by user_id. Raises ValueError for any other user, before
    anything is touched, and RuntimeError if rows of the tables this script writes
    are still there afterwards.
    """
    _require_demo(user_id)
    for table in _RESET_ORDER:
        client.table(table).delete().eq("user_id", user_id).execute()
    left = {
        table: len(client.table(table).select("user_id").eq("user_id", user_id).execute().data)
        for table in _RESET_ORDER
    }
    if any(left.values()):
        raise RuntimeError(f"rows left behind after the reset: {left}")


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


def _format(report: dict) -> str:
    events = ", ".join(f"{kind} {count}" for kind, count in report["events_by_type"].items())
    return "\n".join([
        f"Seeded {DEMO_USER_ID}: {report['days']} days, {report['tasks']} tasks,"
        f" {report['memories']} memories",
        f"  events: {events}",
        f"  patterns PEARL remembered: {', '.join(report['patterns']) or 'none'}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", default=DEMO_USER_ID, help="must be 'demo'")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="days of history")
    parser.add_argument("--reset", action="store_true", help="delete the demo user's rows first")
    parser.add_argument("--dry-run", action="store_true",
                        help="seed an in-memory store instead (no network)")
    args = parser.parse_args(argv)

    if args.user != DEMO_USER_ID:
        print(f"Refusing to touch user {args.user!r}: this script only seeds {DEMO_USER_ID!r}.")
        return 2
    if not 1 <= args.days <= config.FOCUS_WINDOW_DAYS:
        print(f"--days must be between 1 and {config.FOCUS_WINDOW_DAYS}.")
        return 2

    zone = ZoneInfo(DEMO_TIMEZONE)
    if args.dry_run:
        client, embedder = None, _OfflineEmbedder()
    else:
        client = _connect()
        if client is None:
            print("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set in .env; nothing to seed.")
            return 1
        embedder = MiniLMEmbedder()
        if args.reset:
            reset_user(client, args.user)
            print(f"Deleted every row of {args.user!r}.")

    container = AppContainer.build(
        supabase_client=client, cfg=config, hooks=ActionHooks(), embedder=embedder,
        install_chain=False,
    )
    report = asyncio.run(run(
        container.repos, container.embedder, args.user,
        days=args.days, today=local_today(zone), zone=zone,
    ))
    if report is not None:
        print(_format(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
