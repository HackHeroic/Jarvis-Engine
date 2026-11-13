"""The CLI migration set, read as text: it must build the whole schema the code expects.

Offline checks only. A real apply against Postgres is out of scope for the suite,
so these tests pin what can go wrong silently in SQL nobody runs locally: a table
the code needs but no migration creates, a statement that fails when re-run, a user
table left readable without row-level security, and an ``accept_draft`` column list
that drifts from the rows the planner builds.
"""

import re
from pathlib import Path

import pytest

from app.domain.entities import DraftStatus, SuggestionStatus, TaskEventType, TaskStatus
from app.services.planning.task_rows import build_task_rows

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"
BASELINE = MIGRATIONS / "20260101000000_baseline_legacy_tables.sql"
CAPSTONE = MIGRATIONS / "20261114000001_capstone_lld.sql"

NEW_TABLES = ("user_profiles", "goals", "task_events", "proactive_suggestions", "llm_usage_events")
LEGACY_USER_TABLES = (
    "user_tasks",
    "draft_schedules",
    "behavioral_constraints",
    "habit_trackers",
    "user_memories",
    "chat_sessions",
    "chat_messages",
    "task_materials",
    "ingested_documents",
    "pending_calendar_updates",
    "user_plan_updates",
    "user_preferences",
    "task_workspaces",
    "task_completion_signals",
)
USER_TABLES = NEW_TABLES + LEGACY_USER_TABLES
BASELINE_TABLES = (
    "behavioral_constraints",
    "habit_trackers",
    "pending_calendar_updates",
    "task_materials",
    "user_plan_updates",
    "ingested_documents",
    "user_tasks",
)

_TABLE = r"(?:public\.)?"


def _sql(path: Path) -> str:
    """The file's text, lower-cased, with `--` comments removed."""
    return re.sub(r"--[^\n]*", "", path.read_text()).lower()


def _all_migrations() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def _check_values(sql: str, constraint: str) -> set[str]:
    """The quoted values in ``constraint``'s ``in (...)`` list, as the file last defines it."""
    found = re.findall(
        rf"constraint\s+{constraint}\s+check\s*\(\s*\w+\s+in\s*\(([^)]*)\)", sql
    )
    assert found, f"no CHECK named {constraint}"
    return set(re.findall(r"'([^']*)'", found[-1]))


def _full_task_row() -> dict:
    """One build_task_rows row with every optional key present."""
    chunk = {
        "task_id": "thesis_task_1",
        "title": "Outline chapter 1",
        "duration_minutes": 25,
        "difficulty_weight": 0.5,
        "dependencies": [],
        "deadline_hint": "Friday",
        "completion_criteria": "An outline exists",
        "implementation_intention": {"obstacle_trigger": "if tired", "behavioral_response": "walk"},
        "topic_keywords": ["thesis"],
    }
    _, rows = build_task_rows(
        "alice",
        [chunk],
        schedule={"thesis_task_1": {"start_min": 0, "end_min": 25}},
        horizon_start="2026-11-14T08:00:00+00:00",
    )
    return rows[0]


def test_migrations__capstone_file__creates_required_tables():
    sql = _sql(CAPSTONE)

    for table in NEW_TABLES:
        assert re.search(rf"create table if not exists {_TABLE}{table}\s*\(", sql), table
    assert re.search(rf"create or replace function {_TABLE}accept_draft\s*\(", sql)


def test_migrations__baseline__creates_legacy_tables_before_any_cli_file_alters_them():
    baseline = _sql(BASELINE)
    for table in BASELINE_TABLES:
        assert re.search(rf"create table if not exists {_TABLE}{table}\s*\(", baseline), table

    files = _all_migrations()
    assert files[0] == BASELINE, "the baseline must sort first: later files ALTER its tables"


def test_migrations__every_user_table__has_rls_enabled():
    sql = _sql(CAPSTONE)

    for table in USER_TABLES:
        assert re.search(rf"alter table {_TABLE}{table}\s+enable row level security", sql), table
        assert re.search(rf"create policy owner_all\s+on {_TABLE}{table}\s", sql), table


def test_migrations__every_user_table__references_user_profiles():
    sql = _sql(CAPSTONE)

    for table in USER_TABLES:
        if table == "user_profiles":
            continue
        inline = re.search(
            rf"create table if not exists {_TABLE}{table}\s*\((?:(?!\);).)*?"
            rf"references {_TABLE}user_profiles\s*\(user_id\)",
            sql,
            re.S,
        )
        added = re.search(
            rf"alter table {_TABLE}{table}\s+add constraint {table}_user_id_fkey\s+"
            rf"foreign key \(user_id\) references {_TABLE}user_profiles\s*\(user_id\)",
            sql,
        )
        assert inline or added, table


@pytest.mark.parametrize("path", _all_migrations(), ids=lambda p: p.name)
def test_migrations__statements_are_idempotent(path: Path):
    sql = _sql(path)

    assert not re.search(r"create table (?!if not exists)", sql)
    assert not re.search(r"create (?:unique )?index (?!if not exists)", sql)
    assert not re.search(r"add column (?!if not exists)", sql)
    assert not re.search(r"create function", sql), "use create or replace function"
    assert not re.search(r"create trigger", sql), "use create or replace trigger"
    for table in re.findall(rf"create policy \w+\s+on {_TABLE}(\w+)", sql):
        assert re.search(rf"drop policy if exists \w+ on {_TABLE}{table}\b", sql), table


def test_migrations__accept_draft_columns__match_build_task_rows():
    sql = _sql(CAPSTONE)
    function = re.search(r"function (?:public\.)?accept_draft\b.*?\$\$;", sql, re.S)
    assert function, "accept_draft not found"
    listed = re.search(r"v_columns\s+constant\s+text\[\]\s*:=\s*array\[(.*?)\]", function.group(0), re.S)
    assert listed, "accept_draft has no explicit column list"
    columns = set(re.findall(r"'(\w+)'", listed.group(1)))

    assert set(_full_task_row()) <= columns
    assert "select *" not in function.group(0)


def test_migrations__status_checks__match_domain_enums():
    sql = _sql(CAPSTONE)

    # pacing_pushed is the legacy Skip status; the repositories read it as SKIPPED.
    assert _check_values(sql, "user_tasks_status_check") == {s.value for s in TaskStatus} | {
        "pacing_pushed"
    }
    assert _check_values(sql, "draft_schedules_status_check") == {s.value for s in DraftStatus}
    assert _check_values(sql, "task_events_event_type_check") == {
        t.value for t in TaskEventType
    }
    assert _check_values(sql, "proactive_suggestions_status_check") == {
        s.value for s in SuggestionStatus
    }
