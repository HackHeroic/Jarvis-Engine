"""The memories REST API (the Memory panel) over MemoryService.

The composition root runs on the Supabase repositories over ``FakeSupabase``, with
the bag-of-words embedder. Another user's memory reads as absent (404), and every
error is the one envelope.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.domain.entities import MemorySource
from app.services.memory.memory_service import MemoryCandidate
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

MARATHON = "Training for the Mumbai marathon in March"
SKIPS_AT_NINE = "Skips work around 9 AM"
SECRET = "postgres://admin:hunter2@db.internal"

_MEMORY_OUT_FIELDS = {
    "memory_id",
    "memory_type",
    "content",
    "confidence",
    "provenance",
    "freshness",
    "noted_at",
    "last_confirmed_at",
    "observation_count",
    "pattern_key",
}


def _container(supabase, embedder=None):
    import app.core.config as cfg
    from app.container import AppContainer
    from app.orchestrator.hooks import ActionHooks

    return AppContainer.build(
        supabase_client=supabase,
        cfg=cfg,
        hooks=ActionHooks(),
        embedder=embedder or BagOfWordsEmbedder(),
        install_chain=False,
    )


@asynccontextmanager
async def _api(monkeypatch, container, supabase):
    """The app over ``container``, its database client over ``supabase`` (None: no database)."""
    from app.main import app

    db_client = FakeDBClient(supabase) if supabase is not None else SimpleNamespace(supabase=None)
    monkeypatch.setattr(app.state, "container", container, raising=False)
    monkeypatch.setattr(app.state, "db_client", db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def supabase() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def container(supabase):
    return _container(supabase)


async def _remember(container, content: str, memory_type: str = "goal", user: str = "alice", **kw):
    outcome = await container.memory_service.remember(
        user, MemoryCandidate(memory_type=memory_type, content=content, confidence=0.8), **kw
    )
    return outcome.memory


def _row(supabase, memory_id: str) -> dict:
    (row,) = [r for r in supabase.rows["user_memories"] if r["id"] == memory_id]
    return row


@pytest.mark.asyncio
async def test_memories_api__forget_then_list__gone(monkeypatch, supabase, container):
    marathon = await _remember(container, MARATHON)
    pattern = await _remember(
        container, SKIPS_AT_NINE, "behavioral_pattern",
        provenance=MemorySource.INFERRED, origin="behavior",
    )

    async with _api(monkeypatch, container, supabase) as api:
        forgot = await api.delete(f"/api/v1/memories/{marathon.id}", params={"user_id": "alice"})
        panel = await api.get("/api/v1/memories/", params={"user_id": "alice"})

    assert (forgot.status_code, forgot.json()) == (200, {"status": "forgotten", "forgotten_rule_ids": []})
    assert panel.status_code == 200
    body = panel.json()
    assert body["count"] == 1
    (shown,) = body["memories"]
    assert set(shown) == _MEMORY_OUT_FIELDS
    assert (shown["memory_id"], shown["content"], shown["provenance"]) == (
        pattern.id, SKIPS_AT_NINE, "inferred"
    )
    assert shown["freshness"] == pytest.approx(1.0, abs=1e-3)
    row = _row(supabase, marathon.id)
    assert (row["status"], row["content"]) == ("forgotten", "")
    assert row["embedding"] is not None  # kept, so the fact is recognised if said again


@pytest.mark.asyncio
async def test_memories_api__other_users_memory__404(monkeypatch, supabase, container):
    memory = await _remember(container, MARATHON)
    before = dict(_row(supabase, memory.id))
    path = f"/api/v1/memories/{memory.id}"

    async with _api(monkeypatch, container, supabase) as api:
        responses = [
            await api.patch(path, params={"user_id": "bob"}, json={"content": "Hijacked"}),
            await api.delete(path, params={"user_id": "bob"}),
            await api.post(f"{path}/confirm", params={"user_id": "bob"}),
            await api.post(f"{path}/dismiss", params={"user_id": "bob"}),
        ]
        bobs_panel = await api.get("/api/v1/memories/", params={"user_id": "bob"})

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert {r.json()["error"]["code"] for r in responses} == {"not_found"}
    assert bobs_panel.json() == {"memories": [], "count": 0}
    assert _row(supabase, memory.id) == before


class _BrokenTable:
    """A table whose every query fails with a planted secret."""

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise RuntimeError(f"connection to {SECRET} refused")

        return fail


class _MemoriesDown(FakeSupabase):
    def table(self, name: str):
        return _BrokenTable() if name == "user_memories" else super().table(name)


@pytest.mark.asyncio
async def test_memories_api__store_down__503_envelope(monkeypatch):
    supabase = _MemoriesDown()
    container = _container(supabase)

    async with _api(monkeypatch, container, supabase) as api:
        resp = await api.get("/api/v1/memories/", params={"user_id": "alice"})

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "persistence_unavailable"
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_memories_api__dismiss__status_dismissed(monkeypatch, supabase, container):
    memory = await _remember(
        container, SKIPS_AT_NINE, "behavioral_pattern",
        provenance=MemorySource.INFERRED, origin="behavior",
    )

    async with _api(monkeypatch, container, supabase) as api:
        dismissed = await api.post(
            f"/api/v1/memories/{memory.id}/dismiss", params={"user_id": "alice"}
        )
        panel = await api.get("/api/v1/memories/", params={"user_id": "alice"})

    assert dismissed.status_code == 200
    assert set(dismissed.json()) == _MEMORY_OUT_FIELDS
    assert dismissed.json()["memory_id"] == memory.id
    row = _row(supabase, memory.id)
    assert (row["status"], row["content"]) == ("dismissed", SKIPS_AT_NINE)  # hidden, kept
    assert panel.json() == {"memories": [], "count": 0}


@pytest.mark.asyncio
async def test_memories_api__no_database__503_on_every_route_never_empty_or_404(monkeypatch):
    container = _container(None)  # what the lifespan builds with Supabase unreachable
    memory = await _remember(container, MARATHON)
    path = f"/api/v1/memories/{memory.id}"
    params = {"user_id": "alice"}

    async with _api(monkeypatch, container, None) as api:
        responses = [
            await api.get("/api/v1/memories/", params=params),
            await api.patch(path, params=params, json={"content": "Edited"}),
            await api.delete(path, params=params),
            await api.post(f"{path}/confirm", params=params),
            await api.post(f"{path}/dismiss", params=params),
        ]

    assert [r.status_code for r in responses] == [503] * 5
    assert {r.json()["error"]["code"] for r in responses} == {"persistence_unavailable"}


@pytest.mark.asyncio
async def test_memories_api__embedder_unavailable_on_edit__503_envelope(monkeypatch, supabase):
    from app.infrastructure.embedding import MiniLMEmbedder

    memory = await _remember(_container(supabase), MARATHON)
    monkeypatch.setattr(
        "app.infrastructure.embedding.embed_texts", lambda texts: [None] * len(texts)
    )
    container = _container(supabase, embedder=MiniLMEmbedder())

    async with _api(monkeypatch, container, supabase) as api:
        resp = await api.patch(
            f"/api/v1/memories/{memory.id}",
            params={"user_id": "alice"},
            json={"content": "Training for the Pune half marathon"},
        )

    assert resp.status_code == 503
    assert set(resp.json()["error"]) == {"code", "message", "details", "request_id"}
    assert _row(supabase, memory.id)["content"] == MARATHON


@pytest.mark.asyncio
async def test_memories_api__confirm_after_dismiss__409_envelope(monkeypatch, supabase, container):
    memory = await _remember(container, SKIPS_AT_NINE, "behavioral_pattern")

    async with _api(monkeypatch, container, supabase) as api:
        await api.post(f"/api/v1/memories/{memory.id}/dismiss", params={"user_id": "alice"})
        resp = await api.post(f"/api/v1/memories/{memory.id}/confirm", params={"user_id": "alice"})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "invalid_state_transition"
    assert _row(supabase, memory.id)["status"] == "dismissed"


# --- a stated rule and its memory: letting go of one lets go of both ------------------


async def _taught_rule(container, user: str = "alice"):
    """A rule row and the memory of stating it, linked as the chat turn links them."""
    from uuid import uuid4

    from app.domain.entities import BehavioralConstraint

    rule = await container.repos.constraints.add(user, BehavioralConstraint(
        id=str(uuid4()), user_id=user, raw_text="Never schedule before 11 AM",
        constraint_type="habit", recurrence="daily",
    ))
    memory = await _remember(
        container, "Never schedule before 11 AM", memory_type="constraint", user=user,
        provenance=MemorySource.SAID,
    )
    await container.stated_rules.link(user, rule.id, memory.id)
    return rule, memory


@pytest.mark.asyncio
async def test_memories_api__forget_a_stated_rules_memory__the_rule_is_gone_too(
    monkeypatch, supabase, container
):
    rule, memory = await _taught_rule(container)

    async with _api(monkeypatch, container, supabase) as client:
        forgotten = await client.delete(f"/api/v1/memories/{memory.id}?user_id=alice")
        rules = await client.get("/api/v1/habits/constraints?user_id=alice")

    assert forgotten.status_code == 200
    assert forgotten.json() == {"status": "forgotten", "forgotten_rule_ids": [rule.id]}
    assert rules.json()["constraints"] == []
    assert await container.repos.constraints.list_active("alice") == []


@pytest.mark.asyncio
async def test_habits_api__delete_a_stated_rule__its_memory_is_forgotten(
    monkeypatch, supabase, container
):
    rule, memory = await _taught_rule(container)
    await _taught_rule(container, user="bob")

    async with _api(monkeypatch, container, supabase) as client:
        stranger = await client.delete(f"/api/v1/habits/constraints/{rule.id}?user_id=bob")
        deleted = await client.delete(f"/api/v1/habits/constraints/{rule.id}?user_id=alice")
        again = await client.delete(f"/api/v1/habits/constraints/{rule.id}?user_id=alice")
        listed = await client.get("/api/v1/memories/?user_id=alice")

    assert stranger.status_code == 404 and again.status_code == 404
    assert stranger.json()["error"]["code"] == "not_found"
    assert deleted.status_code == 200 and deleted.json() == {"status": "deleted", "id": rule.id}
    assert listed.json()["memories"] == []
    assert _row(supabase, memory.id)["status"] == "forgotten"
    assert [m.content for m in await container.memory_service.list_active("bob")] == [
        "Never schedule before 11 AM"
    ]
