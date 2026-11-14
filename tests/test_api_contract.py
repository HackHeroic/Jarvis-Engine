"""The API contract: a typed response on every route, one error envelope, documented.

Every JSON route declares a Pydantic response model, and the generated OpenAPI schema
says so: a typed 2xx response for every operation, and the one error envelope
(``ErrorResponse``) for 4xx and 5xx, never FastAPI's default validation shape. The
stream is the one route without a JSON body: its description points at the frame
catalogue and its 200 documents the ``complete`` frame.
"""

import ast
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from pydantic import BaseModel

from tests.fakes import FakeDBClient, FakeSupabase, build_test_container

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS = ROOT / "app" / "api" / "v1" / "endpoints"

# The one route with no JSON body: the chat stream (server-sent events). Nothing else may
# go untyped.
UNTYPED_ROUTES = frozenset({("POST", "/api/v1/chat/stream")})


def _routes() -> list[APIRoute]:
    from app.main import app

    return [r for r in app.routes if isinstance(r, APIRoute)]


def _untyped(route: APIRoute) -> bool:
    model = route.response_model
    return not (isinstance(model, type) and issubclass(model, BaseModel))


# --- typed responses ---------------------------------------------------------------------


def test_openapi__every_json_route__declares_a_typed_response_model():
    untyped = sorted(
        f"{method} {route.path}"
        for route in _routes()
        for method in route.methods
        if _untyped(route) and (method, route.path) not in UNTYPED_ROUTES
    )

    assert untyped == []


def _openapi() -> dict:
    from app.main import app

    app.openapi_schema = None  # built fresh, from the routes as they are now
    return app.openapi()


def test_openapi__schema__every_operation_has_a_typed_2xx_but_the_stream():
    schema = _openapi()
    untyped = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            ok = [
                body for status, body in operation["responses"].items()
                if status.startswith("2")
                and "$ref" in str(body.get("content", {}).get("application/json", {}).get("schema"))
            ]
            if not ok and (method.upper(), path) not in UNTYPED_ROUTES:
                untyped.append(f"{method.upper()} {path}")

    assert untyped == []


def test_openapi__errors__documented_with_the_envelope_never_the_default_shape():
    schema = _openapi()
    envelope = {"$ref": "#/components/schemas/ErrorResponse"}

    missing = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for status in ("422", "4XX", "5XX"):
                documented = operation["responses"].get(status, {})
                if documented.get("content", {}).get("application/json", {}).get("schema") != envelope:
                    missing.append(f"{method.upper()} {path} {status}")

    assert missing == []
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "ErrorResponse" in schema["components"]["schemas"]


def test_openapi__chat_stream__documents_the_complete_frame():
    from app.schemas.chat_stream import ChatCompletePayload

    schema = _openapi()
    operation = schema["paths"]["/api/v1/chat/stream"]["post"]

    assert "SSE_EVENTS" in operation["description"]  # the frame catalogue
    assert "ChatCompletePayload" in operation["description"]
    stream = operation["responses"]["200"]["content"]["text/event-stream"]["schema"]
    assert stream["$ref"] == "#/components/schemas/ChatCompletePayload"
    documented = schema["components"]["schemas"]["ChatCompletePayload"]
    assert set(documented["properties"]) == set(ChatCompletePayload.model_fields)


# --- one error envelope ------------------------------------------------------------------


def _endpoint_files() -> list[Path]:
    return sorted(ENDPOINTS.glob("*.py"))


def test_endpoints__raise_no_http_exception():
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in _endpoint_files()
        for node in ast.walk(ast.parse(path.read_text()))
        if (isinstance(node, ast.Name) and node.id == "HTTPException")
        or (isinstance(node, ast.alias) and node.name == "HTTPException")
    ]

    assert offenders == [], "raise a JarvisError: the handler answers it with the envelope"


def test_endpoints__no_ad_hoc_error_bodies():
    offenders = [
        path.name for path in _endpoint_files()
        if "JSONResponse(" in path.read_text() or '{"status": "error"' in path.read_text()
    ]

    assert offenders == []


@pytest_asyncio.fixture
async def api(monkeypatch):
    """The app over an empty in-memory container and a database with nothing in it."""
    from app.main import app

    monkeypatch.setattr(app.state, "container", build_test_container(), raising=False)
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(FakeSupabase()), raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _error(response: httpx.Response) -> dict:
    return response.json()["error"]


@pytest.mark.asyncio
async def test_chat_stream__unknown_model_mode__422_envelope(api):
    response = await api.post(
        "/api/v1/chat/stream", json={"user_prompt": "hi", "user_id": "u1", "model_mode": "27b"}
    )

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert _error(response)["details"]["errors"][0]["loc"] == ["body", "model_mode"]


def test_chat_stream_request__model_modes__exactly_the_ones_the_turn_reads():
    """'auto', and 'cloud' with the aliases the turn maps to it ('gemini', the older '4b')."""
    from typing import get_args

    from app.schemas.chat_stream import ChatStreamRequest
    from app.services.chat_turn.service import CLOUD_MODEL_MODES

    accepted = set(get_args(ChatStreamRequest.model_fields["model_mode"].annotation))

    assert accepted == {"auto", *CLOUD_MODEL_MODES}
    assert CLOUD_MODEL_MODES == frozenset({"cloud", "gemini", "4b"})


@pytest.mark.asyncio
async def test_chat_stream__unknown_media_type__422_envelope(api):
    response = await api.post("/api/v1/chat/stream", json={
        "user_prompt": "my syllabus", "user_id": "u1", "file_base64": "AAAA", "media_type": "exe",
    })

    assert response.status_code == 422
    assert _error(response)["details"]["errors"][0]["loc"] == ["body", "media_type"]


@pytest.mark.asyncio
async def test_ingestion__oversized_upload__422_envelope(api):
    from app.core.config import MAX_CHAT_UPLOAD_BYTES

    oversized = "A" * (4 * (MAX_CHAT_UPLOAD_BYTES // 3 + 1))
    response = await api.post(
        "/api/v1/ingestion/process", json={"file_base64": oversized, "media_type": "pdf"}
    )

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert "MB" in _error(response)["details"]["errors"][0]["msg"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [{"duration_minutes": 0}, {"duration_minutes": 26},
                                  {"start_min": -5}])
async def test_draft_task_edit__duration_out_of_bounds__422(api, edit):
    response = await api.patch(
        "/api/v1/drafts/00000000-0000-4000-8000-000000000001/tasks/t1",
        params={"user_id": "u1"}, json=edit,
    )

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), [
    ("get", "/api/v1/drafts/00000000-0000-4000-8000-000000000001"),
    ("get", "/api/v1/sessions/00000000-0000-4000-8000-000000000001"),
    ("delete", "/api/v1/documents/doc-that-is-not-there"),
])
async def test_missing_record__404_not_found_envelope(api, method, path):
    response = await api.request(method.upper(), path, params={"user_id": "u1"})

    assert response.status_code == 404
    assert _error(response)["code"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), [
    ("get", "/api/v1/sessions/"),
    ("get", "/api/v1/sessions/00000000-0000-4000-8000-000000000001"),
    ("get", "/api/v1/sessions/00000000-0000-4000-8000-000000000001/messages"),
    ("delete", "/api/v1/sessions/"),
    ("get", "/api/v1/documents/"),
    ("delete", "/api/v1/documents/doc-1"),
])
async def test_no_database__503_persistence_unavailable_envelope(api, monkeypatch, method, path):
    from app.main import app

    monkeypatch.setattr(app.state, "db_client", None, raising=False)
    response = await api.request(method.upper(), path, params={"user_id": "u1"})

    assert response.status_code == 503
    assert _error(response)["code"] == "persistence_unavailable"


class _UnreachableTable:
    """A table whose every query fails, as on a network blip."""

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise ConnectionError("[SSL] could not reach db.internal")

        return fail


class _Unreachable(FakeSupabase):
    """A database whose named tables cannot be reached; the rest answer."""

    def __init__(self, *unreachable: str) -> None:
        super().__init__()
        self._unreachable = set(unreachable)

    def table(self, name: str):
        return _UnreachableTable() if name in self._unreachable else super().table(name)


_SESSION = "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "unreachable"), [
    ("/api/v1/sessions/", ("chat_sessions",)),
    (f"/api/v1/sessions/{_SESSION}/messages", ("chat_messages",)),
    (f"/api/v1/sessions/{_SESSION}", ("chat_messages",)),
])
async def test_sessions_read__database_unreachable__503_envelope_never_an_empty_list(
    api, monkeypatch, path, unreachable
):
    """An outage is a 503, as on every other route: an empty list would tell the user
    their conversations are gone."""
    from app.main import app

    supabase = _Unreachable(*unreachable)
    FakeSupabase.table(supabase, "chat_sessions").insert(  # the session row exists
        {"id": _SESSION, "user_id": "u1", "title": "DSA"}
    ).execute()
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(supabase), raising=False)

    response = await api.get(path, params={"user_id": "u1"})

    assert response.status_code == 503
    assert _error(response)["code"] == "persistence_unavailable"
    assert "SSL" not in response.text and "db.internal" not in response.text


@pytest.mark.asyncio
async def test_chat_context_window__messages_unreachable__the_turn_goes_on_with_no_history(caplog):
    """The chat turn reads the same messages: there an outage degrades to no history, logged."""
    from app.services.chat_history import build_context_messages

    window = await build_context_messages(_SESSION, "u1", supabase=_Unreachable("chat_messages"))

    assert window == []
    assert "history could not be read" in caplog.text


# --- a body is capped before it is read ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/chat/stream", "/api/v1/ingestion/process"])
async def test_upload_routes__content_length_over_the_cap__413_envelope_before_the_body_is_read(
    api, monkeypatch, path
):
    """The field limits (an 8,000-character message, a 10 MB file) are checked after
    the whole body is parsed; a body far past them is refused on its declared length."""
    from app.api.request_limits import MAX_REQUEST_BODY_BYTES

    reached = []
    monkeypatch.setattr(
        "app.services.chat_turn.service.ChatTurnService.open_turn",
        lambda *a, **k: reached.append("chat"),
    )
    monkeypatch.setattr(
        "app.api.v1.endpoints.ingestion.process_ingestion",
        lambda **k: reached.append("ingestion"),
    )

    response = await api.post(
        path, content=b'{"user_id": "u1"}',
        headers={"content-type": "application/json",
                 "content-length": str(MAX_REQUEST_BODY_BYTES + 1)},
    )

    assert response.status_code == 413
    assert _error(response)["code"] == "payload_too_large"
    assert _error(response)["details"] == {"max_bytes": MAX_REQUEST_BODY_BYTES}
    assert _error(response)["request_id"] == response.headers["x-request-id"]
    assert reached == []


def test_request_body_cap__room_for_the_largest_valid_request():
    """The cap sits above the biggest body the field limits allow: a 10 MB file in
    base64 and an 8,000-character message, every character escaped."""
    from app.api.request_limits import MAX_REQUEST_BODY_BYTES
    from app.core.config import MAX_CHAT_PROMPT_CHARS, MAX_CHAT_UPLOAD_BYTES

    largest_file = 4 * -(-MAX_CHAT_UPLOAD_BYTES // 3)
    largest_message = MAX_CHAT_PROMPT_CHARS * len('"\\ud83d\\ude00"')
    assert largest_file + largest_message < MAX_REQUEST_BODY_BYTES < 2 * largest_file


async def _post_through_cap(body, *, limit: int = 64):
    """POST ``body`` (bytes, or an iterator of chunks: sent chunked, with no length)
    to a route behind a ``BodyLimitMiddleware`` capping it at ``limit`` bytes."""
    from fastapi import FastAPI, Request

    from app.api.error_handlers import RequestIdMiddleware, register_error_handlers
    from app.api.request_limits import BodyLimitMiddleware

    app = FastAPI()
    seen = []

    @app.post("/capped")
    async def capped(request: Request) -> dict:
        seen.append(await request.body())
        return {"ok": True}

    app.add_middleware(BodyLimitMiddleware, limits={"/capped": limit})
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/capped", content=body), seen


@pytest.mark.asyncio
async def test_body_limit__chunked_body_over_the_cap__413_and_the_route_never_runs():
    async def chunks():
        for _ in range(10):
            yield b"x" * 10

    response, seen = await _post_through_cap(chunks())

    assert response.status_code == 413
    assert _error(response)["code"] == "payload_too_large"
    assert seen == []


@pytest.mark.asyncio
async def test_body_limit__chunked_body_within_the_cap__the_route_reads_all_of_it():
    async def chunks():
        for _ in range(4):
            yield b"x" * 10

    response, seen = await _post_through_cap(chunks())

    assert response.status_code == 200
    assert seen == [b"x" * 40]


# --- the schedule: infeasible is a scope problem, in the envelope ------------------------


def _graph(minutes: int):
    from app.schemas.decomposition import ExecutionGraph, GoalMetadata, TaskChunk

    return ExecutionGraph(
        goal_metadata=GoalMetadata(objective="Revise", outcome_visualization="ready"),
        decomposition=[
            TaskChunk(task_id=f"t{i}", title=f"Task {i}", duration_minutes=25,
                      difficulty_weight=0.5, completion_criteria="done")
            for i in range(minutes // 25)
        ],
        cognitive_load_estimate={"intrinsic_load": 0.5},
    )


@pytest.mark.asyncio
async def test_run_schedule__infeasible__schedule_infeasible_error_and_generate_endpoint_422_envelope(
    api, deterministic_solver
):
    from datetime import datetime, timezone

    from app.domain.errors import ScheduleInfeasibleError
    from app.schemas.context import Availability, TimeSlot
    from app.services.planning.scheduling import run_schedule

    blocked = [TimeSlot(name="away", start_min=0, end_min=960, availability=Availability.BLOCKED)]
    with pytest.raises(ScheduleInfeasibleError):
        run_schedule(_graph(250), list(blocked), horizon_minutes=960,
                     horizon_start=datetime(2099, 1, 5, 2, 30, tzinfo=timezone.utc))

    response = await api.post("/api/v1/schedule/generate-schedule", json={
        "graph": _graph(250).model_dump(mode="json"),
        "daily_context": [s.model_dump(mode="json") for s in blocked],
        "horizon_minutes": 960,
        "plan_start": "2099-01-05T08:00:00+05:30",
    })

    assert response.status_code == 422
    assert _error(response)["code"] == "schedule_infeasible"


def test_consent__is_data_not_an_exception():
    """Consent reaches the user as a stored suggestion or a consent_request frame;
    the 202 "error" nothing raised is gone."""
    from app.domain import errors

    assert not hasattr(errors, "ConsentRequiredError")
