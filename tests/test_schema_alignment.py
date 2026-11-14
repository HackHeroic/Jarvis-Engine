"""Writes and probes match the tables the migrations actually create.

PostgREST rejects an insert that names a column the table lacks, and a
health probe against a table no migration creates always fails, so each test
pins the code to the schema in ``supabase/migrations``.
"""

import pytest

from app.db.supabase_py import DatabaseClient
from tests.fakes import FakeSupabase


class _RecordingSupabase(FakeSupabase):
    """FakeSupabase that remembers which tables were queried."""

    def __init__(self, seed=None):
        super().__init__(seed)
        self.tables: list[str] = []

    def table(self, name: str):
        self.tables.append(name)
        return super().table(name)


@pytest.mark.asyncio
async def test_check_connection__health_probe__queries_user_tasks():
    supabase = _RecordingSupabase()
    client = DatabaseClient.__new__(DatabaseClient)
    client.supabase = supabase

    assert await client.check_connection() is True
    assert supabase.tables == ["user_tasks"]
