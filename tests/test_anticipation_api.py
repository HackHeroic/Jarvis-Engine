"""The suggestions API: list, accept, dismiss, and run a trigger on demand."""

import asyncio
from datetime import datetime, timezone
from typing import ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient

from app.domain.entities import ConsentReasonKind, Suggestion, SuggestionStatus
from tests.anticipation_fakes import ActionLog, CountingTrigger, FakeAction

U1 = "u1"
NOW = datetime(2026, 11, 14, 3, 30, tzinfo=timezone.utc)


class _MorningBrief(CountingTrigger):
    name: ClassVar[str] = "morning_brief"


@pytest.fixture
def container(monkeypatch):
    import app.core.config as cfg
    from app.container import AppContainer
    from app.main import app
    from app.orchestrator.hooks import ActionHooks

    built = AppContainer.build(supabase_client=None, cfg=cfg, hooks=ActionHooks())
    monkeypatch.setattr(app.state, "container", built, raising=False)
    return built


@pytest.fixture
def client(container) -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture
def log(container) -> ActionLog:
    log = ActionLog()
    container.actions.register(FakeAction.kind, FakeAction.builder(log))
    return log


def _suggestion(status: SuggestionStatus = SuggestionStatus.PENDING) -> Suggestion:
    return Suggestion(
        user_id=U1,
        trigger="morning_brief",
        kind="replan",
        title="A lighter morning",
        body="Two tasks found a new slot this afternoon.",
        action=FakeAction().to_payload(),
        status=status,
        consent_behavior="ask",
        consent_reason="This changes your schedule, so I'm asking first.",
        consent_kind=ConsentReasonKind.MODE_DECISION,
        created_at=NOW,
    )


def test_anticipation_api__run_unknown_trigger__422_envelope(client):
    response = client.post(
        "/api/v1/anticipation/run", params={"user_id": U1}, json={"trigger": "teleport"}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert set(error) == {"code", "message", "details", "request_id"}


@pytest.mark.asyncio
async def test_anticipation_api__list__pending_only_by_default(client, container):
    pending = await container.repos.suggestions.add(U1, _suggestion())
    await container.repos.suggestions.add(U1, _suggestion(SuggestionStatus.DISMISSED))
    await container.repos.suggestions.add("u2", _suggestion().model_copy(update={"user_id": "u2"}))

    listed = client.get("/api/v1/anticipation/suggestions", params={"user_id": U1})
    dismissed = client.get(
        "/api/v1/anticipation/suggestions", params={"user_id": U1, "status": "dismissed"}
    )

    assert listed.status_code == 200
    (only,) = listed.json()["suggestions"]
    assert only["id"] == pending.id
    assert only["consent_kind"] == "mode_decision"
    assert only["details"] == FakeAction().to_payload()
    assert [s["status"] for s in dismissed.json()["suggestions"]] == ["dismissed"]


def test_anticipation_api__list__new_user__empty(client):
    response = client.get("/api/v1/anticipation/suggestions", params={"user_id": "x"})

    assert (response.status_code, response.json()) == (200, {"suggestions": []})


@pytest.mark.asyncio
async def test_anticipation_api__accept_twice__409_envelope(client, container, log):
    stored = await container.repos.suggestions.add(U1, _suggestion())
    url = f"/api/v1/anticipation/suggestions/{stored.id}/accept"

    first = client.post(url, params={"user_id": U1})
    second = client.post(url, params={"user_id": U1})

    assert first.status_code == 200
    assert first.json()["suggestion"]["status"] == "accepted"
    assert first.json()["result"]["kind"] == "fake_action"
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "stale_version"
    assert log.runs == [U1]


@pytest.mark.asyncio
async def test_anticipation_api__dismiss__dismissed(client, container):
    stored = await container.repos.suggestions.add(U1, _suggestion())

    response = client.post(
        f"/api/v1/anticipation/suggestions/{stored.id}/dismiss", params={"user_id": U1}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "dismissed"


def test_anticipation_api__run_registered_trigger__created_with_consent(client, container, log):
    container.anticipation.register_trigger(_MorningBrief(action=FakeAction(log)))

    response = client.post(
        "/api/v1/anticipation/run", params={"user_id": U1}, json={"trigger": "morning_brief"}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "created"
    assert body["suggestion"]["trigger"] == "morning_brief"
    assert body["consent"] == {
        "behavior": "ask",
        "kind": "mode_decision",
        "message": "This changes your schedule, so I'm asking first.",
    }


@pytest.mark.asyncio
async def test_anticipation_api__concurrent_run__one_suggestion(container, log):
    from app.main import app

    container.anticipation.register_trigger(_MorningBrief(action=FakeAction(log)))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/anticipation/run",
                    params={"user_id": U1},
                    json={"trigger": "morning_brief"},
                )
                for _ in range(2)
            )
        )

    assert [r.status_code for r in responses] == [200, 200]
    assert sorted(r.json()["status"] for r in responses) == ["already_done", "created"]
    assert len(await container.repos.suggestions.list(U1, status=None)) == 1
