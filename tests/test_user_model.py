# tests/test_user_model.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.user_model import UserModel


@pytest.fixture
def mock_db():
    db = MagicMock()
    table_mock = MagicMock()
    select_mock = MagicMock()
    eq_mock = MagicMock()
    execute_mock = MagicMock()
    execute_mock.data = [{"id": "c1", "raw_text": "no work before 11am", "constraint_type": "preference"}]

    db.supabase.table.return_value = table_mock
    table_mock.select.return_value = select_mock
    select_mock.eq.return_value = eq_mock
    eq_mock.execute.return_value = execute_mock
    return db


@pytest.mark.asyncio
async def test_get_behavioral_constraints_lazy_loads(mock_db):
    um = UserModel(user_id="u1", db=mock_db)
    constraints = await um.get_behavioral_constraints()
    assert len(constraints) == 1
    assert constraints[0]["raw_text"] == "no work before 11am"
    mock_db.supabase.table.reset_mock()
    constraints2 = await um.get_behavioral_constraints()
    assert constraints2 == constraints
    mock_db.supabase.table.assert_not_called()


@pytest.mark.asyncio
async def test_invalidate_clears_cache(mock_db):
    um = UserModel(user_id="u1", db=mock_db)
    await um.get_behavioral_constraints()
    um.invalidate("constraints")
    await um.get_behavioral_constraints()
    assert mock_db.supabase.table.call_count == 2


@pytest.mark.asyncio
async def test_get_estimated_energy_returns_float(mock_db):
    um = UserModel(user_id="u1", db=mock_db)
    energy = await um.get_estimated_energy()
    assert isinstance(energy, float)
    assert 0.0 <= energy <= 1.0
