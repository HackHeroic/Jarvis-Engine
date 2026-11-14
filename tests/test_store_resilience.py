"""Stores must respect an explicitly-passed None client (degraded mode)."""
from unittest.mock import patch


def test_draft_store__explicit_none_client__no_env_fallback():
    from app.services import draft_store as ds_mod
    with patch.object(ds_mod, "_get_supabase") as mock_get:
        s = ds_mod.DraftStore(supabase_client=None, ttl_seconds=300)
        mock_get.assert_not_called()
        assert s.get_pending_draft("u1") is None
