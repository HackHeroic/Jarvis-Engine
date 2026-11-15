"""Tasks, rules, goals and habit trackers each have one persistence path.

Endpoints and services read and write them through the repository ports the
composition root builds; no raw table call is left in the files that used to make
them. The lists the dashboard and the Habits page read keep their shape and count,
and the deadline scan and the bring-back phrase change a status only through
``TaskService``.
"""

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import TaskEvent, TaskEventType
from tests.fakes import FakeDBClient, FakeSupabase, build_test_container

U = "alice"
_APP_DIR = Path(__file__).resolve().parents[1] / "app"
# What the dashboard reads off each task (jarvis-frontend lib/transforms.ts).
_DASHBOARD_FIELDS = {
    "task_id", "title", "status", "duration_minutes", "scheduled_start", "scheduled_end",
    "goal_id", "deadline_hint",
}
_ENVELOPE = {"code", "message", "details", "request_id"}


def _client(monkeypatch, fake: FakeSupabase):
    """The app over a container on ``fake``, as the lifespan wires it (no lifespan run)."""
    from app.main import app

    container = build_test_container(fake)
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(fake), raising=False)
    monkeypatch.setattr(app.state, "container", container, raising=False)
    return TestClient(app, raise_server_exceptions=False), container


def _task_row(i: int, *, user_id: str = U, status: str = "pending", **fields) -> dict:
    """A user_tasks row as draft acceptance writes it, created i minutes after the first."""
    first = datetime(2026, 11, 1, 2, 30, tzinfo=timezone.utc)
    start = first + timedelta(hours=i)
    return {
        "id": str(uuid4()), "user_id": user_id, "task_id": f"g{i % 3}_run_t{i:02}",
        "goal_id": f"g{i % 3}", "plan_id": str(uuid4()), "title": f"Task {i}",
        "status": status, "duration_minutes": 25, "difficulty_weight": 0.5, "dependencies": [],
        "scheduled_start": start.isoformat(),
        "scheduled_end": (start + timedelta(minutes=25)).isoformat(),
        "deadline_hint": "2026-12-01" if i % 2 else None,
        "completion_criteria": "Explain it back",  # a column the Task entity does not model
        "created_at": (first + timedelta(minutes=i)).isoformat(),
        **fields,
    }


# --- the lists the pages read --------------------------------------------------------


def test_list_tasks__seeded_size__every_field_the_dashboard_reads_newest_first(monkeypatch):
    statuses = ["pending", "completed", "skipped", "missed"]
    rows = [_task_row(i, status=statuses[i % 4]) for i in range(90)]  # the seeded demo's size
    theirs = _task_row(90, user_id="bob")
    client, _ = _client(monkeypatch, FakeSupabase({"user_tasks": [*rows, theirs]}))

    resp = client.get("/api/v1/tasks/", params={"user_id": U})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == len(body["tasks"]) == 90
    newest_first = list(reversed(rows))
    assert [t["task_id"] for t in body["tasks"]] == [r["task_id"] for r in newest_first]
    for task, row in zip(body["tasks"], newest_first):
        assert _DASHBOARD_FIELDS <= set(task)
        assert {k: task[k] for k in ("title", "status", "duration_minutes", "goal_id",
                                     "deadline_hint")} == {
            k: row[k] for k in ("title", "status", "duration_minutes", "goal_id", "deadline_hint")
        }
        for slot in ("scheduled_start", "scheduled_end"):
            assert datetime.fromisoformat(task[slot]) == datetime.fromisoformat(row[slot])
    assert theirs["task_id"] not in {t["task_id"] for t in body["tasks"]}


def test_habits_constraints__same_rules_as_the_you_page(monkeypatch):
    now = datetime.now(timezone.utc)

    def rule(text: str, *, days_ago: int, user_id: str = U, **window) -> dict:
        return {
            "id": str(uuid4()), "user_id": user_id, "raw_text": text, "constraint_type": "habit",
            "source": "said", "created_at": (now - timedelta(days=days_ago)).isoformat(),
            **{k: v.isoformat() for k, v in window.items()},
        }

    older = rule("No work after 10 PM", days_ago=9)
    expired = rule("Gym at 7 until the exams", days_ago=8, valid_until=now - timedelta(days=1))
    not_yet = rule("Choir on Fridays", days_ago=7, valid_from=now + timedelta(days=3))
    newer = rule("No study before 11 AM", days_ago=1)
    theirs = rule("Bob's rule", days_ago=2, user_id="bob")
    fake = FakeSupabase({"behavioral_constraints": [older, expired, not_yet, newer, theirs]})
    client, _ = _client(monkeypatch, fake)

    habits = client.get("/api/v1/habits/constraints", params={"user_id": U})
    you = client.get("/api/v1/you", params={"user_id": U})

    assert habits.status_code == you.status_code == 200
    listed = habits.json()["constraints"]
    assert [r["id"] for r in listed] == [newer["id"], older["id"]]  # newest first
    assert {r["id"] for r in you.json()["rules"]} == {newer["id"], older["id"]}
    for row, seeded in zip(listed, (newer, older)):
        assert set(row) == {"id", "raw_text", "constraint_type", "created_at"}
        assert (row["raw_text"], row["constraint_type"]) == (seeded["raw_text"], "habit")
        assert datetime.fromisoformat(row["created_at"]) == datetime.fromisoformat(
            seeded["created_at"]
        )


class _Unreachable:
    """A database client whose every call fails the way a dropped connection does."""

    def table(self, name: str):
        raise ConnectionError("connection refused: postgres://admin:hunter2@db.internal")


@pytest.mark.parametrize(
    "path", ["/api/v1/tasks/", "/api/v1/habits/constraints", "/api/v1/habits/tracker/due"]
)
def test_task_and_rule_lists__store_unreachable__503_never_an_empty_list(monkeypatch, path):
    from app.main import app

    monkeypatch.setattr(app.state, "db_client", FakeDBClient(_Unreachable()), raising=False)
    monkeypatch.setattr(
        app.state, "container", build_test_container(_Unreachable()), raising=False
    )

    resp = TestClient(app, raise_server_exceptions=False).get(path, params={"user_id": U})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "persistence_unavailable"
    assert "hunter2" not in resp.text


def test_task_routes__patch_and_deletes__gone_405_or_404(monkeypatch):
    fake = FakeSupabase({"user_tasks": [_task_row(1)]})
    client, _ = _client(monkeypatch, fake)
    task_id = fake.rows["user_tasks"][0]["task_id"]

    patched = client.patch(f"/api/v1/tasks/{task_id}", json={"user_id": U, "title": "Renamed"})
    deleted = client.delete(f"/api/v1/tasks/{task_id}", params={"user_id": U})
    cleared = client.delete("/api/v1/tasks/", params={"user_id": U})

    assert (patched.status_code, deleted.status_code) == (404, 404)
    assert cleared.status_code == 405  # GET /tasks/ remains at that path
    for resp in (patched, deleted, cleared):
        assert set(resp.json()["error"]) == _ENVELOPE
    (row,) = fake.rows["user_tasks"]
    assert (row["title"], row["status"]) == ("Task 1", "pending")  # nothing changed


# --- mastery reads the outcome history -----------------------------------------------


def _completed(task_id: str, at: datetime, quality: int | None, *, user_id: str = U) -> TaskEvent:
    return TaskEvent(
        user_id=user_id, task_id=task_id, event_type=TaskEventType.COMPLETED, occurred_at=at,
        scheduled_start=at, scheduled_end=at + timedelta(minutes=25),
        metadata={"goal_id": "g1", "quality": quality},
    )


@pytest.mark.asyncio
async def test_mastery__quality_from_completed_events__trend_improving():
    from app.services.analytical.mastery_tracker import compute_quality_trend

    events = build_test_container(FakeSupabase()).repos.task_events
    now = datetime.now(timezone.utc)
    # Oldest first: five completions rated 2, then five rated 4.
    for i, quality in enumerate([2, 2, 2, 2, 2, 4, 4, 4, 4, 4]):
        await events.append(U, _completed(f"t{i}", now - timedelta(days=12 - i), quality))
    await events.append("bob", _completed("b1", now - timedelta(days=1), 5, user_id="bob"))

    assert await compute_quality_trend(U, events) == "improving"
    # Fewer than four rated completions: too few to call a trend.
    assert await compute_quality_trend("bob", events) == "stable"
    assert await compute_quality_trend("nobody", events) == "stable"


@pytest.mark.asyncio
async def test_success_ratio__latest_outcome_per_slot__missed_then_completed_counts_once():
    from app.services.analytical.mastery_tracker import calculate_success_ratio

    events = build_test_container(FakeSupabase()).repos.task_events
    now = datetime.now(timezone.utc)

    def outcome(kind: str, task_id: str, slot: datetime, at: datetime, goal_id: str = "g1"):
        return TaskEvent(
            user_id=U, task_id=task_id, event_type=TaskEventType(kind), occurred_at=at,
            scheduled_start=slot, scheduled_end=slot + timedelta(minutes=25),
            metadata={"goal_id": goal_id},
        )

    a, b, c, d, e = (now - timedelta(days=n) for n in (5, 4, 3, 2, 1))
    await events.append_many(U, [
        # The slot passed, then the user did it after all: one slot, completed.
        outcome("missed", "a", a, a + timedelta(hours=1)),
        outcome("completed", "a", a, a + timedelta(hours=5)),
        outcome("skipped", "b", b, b),
        outcome("completed", "c", c, c),
        outcome("scheduled", "c2", c, c),  # not an outcome
        outcome("missed", "d", d, d, goal_id="g2"),
        outcome("completed", "e", e, e, goal_id="g2"),
        # Older than the window: not counted.
        outcome("skipped", "old", now - timedelta(days=70), now - timedelta(days=70)),
    ])

    assert await calculate_success_ratio(U, "g1", events) == pytest.approx(2 / 3)
    assert await calculate_success_ratio(U, "g2", events) == pytest.approx(1 / 2)
    assert await calculate_success_ratio(U, None, events) == pytest.approx(3 / 5)
    assert await calculate_success_ratio("nobody", None, events) == 0.5  # the neutral prior


# --- status changes go through TaskService -------------------------------------------


@pytest.mark.asyncio
async def test_deadline_scan__always_through_task_service__deadline_missed_event_per_task():
    from app.services.analytical.missed_deadlines import detect_and_mark_missed

    rows = [
        _task_row(1, deadline_hint="2020-01-01"),
        _task_row(2, deadline_hint="2999-01-01"),
        _task_row(3, deadline_hint="2020-02-01"),
        _task_row(4, status="completed", deadline_hint="2020-01-01"),
        _task_row(5, user_id="bob", deadline_hint="2020-01-01"),
    ]
    fake = FakeSupabase({"user_tasks": rows})
    container = build_test_container(fake)

    overdue = await detect_and_mark_missed(U, container.repos.tasks, container.task_service)

    passed = {rows[0]["task_id"], rows[2]["task_id"]}
    assert {o["task_id"] for o in overdue} == passed
    events = fake.rows["task_events"]
    assert sorted((e["task_id"], e["event_type"]) for e in events) == sorted(
        (task_id, "deadline_missed") for task_id in passed
    )
    statuses = {(r["user_id"], r["task_id"]): r["status"] for r in fake.rows["user_tasks"]}
    assert [key for key, status in statuses.items() if status == "missed"] == [
        (U, rows[0]["task_id"]), (U, rows[2]["task_id"])
    ]
    # The no-service write path is gone: the scan takes the repository and the service.
    assert list(inspect.signature(detect_and_mark_missed).parameters) == [
        "user_id", "tasks", "task_service", "zone"
    ]


@pytest.mark.asyncio
async def test_restore_missed__through_bring_back__pending_and_deadline_cleared():
    from app.services.analytical.missed_deadlines import restore_missed

    first = _task_row(1, status="missed", deadline_hint="2020-01-01")
    second = _task_row(2, status="missed", deadline_hint="2020-02-01")
    planned = _task_row(3, deadline_hint="2999-01-01")
    theirs = _task_row(4, user_id="bob", status="missed", deadline_hint="2020-01-01")
    fake = FakeSupabase({"user_tasks": [first, second, planned, theirs]})
    container = build_test_container(fake)

    restored = await restore_missed(U, container.repos.tasks, container.task_service)

    assert restored == [
        {"task_id": second["task_id"], "title": second["title"]},  # newest first
        {"task_id": first["task_id"], "title": first["title"]},
    ]
    by_id = {(r["user_id"], r["task_id"]): r for r in fake.rows["user_tasks"]}
    for row in (first, second):
        back = by_id[(U, row["task_id"])]
        assert (back["status"], back["deadline_hint"]) == ("pending", None)
    assert by_id[(U, planned["task_id"])]["deadline_hint"] == "2999-01-01"
    assert by_id[("bob", theirs["task_id"])]["status"] == "missed"
    # Bringing a task back is not an outcome: the history learning reads is untouched.
    assert fake.rows.get("task_events", []) == []
    assert await restore_missed(U, container.repos.tasks, container.task_service) == []


# --- no second path --------------------------------------------------------------------

_ONE_PATH_FILES = (
    "api/v1/endpoints/tasks.py",
    "api/v1/endpoints/habits.py",
    "services/analytical/missed_deadlines.py",
    "services/analytical/mastery_tracker.py",
    "services/analytical/sm2_engine.py",
    "services/analytical/rule_check_ins.py",
    "services/extraction/behavioral_store.py",
)


def _table_calls(path: Path) -> list[str]:
    """``<line>: .table(...)`` for every ``X.table(...)`` call in ``path``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        f"{node.lineno}: .table(...)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "table"
    ]


@pytest.mark.parametrize("relative", _ONE_PATH_FILES)
def test_tasks_and_habits_endpoints__no_raw_table_calls(relative):
    assert _table_calls(_APP_DIR / relative) == []


def test_raw_table_check__planted_call__is_caught(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text("rows = client.table('user_tasks').select('*').execute()\n")

    assert _table_calls(planted) == ["1: .table(...)"]
