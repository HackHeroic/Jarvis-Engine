"""The live end-to-end script's helpers: polling, the user's clock, facts, frames, SKIP, cleanup.

The script itself needs a running server and real credentials, so it is never run
here; only its helpers are.
"""

from datetime import datetime, timezone

import pytest

import scripts.live_e2e as live_e2e


class _FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time(monkeypatch) -> _FakeTime:
    clock = _FakeTime()
    monkeypatch.setattr(live_e2e, "time", clock)
    return clock


def test_poll_until__work_lands_on_the_third_look__returns_it_without_waiting_longer(fake_time):
    answers = iter([None, [], ["the rule"]])

    found = live_e2e.poll_until(lambda: next(answers))

    assert found == ["the rule"]
    assert fake_time.now == 2 * live_e2e.POLL_INTERVAL_S


def test_poll_until__work_never_lands__gives_up_at_the_deadline_with_the_last_value(fake_time):
    found = live_e2e.poll_until(lambda: [])

    assert found == []
    assert live_e2e.BACKGROUND_DEADLINE_S <= fake_time.now <= 20.0 + live_e2e.POLL_INTERVAL_S
    assert set(fake_time.sleeps) == {live_e2e.POLL_INTERVAL_S}


def test_slots_in_window__window_is_read_on_the_users_clock():
    """The 08:00-11:00 check uses the zone the backend plans in, not UTC."""
    from zoneinfo import ZoneInfo

    payload = {"schedule": {
        "horizon_start": "2026-11-16T08:00:00+05:30",
        "schedule": {
            "a": {"title": "Graphs", "start_min": 60, "end_min": 85},    # 09:00 IST = 03:30 UTC
            "b": {"title": "DP", "start_min": 240, "end_min": 265},      # 12:00 IST
        },
    }}

    assert live_e2e.slots_in_window(payload, 8, 11, ZoneInfo("Asia/Kolkata")) == ["Graphs @ Mon 09:00"]
    assert live_e2e.slots_in_window(payload, 8, 11, ZoneInfo("UTC")) == []


# --- the night-owl fact ---------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "I'm a night owl and do my best work after 9 pm.",
    "User is a night-owl.",
    "Self-described NightOwl",
    "NIGHT OWL",
    "Does their best work after 9pm",
    "Works best after 9 p.m.",
    "Prefers deep work after 21:00",
    "Works best late at night",
    "Is most productive at night",
])
def test_matches_fact__night_owl_variants(text):
    """However the model words the fact, the forget check still recognises it."""
    assert live_e2e.matches_fact(text)


@pytest.mark.parametrize("text", [
    "From now on never schedule any study before 11 AM, I'm useless in the mornings.",
    "Finished the graphs revision today",
    "Works best after 9 am",
    "",
    None,
])
def test_matches_fact__other_facts__no_match(text):
    assert not live_e2e.matches_fact(text)


# --- frame order ----------------------------------------------------------------------


def _memory(content: str) -> tuple[str, dict]:
    return ("memory_extracted", {"memory_id": "m1", "content": content, "action": "created"})


def _is_fact(data) -> bool:
    return live_e2e.matches_fact(data.get("content"))


def test_arrives_before_complete__learning_frame_then_complete__true():
    frames = [("phase", {}), _memory("A night owl"), ("message", {"token": "Hi"}), ("complete", {})]

    assert live_e2e.arrives_before_complete(frames, "memory_extracted", where=_is_fact)


def test_arrives_before_complete__learning_frame_after_complete__false():
    frames = [("phase", {}), ("complete", {}), _memory("A night owl")]

    assert not live_e2e.arrives_before_complete(frames, "memory_extracted", where=_is_fact)


def test_arrives_before_complete__only_other_memories__false():
    frames = [_memory("Finished the graphs revision"), ("complete", {})]

    assert not live_e2e.arrives_before_complete(frames, "memory_extracted", where=_is_fact)
    assert not live_e2e.arrives_before_complete([_memory("A night owl")], "memory_extracted")


def test_frame_order__repeated_frames__collapsed_in_arrival_order():
    frames = [("phase", {}), ("phase", {}), _memory("x"), ("message", {}), ("message", {}),
              ("message", {}), ("complete", {})]

    assert live_e2e.frame_order(frames) == "phase x2 > memory_extracted > message x3 > complete"


# --- PASS / FAIL / SKIP ---------------------------------------------------------------


def _fails() -> str:
    raise AssertionError("expected 409, got 200")


def _skips() -> str:
    raise live_e2e.Skip("status nothing_to_say:\nnothing set aside")


def test_run__skips_but_no_failure__exit_zero_and_every_step_printed(capsys):
    run = live_e2e.Run()

    run.check("12 brief", _skips)
    run.check("13 accept the brief", lambda: "accepted", needs=("12 brief",))
    run.check("14 outcomes", lambda: "one event each")

    out = capsys.readouterr().out
    assert "[SKIP] 12 brief: status nothing_to_say: nothing set aside" in out  # one line
    assert "[SKIP] 13 accept the brief: blocked by: 12 brief (SKIP)" in out
    assert "[PASS] 14 outcomes: one event each" in out
    assert run.ok and run.exit_code == 0
    assert run.verdict == "PASSED, 2 skipped"


def test_run__one_failure__exit_one_and_the_step_blocked_by_it_is_a_skip():
    run = live_e2e.Run()

    run.check("2 accept", _fails)
    run.check("3 double accept", lambda: "409", needs=("2 accept",))

    assert [(s.status, s.detail) for s in run.steps] == [
        ("FAIL", "expected 409, got 200"),
        ("SKIP", "blocked by: 2 accept (FAIL)"),
    ]
    assert not run.ok and run.exit_code == 1
    assert run.verdict == "FAILED"


def test_run__every_step_passes__all_passed():
    run = live_e2e.Run()

    run.check("1 brain dump", lambda: "draft")

    assert run.exit_code == 0 and run.verdict == "ALL PASSED"


# --- the brief's plan in user_tasks ---------------------------------------------------


def test_slot_mismatches__slots_from_a_nested_draft_schedule__names_missing_and_moved_tasks():
    draft = {
        "horizon_start": "2026-11-16T08:00:00+05:30",
        "schedule": {"schedule": {
            "a": {"start_min": 60, "end_min": 85},
            "b": {"start_min": 120, "end_min": 145},
            "c": {"start": 180, "end": 205},  # the solver's spelling
            "d": {"start_min": None},  # no slot: not planned
        }},
    }
    tasks = [
        {"task_id": "a", "scheduled_start": "2026-11-16T03:30:00+00:00"},  # 09:00 IST: right
        {"task_id": "b", "scheduled_start": "2026-11-16T05:30:00+00:00"},  # 11:00 IST: moved
    ]

    planned = live_e2e.draft_slots(draft)

    assert planned["a"] == datetime(2026, 11, 16, 3, 30, tzinfo=timezone.utc)
    assert set(planned) == {"a", "b", "c"}
    assert live_e2e.slot_mismatches(planned, tasks) == [
        "b: 2026-11-16T05:30:00+00:00 != 2026-11-16T10:00:00+05:30",
        "c: not written",
    ]


# --- cleanup --------------------------------------------------------------------------


class _MissingTable(Exception):
    """What PostgREST answers for a table that does not exist."""

    code = "PGRST205"


class _Query:
    def __init__(self, db: "_FakeDb", table: str) -> None:
        self.db, self.table, self.op, self.filters = db, table, None, []

    def select(self, columns: str) -> "_Query":
        self.op = "select"
        return self

    def delete(self) -> "_Query":
        self.op = "delete"
        return self

    def eq(self, column: str, value: str) -> "_Query":
        self.filters.append((column, value))
        return self

    def execute(self):
        self.db.calls.append((self.op, self.table, tuple(self.filters)))
        if self.table in self.db.missing:
            raise _MissingTable(self.table)
        stored = self.db.tables.setdefault(self.table, [])
        hits = [r for r in stored if all(r.get(c) == v for c, v in self.filters)]
        if self.op == "delete":
            self.db.tables[self.table] = [r for r in stored if r not in hits]
        return type("Result", (), {"data": hits})()


class _FakeDb:
    def __init__(self, rows: dict[str, list[dict]], missing: set[str] = frozenset()) -> None:
        self.tables = rows
        self.missing = set(missing)
        self.calls: list[tuple] = []

    def table(self, name: str) -> _Query:
        return _Query(self, name)


def test_cleanup__non_e2e_id__refuses_before_touching_the_database():
    db = _FakeDb({"user_tasks": [{"user_id": "demo-user"}]})

    with pytest.raises(ValueError, match="e2e-"):
        live_e2e.cleanup(db, "demo-user")

    assert db.calls == []
    assert db.tables["user_tasks"] == [{"user_id": "demo-user"}]


def test_cleanup__e2e_id__deletes_only_its_rows_everywhere_and_tolerates_an_absent_optional_table():
    me, other = "e2e-20261114-ab12", "someone-else"
    db = _FakeDb(
        {table: [{"user_id": me}, {"user_id": other}] for table in live_e2e.USER_TABLES},
        missing=set(live_e2e.OPTIONAL_USER_TABLES),
    )

    detail = live_e2e.cleanup(db, me)

    assert all(db.tables[t] == [{"user_id": other}] for t in live_e2e.USER_TABLES)
    deletes = [(table, filters) for op, table, filters in db.calls if op == "delete"]
    assert {table for table, _ in deletes} >= set(live_e2e.USER_TABLES)
    assert all(filters == (("user_id", me),) for _, filters in deletes)
    assert [table for table, _ in deletes][-1] == "user_profiles"  # the root goes last
    assert {"task_events", "proactive_suggestions", "user_memories", "goals",
            "behavioral_constraints", "draft_schedules", "llm_usage_events",
            "chat_sessions", "chat_messages"} <= set(live_e2e.USER_TABLES)
    assert "all rows deleted" in detail
