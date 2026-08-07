"""Stores must respect an explicitly-passed None client (degraded mode)."""
from unittest.mock import patch


def test_memory_store__explicit_none_client__no_env_fallback():
    from app.services.memory import store as store_mod
    with patch.object(store_mod, "_get_supabase") as mock_get:
        s = store_mod.MemoryStore(supabase_client=None)
        mock_get.assert_not_called()
        assert s.get_active_memories("u1") == []


def test_memory_store__default_arg__uses_env_fallback():
    from app.services.memory import store as store_mod
    with patch.object(store_mod, "_get_supabase", return_value=None) as mock_get:
        store_mod.MemoryStore()
        mock_get.assert_called_once()


def test_draft_store__explicit_none_client__no_env_fallback():
    from app.services import draft_store as ds_mod
    with patch.object(ds_mod, "_get_supabase") as mock_get:
        s = ds_mod.DraftStore(supabase_client=None, ttl_seconds=300)
        mock_get.assert_not_called()
        assert s.get_pending_draft("u1") is None


def test_build_memory_context__store_raises__returns_empty_string():
    from app.services.memory.retriever import build_memory_context

    class ExplodingStore:
        def get_active_memories(self, user_id):
            raise ConnectionError("dns dead")

    assert build_memory_context("u1", ExplodingStore()) == ""
