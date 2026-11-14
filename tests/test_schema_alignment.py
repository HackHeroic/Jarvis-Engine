"""Writes and probes match the tables the migrations actually create.

PostgREST rejects an insert that names a column the table lacks, and a
health probe against a table no migration creates always fails, so each test
pins the code to the schema in ``supabase/migrations``.
"""

import pytest
from fastapi.testclient import TestClient

from app.db.supabase_py import DatabaseClient
from tests.fakes import FakeDBClient, FakeSupabase

_UNSET = object()

# task_completion_signals as created by 20260329000002_phase1_missing_tables.sql
_SIGNAL_COLUMNS = {"id", "user_id", "task_id", "signal_type", "quality", "metadata", "created_at"}


class _RecordingSupabase(FakeSupabase):
    """FakeSupabase that remembers which tables were queried."""

    def __init__(self, seed=None):
        super().__init__(seed)
        self.tables: list[str] = []

    def table(self, name: str):
        self.tables.append(name)
        return super().table(name)


@pytest.fixture
def app_with_db():
    import app.core.config as cfg
    from app.container import AppContainer
    from app.main import app
    from app.orchestrator.hooks import ActionHooks

    originals = {
        name: getattr(app.state, name, _UNSET) for name in ("db_client", "container")
    }
    supabase = FakeSupabase({"user_tasks": [{
        "id": "row-1", "task_id": "t1", "user_id": "alice", "status": "pending",
        "title": "Revise notes", "duration_minutes": 25, "difficulty_weight": 0.5,
    }]})
    app.state.db_client = FakeDBClient(supabase)
    app.state.container = AppContainer.build(supabase_client=supabase, cfg=cfg, hooks=ActionHooks())
    yield app, supabase
    for name, original in originals.items():
        if original is _UNSET:
            delattr(app.state, name)
        else:
            setattr(app.state, name, original)


def test_complete_task__completion_signal__written_with_existing_columns_only(app_with_db):
    app, supabase = app_with_db

    resp = TestClient(app).post(
        "/api/v1/tasks/t1/complete",
        json={"user_id": "alice", "quality": 4, "actual_duration_minutes": 30},
    )

    assert resp.status_code == 200
    signals = supabase.rows["task_completion_signals"]
    assert len(signals) == 1
    signal = signals[0]
    assert set(signal) <= _SIGNAL_COLUMNS
    assert signal["signal_type"] == "completed"
    assert signal["user_id"] == "alice"
    assert signal["task_id"] == "t1"
    assert signal["metadata"] == {"actual_duration_minutes": 30}


@pytest.mark.asyncio
async def test_check_connection__health_probe__queries_user_tasks():
    supabase = _RecordingSupabase()
    client = DatabaseClient.__new__(DatabaseClient)
    client.supabase = supabase

    assert await client.check_connection() is True
    assert supabase.tables == ["user_tasks"]
