"""Drafts have one persistence path: ``DraftService`` over ``DraftRepository``.

A planning turn proposes through ``PlanningServices.drafts``; the chat accept and the
REST accept both go through the same service and the ``accept_draft`` SQL function, so
a draft proposed in chat is found by id when the user says "looks good", and a second
accept writes nothing. Edits and the discard are moves of the draft State table: a
closed draft is refused before any write. An accept may carry the version the client
read, and a draft edited since is not accepted behind its back.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.services.planning.scheduling as scheduling
from app.domain import errors
from app.domain.entities import DraftStatus
from app.domain.events import NullEventBus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.draft_service import DraftService
from tests.behaviour.conftest import (  # noqa: F401 - jarvis and llm are fixtures
    brain_dump,
    decomposition,
    jarvis,
    llm,
)
from tests.fakes import FakeDBClient, FakeSupabase, build_test_container

IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
HORIZON = "2026-11-16T08:00:00+05:30"
U = "alice"
DSA_DUMP = "DSA exam on Friday. I still need to revise graphs, DP and trees, and do some mock problems."
DSA_TOPICS = ["Graph traversal", "Shortest paths", "DP on sequences", "Tree recursion", "Mock problems"]


@pytest.fixture(autouse=True)
def _monday_morning(clock):
    """Plans start at 08:00 on Monday on the user's clock; accepts happen before then."""
    clock.freeze(MONDAY_8AM, scheduling)


def _script_dsa_plan(llm) -> None:
    llm.script(
        "brain_dump_extraction",
        lambda req: brain_dump(planning_goal="Revise DSA for Friday's exam")
        if "DSA exam" in req.prompt else "{}",
    )
    llm.script("socratic_chunker", decomposition(DSA_TOPICS))


def _tasks(*ids: str) -> list[dict]:
    return [{"task_id": t, "title": f"Do {t}", "duration_minutes": 25} for t in ids]


@asynccontextmanager
async def _api(monkeypatch, container, supabase):
    from app.main import app

    monkeypatch.setattr(app.state, "container", container, raising=False)
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(supabase), raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --- one path from the chat proposal to the accept ------------------------------------


@pytest.mark.asyncio
async def test_planning_turn__draft_proposed__chat_accept_resolves_it_by_id(jarvis, llm):
    _script_dsa_plan(llm)
    plan = await jarvis.say(DSA_DUMP)
    assert plan.draft_id, plan.reply
    proposed = sorted(t["task_id"] for t in jarvis.draft(plan.draft_id)["tasks"])
    # A newer open draft of the same user: "looks good" on the planning thread means the
    # draft that thread proposed, never the user's newest open one.
    newer = await jarvis.state.container.draft_service.propose(
        U, tasks=_tasks("other_t1"), horizon_start=HORIZON, goal_id=None, schedule=None
    )

    reply = await jarvis.say("looks good", session=plan.conversation_id)

    assert "Locked in" in reply.reply
    assert jarvis.draft(plan.draft_id)["status"] == "accepted"
    assert jarvis.draft(newer.id)["status"] == "superseded"
    assert sorted(r["task_id"] for r in jarvis.rows("user_tasks", user_id=U)) == proposed


@pytest.mark.asyncio
async def test_rest_accept__after_a_chat_proposal__second_accept_409_and_tasks_written_once(
    jarvis, llm, monkeypatch
):
    _script_dsa_plan(llm)
    plan = await jarvis.say(DSA_DUMP)
    proposed = sorted(t["task_id"] for t in jarvis.draft(plan.draft_id)["tasks"])

    async with _api(monkeypatch, jarvis.state.container, jarvis.db) as api:
        first = await api.post(f"/api/v1/drafts/{plan.draft_id}/accept", json={"user_id": U})
        second = await api.post(f"/api/v1/drafts/{plan.draft_id}/accept", json={"user_id": U})

    assert first.status_code == 200, first.text
    assert first.json()["task_count"] == len(proposed)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"
    assert sorted(r["task_id"] for r in jarvis.rows("user_tasks", user_id=U)) == proposed


# --- edits and the discard are moves of the State table --------------------------------


def _service(repos) -> DraftService:
    return DraftService(repos.drafts, NullEventBus(), task_events=repos.task_events, tasks=repos.tasks)


@pytest.mark.asyncio
async def test_edit_tasks__closed_draft__invalid_state_transition_and_unchanged():
    repos = RepositoryFactory.in_memory()
    service = _service(repos)
    draft = await service.propose(
        U, tasks=_tasks("t1", "t2"), horizon_start=HORIZON, goal_id=None, schedule=None
    )
    await service.accept(U, draft.id)

    with pytest.raises(errors.InvalidStateTransition):
        await service.edit_tasks(U, draft.id, _tasks("t9"))

    stored = await repos.drafts.get(U, draft.id)
    assert stored.status is DraftStatus.ACCEPTED
    assert [t["task_id"] for t in stored.tasks] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_edit_task__unknown_task__not_found_and_draft_unchanged():
    repos = RepositoryFactory.in_memory()
    service = _service(repos)
    draft = await service.propose(
        U, tasks=_tasks("t1", "t2"), horizon_start=HORIZON, goal_id=None, schedule=None
    )

    with pytest.raises(errors.NotFoundError):
        await service.edit_task(U, draft.id, "no_such_task", {"title": "x"})

    stored = await repos.drafts.get(U, draft.id)
    assert stored.status is DraftStatus.PENDING  # never marked modified
    assert stored.tasks == draft.tasks


@pytest.mark.asyncio
async def test_edit_task__open_draft__edited_and_modified():
    repos = RepositoryFactory.in_memory()
    service = _service(repos)
    draft = await service.propose(
        U, tasks=_tasks("t1", "t2"), horizon_start=HORIZON, goal_id=None, schedule=None
    )

    edited = await service.edit_task(U, draft.id, "t1", {"title": "Read slowly", "start_min": None})

    assert edited.status is DraftStatus.MODIFIED
    assert [t["title"] for t in edited.tasks] == ["Read slowly", "Do t2"]
    assert (await repos.drafts.get(U, draft.id)).tasks == edited.tasks


@pytest.mark.asyncio
async def test_discard__open_draft__expired_and_row_kept():
    supabase = FakeSupabase()
    service = _service(RepositoryFactory.supabase(supabase))
    draft = await service.propose(
        U, tasks=_tasks("t1"), horizon_start=HORIZON, goal_id=None, schedule=None
    )

    discarded = await service.discard(U, draft.id)

    assert discarded.status is DraftStatus.EXPIRED
    (row,) = supabase.rows["draft_schedules"]
    assert (row["id"], row["status"]) == (draft.id, "expired")
    with pytest.raises(errors.InvalidStateTransition):  # closed now: no second discard
        await service.discard(U, draft.id)


@pytest.mark.asyncio
async def test_open_draft__named_one_else_newest_open():
    repos = RepositoryFactory.in_memory()
    service = _service(repos)
    older = await service.propose(U, tasks=_tasks("a"), horizon_start=HORIZON, goal_id=None,
                                  schedule=None)
    newer = await service.propose(U, tasks=_tasks("b"), horizon_start=HORIZON, goal_id=None,
                                  schedule=None)

    assert (await service.open_draft(U, older.id)).id == older.id
    assert (await service.open_draft(U)).id == newer.id
    assert (await service.open_draft(U, str(uuid4()))).id == newer.id  # unknown id: newest open
    assert await service.open_draft("bob") is None


def test_draft_routes__no_501_chat_route_and_every_route_typed():
    from fastapi.routing import APIRoute

    from app.main import app

    routes = [
        r for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/api/v1/drafts")
    ]

    assert routes
    assert not [r.path for r in routes if r.path.endswith("/chat")]
    untyped = sorted(f"{sorted(r.methods)} {r.path}" for r in routes if r.response_model is None)
    assert untyped == []


# --- the replanner is handed its collaborators -----------------------------------------


@pytest.mark.asyncio
async def test_replanner__built_with_planning_services__no_process_wide_lookup():
    from importlib.util import find_spec

    from app.modules.planning_replan import PlanningGraphReplanner
    from tests.fakes import planning_services

    assert find_spec("app.core.runtime") is None  # there is no registry to look anything up in
    repos = RepositoryFactory.in_memory()
    seed = await repos.drafts.create(
        U, tasks=_tasks("seed"), horizon_start=None, goal_id=None, schedule=None
    )
    await repos.drafts.accept_atomic(U, seed.id, [{
        "task_id": "dsa_t1", "title": "Revise graphs", "goal_id": "dsa", "status": "pending",
        "duration_minutes": 25, "difficulty_weight": 0.5, "dependencies": [],
    }])

    proposed: list[dict] = []

    class _Recording(DraftService):
        async def propose(self, user_id, **kwargs):
            proposed.append({"user_id": user_id, **kwargs})
            return await super().propose(user_id, **kwargs)

    planning = planning_services(repos, drafts=_Recording(repos.drafts, NullEventBus()))

    draft = await PlanningGraphReplanner(planning).replan(U, reslot_task_ids=[])

    assert draft is not None
    (call,) = proposed
    assert call["user_id"] == U
    assert [t["task_id"] for t in call["tasks"]] == ["dsa_t1"]
    assert set(draft.schedule) == {"dsa_t1"}


# --- an accept never lands on a plan edited since the client read it -------------------

PLAN_CHANGED = "This plan changed since you looked — here is the latest."


@pytest.mark.asyncio
async def test_accept__stale_expected_version__stale_version_and_nothing_written():
    repos = RepositoryFactory.in_memory()
    service = _service(repos)
    draft = await service.propose(
        U, tasks=_tasks("t1", "t2"), horizon_start=HORIZON, goal_id=None, schedule=None
    )
    read = draft.version
    edited = await service.edit_task(U, draft.id, "t1", {"title": "Changed"})
    assert edited.version != read

    with pytest.raises(errors.StaleVersionError) as caught:
        await service.accept(U, draft.id, expected_version=read)

    assert caught.value.message == PLAN_CHANGED
    assert caught.value.details["version"] == edited.version
    assert await repos.tasks.list_pending(U) == []
    assert (await repos.drafts.get(U, draft.id)).status is DraftStatus.MODIFIED
    assert await service.accept(U, draft.id, expected_version=edited.version) == 2


@pytest.mark.asyncio
async def test_rest_accept__edited_after_the_read__409_with_the_latest_and_nothing_written(
    monkeypatch,
):
    supabase = FakeSupabase()
    container = build_test_container(supabase)
    draft = await container.draft_service.propose(
        U, tasks=_tasks("t1", "t2"), horizon_start=HORIZON, goal_id=None, schedule=None
    )

    async with _api(monkeypatch, container, supabase) as api:
        read = (await api.get(f"/api/v1/drafts/{draft.id}", params={"user_id": U})).json()
        edit = await api.patch(
            f"/api/v1/drafts/{draft.id}/tasks/t1", params={"user_id": U},
            json={"title": "Read ch1 slowly"},
        )
        stale = await api.post(
            f"/api/v1/drafts/{draft.id}/accept",
            json={"user_id": U, "expected_version": read["version"]},
        )
        latest = (await api.get(f"/api/v1/drafts/{draft.id}", params={"user_id": U})).json()
        fresh = await api.post(
            f"/api/v1/drafts/{draft.id}/accept",
            json={"user_id": U, "expected_version": latest["version"]},
        )

    assert read["created_at"]  # stamped by the store, read back off the draft
    assert edit.status_code == 200
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert (error["code"], error["message"]) == ("stale_version", PLAN_CHANGED)
    assert error["details"]["draft"]["version"] == latest["version"] != read["version"]
    assert error["details"]["draft"]["tasks"][0]["title"] == "Read ch1 slowly"
    assert fresh.status_code == 200 and fresh.json()["task_count"] == 2
    titles = sorted(r["title"] for r in supabase.rows["user_tasks"])
    assert titles == ["Do t2", "Read ch1 slowly"]


@pytest.mark.asyncio
async def test_rest_accept__no_expected_version__accepted_as_today(monkeypatch):
    supabase = FakeSupabase()
    container = build_test_container(supabase)
    draft = await container.draft_service.propose(
        U, tasks=_tasks("t1"), horizon_start=HORIZON, goal_id=None, schedule=None
    )
    await container.draft_service.edit_task(U, draft.id, "t1", {"title": "Changed"})

    async with _api(monkeypatch, container, supabase) as api:
        resp = await api.post(f"/api/v1/drafts/{draft.id}/accept", json={"user_id": U})

    assert resp.status_code == 200 and resp.json()["task_count"] == 1


# --- one constant says how long a draft holds ------------------------------------------


@pytest.mark.asyncio
async def test_draft_ttl__one_constant__24h_and_no_dead_twin_in_config(clock):
    import app.core.config as config
    from app.infrastructure.persistence import _mapping
    from app.infrastructure.persistence._mapping import DRAFT_TTL

    clock.freeze(MONDAY_8AM, _mapping)
    assert not hasattr(config, "DRAFT_TTL_SECONDS")
    assert DRAFT_TTL == timedelta(hours=24)
    for repos in (RepositoryFactory.in_memory(), RepositoryFactory.supabase(FakeSupabase())):
        draft = await repos.drafts.create(
            U, tasks=_tasks("t1"), horizon_start=HORIZON, goal_id=None, schedule=None
        )
        assert draft.expires_at == MONDAY_8AM + DRAFT_TTL
