# tests/test_user_model.py
from uuid import uuid4

import pytest
import pytest_asyncio

from app.core.user_model import UserModel
from app.domain.entities import BehavioralConstraint
from app.infrastructure.persistence.factory import RepositoryFactory


class _CountingConstraints:
    """The user's rules, counting each read (``ConstraintRepository.list_active``)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.reads = 0

    async def list_active(self, user_id: str):
        self.reads += 1
        return await self.inner.list_active(user_id)


@pytest_asyncio.fixture
async def facade():
    repos = RepositoryFactory.in_memory()
    await repos.constraints.add("u1", BehavioralConstraint(
        id=uuid4().hex, user_id="u1", raw_text="no work before 11am", constraint_type="preference",
    ))
    constraints = _CountingConstraints(repos.constraints)
    user_model = UserModel(
        "u1", profiles=repos.profiles, constraints=constraints, tasks=repos.tasks
    )
    return user_model, constraints


@pytest.mark.asyncio
async def test_get_behavioral_constraints_lazy_loads(facade):
    um, constraints = facade
    first = await um.get_behavioral_constraints()
    assert len(first) == 1
    assert first[0]["raw_text"] == "no work before 11am"
    second = await um.get_behavioral_constraints()
    assert second == first
    assert constraints.reads == 1


@pytest.mark.asyncio
async def test_invalidate_clears_cache(facade):
    um, constraints = facade
    await um.get_behavioral_constraints()
    um.invalidate("constraints")
    await um.get_behavioral_constraints()
    assert constraints.reads == 2


@pytest.mark.asyncio
async def test_get_estimated_energy_returns_float(facade):
    um, _constraints = facade
    energy = await um.get_estimated_energy()
    assert isinstance(energy, float)
    assert 0.0 <= energy <= 1.0
