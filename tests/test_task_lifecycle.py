"""
Integration tests for task state transitions.
"""

import pytest
from unittest.mock import MagicMock

from tests.fakes import FakeDBClient, FakeSupabase


def _task_row(
    task_id: str, *, user_id: str = "u1", title: str = "Study CNNs", minutes: int = 25
) -> dict:
    """A pending user_tasks row with the columns the table requires."""
    return {
        "user_id": user_id, "task_id": task_id, "title": title, "status": "pending",
        "duration_minutes": minutes, "difficulty_weight": 0.5,
    }


def _client_over(fake: FakeSupabase, monkeypatch):
    """A TestClient whose app stores everything in ``fake``, as the lifespan wires it."""
    from fastapi.testclient import TestClient

    import app.core.config as cfg
    from app.container import AppContainer
    from app.main import app
    from app.orchestrator.hooks import ActionHooks

    monkeypatch.setattr(app.state, "db_client", FakeDBClient(fake), raising=False)
    monkeypatch.setattr(
        app.state,
        "container",
        AppContainer.build(supabase_client=fake, cfg=cfg, hooks=ActionHooks()),
        raising=False,
    )
    return TestClient(app)


@pytest.fixture
def mock_supabase():
    client = MagicMock()
    def make_query(data=None):
        q = MagicMock()
        q.select.return_value = q
        q.insert.return_value = q
        q.update.return_value = q
        q.delete.return_value = q
        q.eq.return_value = q
        q.in_.return_value = q
        q.single.return_value = q
        q.order.return_value = q
        q.limit.return_value = q
        result = MagicMock()
        result.data = data or []
        q.execute.return_value = result
        return q
    client.table.return_value = make_query()
    return client


class TestListTasks:
    def test_list_tasks__returns_tasks(self, mock_supabase):
        tasks_data = [
            {"task_id": "t1", "title": "Study CNNs", "status": "pending", "user_id": "user-a"},
            {"task_id": "t2", "title": "Essay draft", "status": "completed", "user_id": "user-a"},
        ]
        mock_supabase.table.return_value.execute.return_value.data = tasks_data

        query = mock_supabase.table("user_tasks").select("*").eq("user_id", "user-a")
        result = query.execute()

        assert len(result.data) == 2
        assert result.data[0]["task_id"] == "t1"

    def test_list_tasks__filters_by_user(self, mock_supabase):
        mock_supabase.table.return_value.execute.return_value.data = []

        query = mock_supabase.table("user_tasks").select("*").eq("user_id", "user-b")
        result = query.execute()

        assert len(result.data) == 0


class TestTaskComplete:
    def test_task_complete__updates_status(self, mock_supabase):
        mock_supabase.table.return_value.execute.return_value.data = {
            "task_id": "t1", "status": "completed",
        }

        query = (
            mock_supabase.table("user_tasks")
            .update({"status": "completed"})
            .eq("task_id", "t1")
            .eq("user_id", "user-a")
        )
        result = query.execute()
        assert result.data["status"] == "completed"

    def test_task_skip__marks_skipped(self, monkeypatch):
        """Skip writes ``skipped`` (never ``pacing_pushed``) and one outcome event."""
        fake = FakeSupabase({"user_tasks": [_task_row("t1")]})
        client = _client_over(fake, monkeypatch)

        resp = client.post("/api/v1/tasks/t1/skip", json={"user_id": "u1"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "skipped"
        assert "buffer" in resp.json()["message"]
        assert fake.rows["user_tasks"][0]["status"] == "skipped"
        assert "reschedule_count" not in fake.rows["user_tasks"][0]
        (event,) = fake.rows["task_events"]
        assert (event["user_id"], event["task_id"], event["event_type"]) == ("u1", "t1", "skipped")


# --- actual_duration_minutes reaches storage --------------------------------
# F2 (live run 2026-08-08): the field is declared on TaskCompleteRequest but once
# reached a column no migration created, so every request that supplied it 500'd.
# Completion now goes through TaskService, and the duration is recorded on the
# completed task event, the outcome history the learning layer reads.


class TestCompletionDurationWrite:
    def test_complete_endpoint__actual_duration__recorded_on_the_completed_event(
        self, monkeypatch
    ):
        fake = FakeSupabase({"user_tasks": [_task_row("t1")]})
        client = _client_over(fake, monkeypatch)

        resp = client.post(
            "/api/v1/tasks/t1/complete",
            json={"user_id": "u1", "quality": 4, "actual_duration_minutes": 18},
        )

        assert resp.status_code == 200, resp.text
        assert fake.rows["user_tasks"][0]["status"] == "completed"
        (event,) = fake.rows["task_events"]
        assert event["event_type"] == "completed"
        assert event["metadata"]["actual_duration_minutes"] == 18
        assert event["metadata"]["quality"] == 4

    def test_task_complete_request__accepts_actual_duration_minutes(self):
        """The schema advertises the field; the completed event records it."""
        from app.api.v1.endpoints.tasks import TaskCompleteRequest

        body = TaskCompleteRequest(user_id="demo", quality=4, actual_duration_minutes=18)

        assert body.actual_duration_minutes == 18
        assert TaskCompleteRequest(user_id="demo").actual_duration_minutes is None


# ---------------------------------------------------------------------------
# GET /api/v1/habits/constraints — behavioral constraints listing
# ---------------------------------------------------------------------------


def test_list_constraints__returns_user_rows_only():
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.fakes import FakeSupabase

    fake = FakeSupabase()
    fake.rows.setdefault("behavioral_constraints", []).extend([
        {"id": "c1", "user_id": "u1", "raw_text": "no study before 11am",
         "constraint_type": "habit", "created_at": "2026-08-08T10:00:00+00:00"},
        {"id": "c2", "user_id": "OTHER", "raw_text": "leak", "constraint_type": "habit",
         "created_at": "2026-08-08T10:00:00+00:00"},
    ])

    class _DB:
        supabase = fake

    original = getattr(app.state, "db_client", None)
    app.state.db_client = _DB()
    try:
        client = TestClient(app)
        resp = client.get("/api/v1/habits/constraints?user_id=u1")
        assert resp.status_code == 200
        rows = resp.json()["constraints"]
        assert [r["raw_text"] for r in rows] == ["no study before 11am"]
    finally:
        app.state.db_client = original


def test_list_constraints__degraded_db__503_not_an_empty_list():
    """An outage is not "you have no rules": the list answers 503, never []."""
    from fastapi.testclient import TestClient
    from app.main import app

    original = getattr(app.state, "db_client", None)
    app.state.db_client = None
    try:
        client = TestClient(app)
        resp = client.get("/api/v1/habits/constraints?user_id=u1")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "persistence_unavailable"
    finally:
        app.state.db_client = original


def test_delete_constraint__removes_own_row_only():
    from uuid import uuid4

    from fastapi.testclient import TestClient

    import app.core.config as cfg
    from app.container import AppContainer
    from app.main import app
    from app.orchestrator.hooks import ActionHooks
    from tests.fakes import BagOfWordsEmbedder, FakeSupabase

    mine, theirs = str(uuid4()), str(uuid4())
    fake = FakeSupabase()
    fake.rows.setdefault("behavioral_constraints", []).extend([
        {"id": mine, "user_id": "u1", "raw_text": "no study before 11am",
         "constraint_type": "habit"},
        {"id": theirs, "user_id": "OTHER", "raw_text": "leak", "constraint_type": "habit"},
    ])

    class _DB:
        supabase = fake

    original = getattr(app.state, "db_client", None)
    original_container = getattr(app.state, "container", None)
    app.state.db_client = _DB()
    app.state.container = AppContainer.build(
        supabase_client=fake, cfg=cfg, hooks=ActionHooks(), embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )
    try:
        client = TestClient(app)
        assert client.delete(f"/api/v1/habits/constraints/{mine}?user_id=u1").status_code == 200
        assert [r["id"] for r in fake.rows["behavioral_constraints"]] == [theirs]
        # wrong user: 404, row untouched
        assert client.delete(f"/api/v1/habits/constraints/{theirs}?user_id=u1").status_code == 404
        assert len(fake.rows["behavioral_constraints"]) == 1
    finally:
        app.state.db_client = original
        app.state.container = original_container


# ---------------------------------------------------------------------------
# PATCH /api/v1/tasks/{task_id} — status is limited to what user_tasks accepts
# ---------------------------------------------------------------------------


def test_patch_task__unknown_status__422():
    """`rolled_forward` passed validation and then failed the table's status CHECK."""
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.fakes import FakeSupabase

    fake = FakeSupabase({"user_tasks": [
        {"user_id": "u1", "task_id": "t1", "title": "Study CNNs", "status": "pending"},
    ]})

    class _DB:
        supabase = fake

    original = getattr(app.state, "db_client", None)
    app.state.db_client = _DB()
    try:
        resp = TestClient(app).patch(
            "/api/v1/tasks/t1", json={"user_id": "u1", "status": "rolled_forward"}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        assert fake.rows["user_tasks"][0]["status"] == "pending"
    finally:
        app.state.db_client = original


def test_patch_task__status__422_points_to_complete_and_skip(monkeypatch):
    """An outcome is recorded by Complete or Skip (one event each), never by PATCH."""
    fake = FakeSupabase({"user_tasks": [_task_row("t1")]})
    client = _client_over(fake, monkeypatch)

    resp = client.patch("/api/v1/tasks/t1", json={"user_id": "u1", "status": "completed"})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert "/complete" in error["message"] and "/skip" in error["message"]
    assert fake.rows["user_tasks"][0]["status"] == "pending"
    assert fake.rows.get("task_events", []) == []


def test_patch_task__pacing_pushed__422(monkeypatch):
    """pacing_pushed is retired: skipped tasks are ``skipped``, and the CHECK drops it."""
    fake = FakeSupabase({"user_tasks": [_task_row("t1")]})
    client = _client_over(fake, monkeypatch)

    resp = client.patch("/api/v1/tasks/t1", json={"user_id": "u1", "status": "pacing_pushed"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert fake.rows["user_tasks"][0]["status"] == "pending"


def test_complete_endpoint__unknown_task__404_envelope(monkeypatch):
    fake = FakeSupabase({"user_tasks": [_task_row("t1", user_id="someone_else")]})
    client = _client_over(fake, monkeypatch)

    resp = client.post("/api/v1/tasks/t1/complete", json={"user_id": "u1", "quality": 4})

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "not_found"
    assert fake.rows.get("task_events", []) == []
    assert fake.rows["user_tasks"][0]["status"] == "pending"


def test_skip_endpoint__unknown_task__404_envelope(monkeypatch):
    fake = FakeSupabase({"user_tasks": [_task_row("t1", user_id="someone_else")]})
    client = _client_over(fake, monkeypatch)

    resp = client.post("/api/v1/tasks/t1/skip", json={"user_id": "u1"})

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "not_found"
    assert fake.rows.get("task_events", []) == []
    assert fake.rows["user_tasks"][0]["status"] == "pending"


def test_complete_and_skip__no_database__same_503_envelope_as_list(monkeypatch):
    """With Supabase unreachable the container runs in memory, where every task is
    unknown: answering 404 would tell the user their task is gone."""
    from fastapi.testclient import TestClient

    import app.core.config as cfg
    from app.container import AppContainer
    from app.main import app
    from app.orchestrator.hooks import ActionHooks

    monkeypatch.setattr(app.state, "db_client", None, raising=False)
    monkeypatch.setattr(
        app.state,
        "container",
        AppContainer.build(supabase_client=None, cfg=cfg, hooks=ActionHooks()),
        raising=False,
    )
    client = TestClient(app)

    listed = client.get("/api/v1/tasks/?user_id=u1")
    completed = client.post("/api/v1/tasks/t1/complete", json={"user_id": "u1", "quality": 4})
    skipped = client.post("/api/v1/tasks/t1/skip", json={"user_id": "u1"})

    def shape(resp):
        error = resp.json()["error"]
        return resp.status_code, error["code"], error["message"]

    assert shape(listed)[0] == 503
    assert shape(completed) == shape(listed)
    assert shape(skipped) == shape(listed)


# The background replan (control_policy.trigger_replan) has never run to completion
# and stays off. The task endpoints must not schedule it, nor tell the user their
# schedule was updated. The response keys stay: the frontend reads them.
_REPLAN_CLAIMS = ("schedule updated", "schedule adjusted", "slotted", "moves to", "rescheduled")


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("post", "/api/v1/tasks/t1/complete", {"user_id": "u1", "quality": 4}),
        ("post", "/api/v1/tasks/t1/skip", {"user_id": "u1"}),
        ("post", "/api/v1/tasks/t2/skip", {"user_id": "u1"}),
        ("patch", "/api/v1/tasks/t1", {"user_id": "u1", "duration_minutes": 20}),
        ("delete", "/api/v1/tasks/t1?user_id=u1", None),
    ],
)
def test_task_endpoints__any_change__schedule_no_replan_and_claim_none(
    method, path, body, monkeypatch
):
    from app.main import app

    replans: list[tuple] = []

    async def recording_replan(*args, **kwargs):
        replans.append(args)

    monkeypatch.setattr("app.services.analytical.control_policy.trigger_replan", recording_replan)
    fake = FakeSupabase({"user_tasks": [
        _task_row("t1", title="Study CNNs", minutes=25),
        _task_row("t2", title="Write the essay", minutes=90),
    ]})
    monkeypatch.setattr(app.state, "memory_store", None, raising=False)

    client = _client_over(fake, monkeypatch)
    response = client.request(method.upper(), path, json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"task_id", "status", "message", "replan_triggered"}
    assert payload["replan_triggered"] is False
    assert [c for c in _REPLAN_CLAIMS if c in payload["message"].lower()] == []
    assert replans == []


# --- the skip reason (spec 9.1), one error type, typed responses ----------------------


def test_skip_endpoint__with_reason__kept_on_the_skipped_event_and_anti_guilt_reply(monkeypatch):
    fake = FakeSupabase({"user_tasks": [_task_row("t1")]})
    client = _client_over(fake, monkeypatch)

    resp = client.post(
        "/api/v1/tasks/t1/skip", json={"user_id": "u1", "reason": "Too tired after the lab"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    assert resp.json()["message"].startswith("Life happened")
    (event,) = fake.rows["task_events"]
    assert (event["event_type"], event["metadata"]["reason"]) == (
        "skipped", "Too tired after the lab"
    )


def test_skip_endpoint__reason_too_long__422_and_nothing_recorded(monkeypatch):
    fake = FakeSupabase({"user_tasks": [_task_row("t1")]})
    client = _client_over(fake, monkeypatch)

    resp = client.post("/api/v1/tasks/t1/skip", json={"user_id": "u1", "reason": "x" * 501})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert fake.rows.get("task_events", []) == []
    assert fake.rows["user_tasks"][0]["status"] == "pending"


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("patch", "/api/v1/tasks/nope", {"user_id": "u1", "title": "Renamed"}),
        ("delete", "/api/v1/tasks/nope?user_id=u1", None),
    ],
)
def test_task_endpoints__unknown_task__not_found_envelope(method, path, body, monkeypatch):
    client = _client_over(FakeSupabase({"user_tasks": [_task_row("t1")]}), monkeypatch)

    resp = client.request(method.upper(), path, json=body)

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert (error["code"], error["message"]) == ("not_found", "That task does not exist.")


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/v1/tasks/"),
        ("delete", "/api/v1/tasks/"),
        ("post", "/api/v1/drafts/{draft_id}/accept"),
    ],
)
def test_openapi__task_and_draft_accept_routes__declare_a_response_model(method, path):
    from app.main import app

    response = app.openapi()["paths"][path][method]["responses"]["200"]
    schema = response["content"]["application/json"]["schema"]

    assert "$ref" in schema, schema
