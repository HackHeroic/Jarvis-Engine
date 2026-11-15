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

from app.domain.entities import (
    ConsentReasonKind,
    DraftStatus,
    MemorySource,
    MemoryStatus,
    SuggestionStatus,
    TaskEventType,
    TaskStatus,
)
from app.infrastructure.persistence._mapping import MEMORY_COLUMNS
from app.services.planning.task_rows import build_task_rows

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"
BASELINE = MIGRATIONS / "20260101000000_baseline_legacy_tables.sql"
CAPSTONE = MIGRATIONS / "20261114000001_capstone_lld.sql"
SKIP_WRITES_SKIPPED = MIGRATIONS / "20261115000000_skip_writes_skipped.sql"
ACCEPT_DRAFT_RESLOTS = MIGRATIONS / "20261115000001_accept_draft_reslots_missed.sql"
MEMORY_VECTORS = MIGRATIONS / "20261115000002_memory_lifecycle_and_vectors.sql"
SESSION_SUMMARIES = MIGRATIONS / "20261115000003_session_summaries.sql"
SUMMARY_REACH = MIGRATIONS / "20261115000006_session_summary_reach.sql"
SUGGESTION_CONSENT_KIND = MIGRATIONS / "20261115000004_suggestion_consent_kind.sql"
TASK_NOT_BEFORE = MIGRATIONS / "20261115000007_user_tasks_not_before.sql"

NEW_TABLES = ("user_profiles", "goals", "task_events", "proactive_suggestions", "llm_usage_events")
# Tables a migration creates that need no row-level security: the dead tables the
# capstone drops. Any other table must get RLS, or be added here deliberately.
NO_RLS_ALLOWLIST = frozenset(
    {
        "pending_action_items",
        "extracted_problems",
        "task_completion_criteria",
        "user_calendar_anchors",
        "conversation_messages",
        "conversation_sessions",
    }
)
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


def _set_sql() -> str:
    """Every migration's text, in filename order, as one string."""
    return "\n".join(_sql(path) for path in _all_migrations())


def _created_tables() -> set[str]:
    return set(re.findall(rf"create table (?:if not exists )?{_TABLE}(\w+)", _set_sql()))


def _dropped_tables() -> set[str]:
    statements = re.findall(r"drop table if exists\s+([^;]*);", _set_sql())
    return {
        name for statement in statements for name in re.findall(rf"{_TABLE}(\w+)", statement)
    } - {"cascade"}


def _user_tables() -> set[str]:
    """Every table the migrations create, except the allowlisted ones."""
    return _created_tables() - NO_RLS_ALLOWLIST


def _accept_draft_body(path: Path = CAPSTONE) -> str:
    function = re.search(r"function (?:public\.)?accept_draft\b.*?\$\$;", _sql(path), re.S)
    assert function, "accept_draft not found"
    return function.group(0)


def _accept_draft_in_force() -> str:
    """The body of accept_draft as the last migration that defines it has it."""
    defining = [path for path in _all_migrations() if "function public.accept_draft" in _sql(path)]
    return _accept_draft_body(defining[-1])


def _accept_draft_columns(body: str) -> set[str]:
    listed = re.search(r"v_columns\s+constant\s+text\[\]\s*:=\s*array\[(.*?)\]", body, re.S)
    assert listed, "accept_draft has no explicit column list"
    return set(re.findall(r"'(\w+)'", listed.group(1)))


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
        "not_before": "2026-11-15T18:00:00+00:00",
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


def test_migrations__no_rls_allowlist__holds_only_dropped_tables():
    assert NO_RLS_ALLOWLIST <= _dropped_tables()


def test_migrations__every_user_table__has_rls_enabled():
    sql = _set_sql()
    tables = _user_tables()

    assert set(NEW_TABLES) <= tables
    for table in tables:
        assert re.search(rf"alter table {_TABLE}{table}\s+enable row level security", sql), table
        assert re.search(rf"create policy owner_all\s+on {_TABLE}{table}\s", sql), table


def test_migrations__every_user_table__references_user_profiles():
    sql = _set_sql()

    for table in _user_tables() - {"user_profiles"}:
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
    for name in re.findall(r"add constraint (\w+)", sql):
        dropped_first = re.search(rf"drop constraint if exists {name}\b", sql)
        checked_first = re.search(rf"conname = '{name}'", sql)
        assert dropped_first or checked_first, f"unguarded add constraint {name}"


def test_migrations__accept_draft_columns__match_build_task_rows():
    body = _accept_draft_in_force()
    columns = _accept_draft_columns(body)

    assert set(_full_task_row()) <= columns
    assert "select *" not in body


def test_migrations__accept_draft_columns__fake_supabase_mirrors_the_definition_in_force():
    from tests.fakes import ACCEPT_DRAFT_COLUMNS

    assert _accept_draft_columns(_accept_draft_in_force()) == ACCEPT_DRAFT_COLUMNS


def test_migrations__accept_draft_v3__adds_not_before_and_changes_nothing_else():
    """v3 writes user_tasks.not_before, a new timestamptz column; the rest is v2 word for
    word, so every check above on v2 and the capstone function still holds for it."""
    v2, v3 = _accept_draft_body(ACCEPT_DRAFT_RESLOTS), _accept_draft_body(TASK_NOT_BEFORE)
    sql = _sql(TASK_NOT_BEFORE)

    assert _accept_draft_in_force() == v3
    assert _accept_draft_columns(v3) == _accept_draft_columns(v2) | {"not_before"}
    assert " ".join(v3.replace(", 'not_before'", "").split()) == " ".join(v2.split())
    assert re.search(
        rf"alter table {_TABLE}user_tasks add column if not exists not_before timestamptz;", sql
    )
    signature = r"function public\.accept_draft\(uuid, text, jsonb\)"
    assert re.search(rf"comment on {signature} is", sql)
    assert re.search(rf"revoke execute on {signature} from public, anon;", sql)
    assert re.search(rf"grant execute on {signature} to service_role, authenticated;", sql)


def test_migrations__accept_draft__locks_the_user_before_the_draft():
    body = _accept_draft_body()
    user_lock = re.search(
        r"from public\.user_profiles\s+where user_id = p_user_id\s+for no key update", body
    )
    draft_lock = re.search(
        r"from public\.draft_schedules\s+where id = p_draft_id and user_id = p_user_id\s+for update",
        body,
    )

    assert user_lock and draft_lock
    assert user_lock.start() < draft_lock.start(), "user lock must come first (deadlock order)"


def test_migrations__accept_draft__supersedes_the_users_other_open_drafts():
    assert re.search(
        r"set status = 'superseded'\s+where user_id = p_user_id\s+and id <> p_draft_id\s+"
        r"and status in \('pending', 'modified'\)",
        _accept_draft_body(),
    )


def test_migrations__accept_draft__executable_by_service_role_and_authenticated_only():
    sql = _sql(CAPSTONE)
    signature = r"function (?:public\.)?accept_draft\(uuid, text, jsonb\)"

    assert re.search(rf"revoke execute on {signature} from public, anon;", sql)
    assert re.search(rf"grant execute on {signature} to service_role, authenticated;", sql)


_ACCEPT_DRAFT_DELETE = r"delete from public\.user_tasks\b[^;]*;"


def test_migrations__accept_draft_v2__deletes_reslotted_missed_rows():
    """v2 also deletes the user's missed rows whose task_id the plan re-slots.

    Everything else is the capstone function, word for word, so the checks above on
    its columns, lock order and supersede still hold for the definition now in force.
    """
    v1, v2 = _accept_draft_body(CAPSTONE), _accept_draft_body(ACCEPT_DRAFT_RESLOTS)
    [delete] = re.findall(_ACCEPT_DRAFT_DELETE, v2)

    assert re.fullmatch(
        r"delete from public\.user_tasks\s+where user_id = p_user_id\s+"
        r"and \(\s*status = 'pending'\s+"
        r"or \(\s*status = 'missed'\s+"
        r"and task_id in \(\s*select value ->> 'task_id'\s+"
        r"from jsonb_array_elements\(coalesce\(p_rows, '\[\]'::jsonb\)\)\s*\)\s*\)\s*\)\s*;",
        delete,
    )

    def without_delete(body: str) -> str:
        return " ".join(re.sub(_ACCEPT_DRAFT_DELETE, "", body).split())

    assert without_delete(v2) == without_delete(v1)
    sql = _sql(ACCEPT_DRAFT_RESLOTS)
    signature = r"function public\.accept_draft\(uuid, text, jsonb\)"
    comment = re.search(rf"comment on {signature} is(.*?')\s*;", sql, re.S)
    assert comment and "missed" in comment.group(1), "the comment must name re-slotted missed rows"
    assert re.search(rf"revoke execute on {signature} from public, anon;", sql)
    assert re.search(rf"grant execute on {signature} to service_role, authenticated;", sql)


def test_migrations__user_tasks__full_index_on_scheduled_start():
    # Full, not partial: list_scheduled_between reads every status.
    assert re.search(
        rf"create index if not exists \w+\s+on {_TABLE}user_tasks\s*\(user_id, scheduled_start\)\s*;",
        _set_sql(),
    )


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


def test_migrations__skip_migration__maps_pacing_pushed_and_drops_it_from_check():
    sql = _sql(SKIP_WRITES_SKIPPED)

    mapped = re.search(
        rf"update {_TABLE}user_tasks\s+set status = 'skipped'\s+where status = 'pacing_pushed'\s*;",
        sql,
    )
    added = re.search(r"add constraint user_tasks_status_check\b", sql)
    assert mapped and added
    assert mapped.start() < added.start(), "rows must be mapped before the narrower CHECK"
    assert _check_values(sql, "user_tasks_status_check") == {s.value for s in TaskStatus}
    # The set ends on this definition: no later file brings pacing_pushed back.
    assert _check_values(_set_sql(), "user_tasks_status_check") == {s.value for s in TaskStatus}


# --- Memory lifecycle and vectors (20261115000002) ----------------------------------

_MATCH_MEMORIES_SIGNATURE = (
    r"public\.match_memories\(text, vector\(384\), text\[\], text\[\], int, "
    r"double precision\)"
)


def _match_memories() -> re.Match:
    function = re.search(
        r"create or replace function public\.match_memories\s*\((.*?)\)\s*"
        r"returns table\s*\((.*?)\)\s*(.*?)\$\$(.*?)\$\$;",
        _sql(MEMORY_VECTORS),
        re.S,
    )
    assert function, "match_memories not found"
    return function


def _user_memories_columns() -> set[str]:
    """Every user_memories column the migration set creates and does not drop."""
    sql = _set_sql()
    created = re.search(
        rf"create table if not exists {_TABLE}user_memories\s*\((.*?)\);", sql, re.S
    )
    assert created, "no migration creates user_memories"
    # Top-level definitions only: commas inside a CHECK (...) list are not separators.
    body = re.sub(r"\((?:[^()]|\([^()]*\))*\)", "()", created.group(1))
    columns = {part.split()[0] for part in (p.strip() for p in body.split(",")) if part}
    for statement in re.findall(rf"alter table {_TABLE}user_memories\b(.*?);", sql, re.S):
        columns |= set(re.findall(r"add column (?:if not exists )?(\w+)", statement))
        columns -= set(re.findall(r"drop column (?:if exists )?(\w+)", statement))
    return columns


def test_migrations__memory_migration__enables_vector_and_defines_match_memories():
    sql = _sql(MEMORY_VECTORS)
    params, returned, attributes, _body = _match_memories().groups()

    created = re.search(r"create extension if not exists vector with schema extensions;", sql)
    # The type is unqualified, so the file works wherever pgvector lives (extensions,
    # or public on a database that installed it there before).
    search_path = re.search(r"set local search_path = public, extensions;", sql)
    first_use = re.search(r"vector\(384\)", sql)
    assert created and search_path and first_use
    assert created.start() < search_path.start() < first_use.start()
    assert "extensions.vector" not in sql
    # embedding becomes vector(384); only a flat, 384-long array of finite numbers
    # with no NULL survives the cast, everything else becomes NULL.
    assert re.search(
        r"alter column embedding type vector\(384\)\s+using case\s+"
        r"when array_ndims\(embedding\) is distinct from 1 then null\s+"
        r"when array_length\(embedding, 1\) <> 384 then null\s+"
        r"when array_position\(embedding, null\) is not null then null\s+"
        r"when embedding && array\['nan', 'infinity', '-infinity'\]::float8\[\] then null\s+"
        r"else embedding::vector\(384\)\s+end",
        sql,
    )
    assert [p.split()[0] for p in params.split(",")] == [
        "p_user_id", "p_query", "p_statuses", "p_types", "p_limit", "p_min_similarity",
    ]
    assert re.search(r"p_query vector\(384\)", params)
    assert re.search(r"language sql\s+stable", attributes)
    # Pinned on the function: <=> resolves wherever pgvector lives.
    assert re.search(r"set search_path = public, extensions", attributes)
    # Every memory column but the embedding, plus the similarity.
    returned_columns = [c.split()[0] for c in (part.strip() for part in returned.split(","))]
    assert set(returned_columns) == _user_memories_columns() - {"embedding"} | {"similarity"}
    assert re.search(r"similarity double precision", returned)
    # The repositories read exactly the entity's columns, all of which exist.
    assert "embedding" not in MEMORY_COLUMNS
    assert set(MEMORY_COLUMNS) <= _user_memories_columns()
    assert re.search(
        rf"revoke execute on function {_MATCH_MEMORIES_SIGNATURE} from public, anon;", sql
    )
    assert re.search(
        rf"grant execute on function {_MATCH_MEMORIES_SIGNATURE} to service_role, authenticated;",
        sql,
    )
    # The new columns' CHECKs match the domain enums.
    assert _check_values(sql, "user_memories_status_check") == {s.value for s in MemoryStatus}
    assert _check_values(sql, "user_memories_provenance_check") == {s.value for s in MemorySource}
    assert re.search(r"check \(observation_count >= 1\)", sql)
    assert re.search(r"check \(stability between 0\.01 and 20\)", sql)
    assert re.search(
        rf"create unique index if not exists \w+\s+on {_TABLE}user_memories\s*"
        r"\(user_id, pattern_key\)\s+where pattern_key is not null;",
        sql,
    )
    assert re.search(
        rf"create index if not exists \w+\s+on {_TABLE}user_memories\s*\(user_id, status\);", sql
    )


def test_migrations__match_memories__filters_user_and_status():
    _params, _returned, _attributes, body = _match_memories().groups()
    where = re.search(r"\bwhere\b(.*?)\border by\b", body, re.S)
    assert where, "match_memories has no where clause"
    conditions = [c.strip() for c in re.split(r"\band\b", where.group(1))]

    assert "m.user_id = p_user_id" in conditions
    assert "m.status = any (p_statuses)" in conditions
    assert "(p_types is null or m.memory_type = any (p_types))" in conditions
    assert "m.embedding is not null" in conditions
    assert "m.superseded_by is null" in conditions
    assert "1 - (m.embedding <=> p_query) >= p_min_similarity" in conditions
    assert re.search(r"1 - \(m\.embedding <=> p_query\) as similarity", body)
    assert re.search(r"order by m\.embedding <=> p_query\s+limit p_limit\s*;?\s*$", body)
    assert "select *" not in body and "m.*" not in body


def test_migrations__memory_migration__idempotent_statements():
    sql = _sql(MEMORY_VECTORS)

    for column in (
        "status", "provenance", "pattern_key", "source_session_id", "source_message_id",
        "last_confirmed_at", "strength_as_of",
    ):
        assert re.search(rf"add column if not exists {column}\b", sql), column
    # Soft references: a memory from a chat that was never saved is still stored.
    assert re.search(r"add column if not exists source_session_id uuid,", sql)
    assert re.search(r"add column if not exists source_message_id uuid,", sql)
    assert "references" not in sql
    # A re-run touches no row it already fixed (each update also bumps updated_at).
    assert re.search(
        rf"update {_TABLE}user_memories\s+set provenance = 'inferred'\s+"
        r"where source = 'behavior' and provenance <> 'inferred'\s*;",
        sql,
    )
    # The type change runs only while the column is still an array: a second run
    # would otherwise call array_length() on a vector.
    guard = re.search(
        r"if exists \(.*?column_name = 'embedding'.*?data_type = 'array'.*?\)\s+then", sql, re.S
    )
    alter = re.search(r"alter column embedding type", sql)
    assert guard and alter and guard.start() < alter.start()
    for name in (
        "user_memories_status_check",
        "user_memories_provenance_check",
        "user_memories_observation_count_check",
        "user_memories_stability_range",
    ):
        assert re.search(rf"drop constraint if exists {name};", sql), name


def test_migrations__memory_migration__legacy_nullable_columns_filled_then_not_null():
    sql = _sql(MEMORY_VECTORS)

    for column in (
        "confidence", "strength", "stability", "observation_count", "source", "created_at",
    ):
        filled = re.search(
            rf"update {_TABLE}user_memories set {column} = [^;]+ where {column} is null;", sql
        )
        required = re.search(rf"alter column {column} set not null", sql)
        assert filled and required, column
        assert filled.start() < required.start(), f"{column}: fill the NULLs first"


# --- Suggestion consent kind (20261115000004) ---------------------------------------


def test_migrations__consent_kind__column_and_check_match_the_enum():
    sql = _sql(SUGGESTION_CONSENT_KIND)

    assert re.search(
        rf"alter table {_TABLE}proactive_suggestions\s+add column if not exists consent_kind text",
        sql,
    )
    assert _check_values(sql, "proactive_suggestions_consent_kind_check") == {
        k.value for k in ConsentReasonKind
    }


# --- Session summaries (20261115000003) ---------------------------------------------


def test_migrations__session_summaries__columns_and_check():
    sql = _sql(SESSION_SUMMARIES)

    for column, kind in (
        ("summary", "text"),
        ("summary_through", "timestamptz"),
        ("summary_updated_at", "timestamptz"),
    ):
        assert re.search(
            rf"alter table {_TABLE}chat_sessions\s+add column if not exists {column} {kind}\b",
            sql,
        ), column
    check = re.search(
        r"add constraint chat_sessions_summary_length_check\s+"
        r"check \(summary is null or char_length\(summary\) <= 4000\)",
        sql,
    )
    assert check, "summary length CHECK"
    # A summary always says how far it reaches, and only then.
    assert re.search(
        r"add constraint chat_sessions_summary_through_check\s+"
        r"check \(\(summary is null\) = \(summary_through is null\)\)",
        sql,
    )
    # Writing the summary is not activity: the trigger keeps updated_at for a write that
    # changes only the summary columns, so idle detection and "since then" stay true.
    trigger = re.search(
        rf"create or replace trigger set_updated_at before update on {_TABLE}chat_sessions\s+"
        rf"for each row execute function {_TABLE}set_chat_session_updated_at\(\)",
        sql,
    )
    assert trigger
    body = re.search(r"function (?:public\.)?set_chat_session_updated_at\(\).*?\$\$;", sql, re.S)
    assert body and "old.updated_at" in body.group(0) and "now()" in body.group(0)
    for column in ("summary", "summary_through", "summary_updated_at", "updated_at"):
        assert f"'{column}'" in body.group(0), column
    # Owner-only, restated so the file stands alone.
    assert re.search(rf"alter table {_TABLE}chat_sessions\s+enable row level security", sql)
    assert re.search(rf"create policy owner_all\s+on {_TABLE}chat_sessions\s", sql)


def test_migrations__session_summaries__columns_match_the_entity():
    from app.domain.entities import ConversationSession
    from app.infrastructure.persistence._mapping import SESSION_COLUMNS

    added = set(re.findall(r"add column if not exists (\w+)", _sql(SESSION_SUMMARIES)))
    reach = set(re.findall(r"add column if not exists (\w+)", _sql(SUMMARY_REACH)))
    assert set(SESSION_COLUMNS) == set(ConversationSession.model_fields)
    assert {"summary", "summary_through", "summary_updated_at"} == added
    assert {"summary_covered_through"} == reach


# --- How far a summary reaches (20261115000006) ---------------------------------------


def test_migrations__summary_reach__column_check_and_writing_it_is_not_activity():
    sql = _sql(SUMMARY_REACH)

    assert re.search(
        rf"alter table {_TABLE}chat_sessions\s+"
        r"add column if not exists summary_covered_through timestamptz\b",
        sql,
    )
    # Never before the window's start, and only beside a summary.
    assert re.search(
        r"add constraint chat_sessions_summary_covered_through_check\s+check \("
        r"summary_covered_through is null or \(summary_through is not null and "
        r"summary_covered_through >= summary_through\)\)",
        sql,
    )
    # The trigger keeps updated_at for a write of the summary columns alone, this one too.
    body = re.search(r"function (?:public\.)?set_chat_session_updated_at\(\).*?\$\$;", sql, re.S)
    assert body and "old.updated_at" in body.group(0) and "now()" in body.group(0)
    assert body.group(0).count("summary_covered_through") >= 3, "compared and ignored"
    assert re.search(
        rf"create or replace trigger set_updated_at before update on {_TABLE}chat_sessions\s+"
        rf"for each row execute function {_TABLE}set_chat_session_updated_at\(\)",
        sql,
    )
