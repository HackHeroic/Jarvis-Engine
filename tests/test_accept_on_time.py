"""Accepting a plan some of whose times have already gone by.

A draft accepted three hours after it was made can hold slots that have started. Written
as drafted, the next slot sweep would mark them set aside, as if the user had set them
aside: fake evidence for PEARL and the focus profile. So the chat and REST accepts share
one path (``accept_on_time``): a draft with no passed slot is accepted as drafted; one
with some is re-solved from now first (its own tasks, the draft-edit re-solve, on the
user's clock) and the refreshed plan is put in place, with a plain word to the user; if
the rest no longer fits, nothing is accepted and the draft stays open. A draft whose
every slot has gone by is refused, as before.

The composition root over ``FakeSupabase``, the real CP-SAT solver, Monday 11:00 in India.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.api.v1.endpoints.schedule as schedule_endpoint
import app.core.config as cfg
import app.domain.clock as utc_clock
from app.container import AppContainer
from app.core import runtime
from app.domain.entities import DraftStatus, TaskEventType
from app.orchestrator.hooks import ActionHooks
from app.services.task_service import SlotPassedSweep
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

IST = ZoneInfo("Asia/Kolkata")
ELEVEN_AM = datetime(2026, 11, 16, 11, 0, tzinfo=IST)
HORIZON = "2026-11-16T08:00:00+05:30"  # minute 0: 08:00 on the user's clock
U = "alice"


def _task(task_id: str, title: str, **extra) -> dict:
    return {
        "task_id": task_id, "title": title, "duration_minutes": 25, "difficulty_weight": 0.5,
        "dependencies": [], "completion_criteria": "Done when explained back",
        "deadline_hint": "2026-11-20", "goal_id": "dsa", **extra,
    }


TASKS = [_task("dsa_r1_t1", "Arrays"), _task("dsa_r1_t2", "Linked lists"), _task("dsa_r1_t3", "Trees")]
# 09:00 and 10:00 have gone by at 11:00; 14:00 has not.
PARTLY_PASSED = {
    "dsa_r1_t1": {"start_min": 60, "end_min": 85},
    "dsa_r1_t2": {"start_min": 120, "end_min": 145},
    "dsa_r1_t3": {"start_min": 360, "end_min": 385},
}
ALL_AHEAD = {
    "dsa_r1_t1": {"start_min": 240, "end_min": 265},
    "dsa_r1_t2": {"start_min": 300, "end_min": 325},
    "dsa_r1_t3": {"start_min": 360, "end_min": 385},
}


@pytest.fixture(autouse=True)
def _monday_eleven(clock):
    clock.freeze(ELEVEN_AM, schedule_endpoint, utc_clock)


@pytest.fixture
def fake() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def container(fake) -> AppContainer:
    built = AppContainer.build(
        supabase_client=fake, cfg=cfg, hooks=ActionHooks(), embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )
    runtime.set_container(built)  # conftest resets the registry afterwards
    return built


async def _draft(container, schedule: dict, tasks: list[dict] = TASKS):
    return await container.repos.drafts.create(
        U, tasks=tasks, horizon_start=HORIZON, goal_id="dsa", schedule=schedule
    )


async def _starts(container) -> dict[str, datetime]:
    pending = await container.repos.tasks.list_pending(U)
    return {t.task_id: t.scheduled_start.astimezone(IST) for t in pending}


async def _swept_after(container) -> list:
    """What a slot sweep run right after the accept sets aside."""
    await SlotPassedSweep(container.task_service).run([U], ELEVEN_AM + timedelta(minutes=5))
    events = await container.repos.task_events.list_for_user(
        U, since=ELEVEN_AM - timedelta(days=1), types={TaskEventType.MISSED}
    )
    return events


@asynccontextmanager
async def _api(monkeypatch, container, fake):
    from app.main import app
    from app.services.draft_store import DraftStore

    monkeypatch.setattr(app.state, "draft_store", DraftStore(supabase_client=fake), raising=False)
    monkeypatch.setattr(app.state, "container", container, raising=False)
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(fake), raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --- REST -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_accept__three_hours_late__moved_from_now_and_nothing_swept(
    monkeypatch, container, fake
):
    draft = await _draft(container, PARTLY_PASSED)

    async with _api(monkeypatch, container, fake) as api:
        resp = await api.post(f"/api/v1/drafts/{draft.id}/accept", json={"user_id": U})

    assert resp.status_code == 200
    body = resp.json()
    assert (body["status"], body["task_count"], body["moved"]) == ("accepted", 3, True)
    assert body["message"] == "A few of those times had already gone by, so I moved them — it's locked in now."
    starts = await _starts(container)
    assert set(starts) == {"dsa_r1_t1", "dsa_r1_t2", "dsa_r1_t3"}
    assert all(start >= ELEVEN_AM for start in starts.values()), starts
    assert (await container.repos.drafts.get(U, draft.id)).status is DraftStatus.ACCEPTED
    assert await _swept_after(container) == []


@pytest.mark.asyncio
async def test_rest_accept__nothing_passed__accepted_as_drafted_and_second_accept_409(
    monkeypatch, container, fake
):
    draft = await _draft(container, ALL_AHEAD)

    async with _api(monkeypatch, container, fake) as api:
        first = await api.post(f"/api/v1/drafts/{draft.id}/accept", json={"user_id": U})
        again = await api.post(f"/api/v1/drafts/{draft.id}/accept", json={"user_id": U})

    assert first.status_code == 200
    assert (first.json()["moved"], first.json()["message"]) == (False, None)
    starts = await _starts(container)
    assert {t: s.strftime("%H:%M") for t, s in starts.items()} == {
        "dsa_r1_t1": "12:00", "dsa_r1_t2": "13:00", "dsa_r1_t3": "14:00",
    }
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_rest_accept__the_rest_no_longer_fits__422_and_the_draft_stays_open(
    monkeypatch, container, fake
):
    # Two tasks that each wait for the other: no re-solve can place them.
    cyclic = [
        _task("dsa_r1_t1", "Arrays", dependencies=["dsa_r1_t2"]),
        _task("dsa_r1_t2", "Linked lists", dependencies=["dsa_r1_t1"]),
    ]
    draft = await _draft(container, {
        "dsa_r1_t1": PARTLY_PASSED["dsa_r1_t1"], "dsa_r1_t2": PARTLY_PASSED["dsa_r1_t3"],
    }, tasks=cyclic)

    async with _api(monkeypatch, container, fake) as api:
        resp = await api.post(f"/api/v1/drafts/{draft.id}/accept", json={"user_id": U})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "schedule_infeasible"
    assert "haven't locked it in" in error["message"]
    stored = await container.repos.drafts.get(U, draft.id)
    assert stored.status in (DraftStatus.PENDING, DraftStatus.MODIFIED)
    assert await container.repos.tasks.list_pending(U) == []


# --- chat ---------------------------------------------------------------------------


async def _chat_accept(container, fake, draft_id: str) -> dict:
    """The orchestrator's draft-action node, as a "looks good" turn reaches it."""
    from app.core.user_model import UserModel
    from app.orchestrator.graph import handle_draft_action
    from app.schemas.context import IntentType
    from app.services.draft_store import DraftStore

    db = FakeDBClient(fake)
    return await handle_draft_action({
        "user_id": U,
        "user_model": UserModel(user_id=U, db=db),
        "intent": IntentType.ACCEPT_DRAFT,
        "draft_store": DraftStore(supabase_client=fake),
        "draft_id": draft_id,
        "user_message": "looks good",
    })


@pytest.mark.asyncio
async def test_chat_accept__three_hours_late__moved_said_plainly_and_nothing_swept(
    container, fake
):
    draft = await _draft(container, PARTLY_PASSED)

    out = await _chat_accept(container, fake, draft.id)

    assert out["response_message"].startswith(
        "A few of those times had already gone by, so I moved them — it's locked in now."
    )
    starts = await _starts(container)
    assert len(starts) == 3 and all(start >= ELEVEN_AM for start in starts.values()), starts
    assert await _swept_after(container) == []


@pytest.mark.asyncio
async def test_chat_accept__nothing_passed__locked_in_as_drafted(container, fake):
    draft = await _draft(container, ALL_AHEAD)

    out = await _chat_accept(container, fake, draft.id)

    assert out["response_message"] == "Locked in, sir — 3 tasks are on your schedule."
    assert sorted(s.strftime("%H:%M") for s in (await _starts(container)).values()) == [
        "12:00", "13:00", "14:00",
    ]


@pytest.mark.asyncio
async def test_chat_accept__the_rest_no_longer_fits__not_locked_in_and_still_open(
    container, fake
):
    cyclic = [
        _task("dsa_r1_t1", "Arrays", dependencies=["dsa_r1_t2"]),
        _task("dsa_r1_t2", "Linked lists", dependencies=["dsa_r1_t1"]),
    ]
    draft = await _draft(container, {
        "dsa_r1_t1": PARTLY_PASSED["dsa_r1_t1"], "dsa_r1_t2": PARTLY_PASSED["dsa_r1_t3"],
    }, tasks=cyclic)

    out = await _chat_accept(container, fake, draft.id)

    assert "haven't locked it in" in out["response_message"]
    assert (await container.repos.drafts.get(U, draft.id)).status is DraftStatus.PENDING
    assert await container.repos.tasks.list_pending(U) == []
