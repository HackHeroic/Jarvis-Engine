"""The JarvisError hierarchy and the single HTTP error envelope.

Every error response, whatever raised it, has one shape:
``{"error": {"code", "message", "details", "request_id"}}`` — and an unhandled
exception never puts its own text on the wire.
"""

import re

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.error_handlers import RequestIdMiddleware, register_error_handlers
from app.domain import errors

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class _Item(BaseModel):
    name: str
    qty: int


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    @app.get("/not-found")
    async def not_found() -> None:
        raise errors.NotFoundError("no draft")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret db dsn")

    @app.post("/items")
    async def create_item(item: _Item) -> _Item:
        return item

    @app.get("/http-404")
    async def http_404() -> None:
        raise HTTPException(status_code=404, detail="x")

    @app.get("/http-structured")
    async def http_structured() -> None:
        raise HTTPException(status_code=400, detail={"field": "deadline", "problem": "in the past"})

    @app.get("/http-599")
    async def http_599() -> None:
        raise HTTPException(status_code=599, detail=["upstream", "gave up"])

    @app.get("/http-304")
    async def http_304() -> None:
        raise HTTPException(status_code=304)

    @app.get("/http-204")
    async def http_204() -> None:
        raise HTTPException(status_code=204)

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=False)


def test_error_envelope__jarvis_error__maps_status_and_code(client):
    resp = client.get("/not-found")

    assert resp.status_code == 404
    body = resp.json()
    request_id = body["error"]["request_id"]
    assert _HEX32.match(request_id)
    assert body == {
        "error": {
            "code": "not_found",
            "message": "no draft",
            "details": None,
            "request_id": request_id,
        }
    }
    assert resp.headers["X-Request-ID"] == request_id


def test_error_envelope__unhandled_exception__hides_internals(client):
    resp = client.get("/boom")

    assert resp.status_code == 500
    assert "secret" not in resp.text
    error = resp.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == "Something went wrong on our side."
    assert _HEX32.match(error["request_id"])
    assert resp.headers["X-Request-ID"] == error["request_id"]


def test_error_envelope__validation__422_with_field_details(client):
    resp = client.post("/items", json={"name": "pen", "qty": "lots"})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert _HEX32.match(error["request_id"])
    locs = [field["loc"] for field in error["details"]["errors"]]
    assert ["body", "qty"] in locs


def test_error_envelope__http_exception__keeps_status(client):
    resp = client.get("/http-404")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "http_error"
    assert error["message"] == "x"


def test_error_envelope__http_exception_with_structured_detail__kept_under_details(client):
    resp = client.get("/http-structured")

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "http_error"
    assert error["message"] == "Bad Request"
    assert error["details"] == {"detail": {"field": "deadline", "problem": "in the past"}}


def test_error_envelope__http_exception_with_non_standard_status__still_answered(client):
    resp = client.get("/http-599")

    assert resp.status_code == 599
    error = resp.json()["error"]
    assert error["code"] == "http_error"
    assert error["details"] == {"detail": ["upstream", "gave up"]}
    assert resp.headers["X-Request-ID"] == error["request_id"]


@pytest.mark.parametrize("status", [204, 304])
def test_error_envelope__bodiless_status__answered_without_a_body(client, status):
    resp = client.get(f"/http-{status}")

    assert resp.status_code == status
    assert resp.content == b""
    assert _HEX32.match(resp.headers["X-Request-ID"])


def test_error_envelope__unhandled_exception__logged_with_the_responses_request_id(client, caplog):
    with caplog.at_level("ERROR", logger="app.api.error_handlers"):
        resp = client.get("/boom")

    request_id = resp.json()["error"]["request_id"]
    (record,) = [r for r in caplog.records if r.name == "app.api.error_handlers"]
    assert request_id in record.getMessage()
    assert record.exc_info is not None and "secret db dsn" in str(record.exc_info[1])


class _DB:
    def __init__(self, connected: bool | None) -> None:
        self.connected = connected

    async def check_connection(self) -> bool:
        if self.connected is None:
            raise ConnectionError("dsn=postgres://user:pw@host")
        return self.connected


@pytest.mark.parametrize("connected", [False, None], ids=["check_false", "check_raises"])
def test_health__database_unreachable__503_persistence_envelope(monkeypatch, connected):
    from app.main import app

    monkeypatch.setattr(app.state, "db_client", _DB(connected), raising=False)

    resp = TestClient(app).get("/health")

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "persistence_unavailable"
    assert error["message"] == "Database is unreachable."
    assert error["details"] == {"database": "disconnected"}
    assert "postgres" not in resp.text


def test_health__database_up__healthy(monkeypatch):
    from app.main import app

    monkeypatch.setattr(app.state, "db_client", _DB(True), raising=False)

    resp = TestClient(app).get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy", "database": "connected"}


def test_cors__frontend_origin__may_read_the_request_id_header():
    from app.main import app

    resp = TestClient(app).get("/", headers={"Origin": "http://localhost:3000"})

    exposed = resp.headers.get("access-control-expose-headers", "")
    assert "x-request-id" in exposed.lower()


def test_error_envelope__main_app_unknown_route__envelope_and_request_id_header():
    from app.main import app

    resp = TestClient(app).get("/no-such-route")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "http_error"
    assert resp.headers["X-Request-ID"] == error["request_id"]


def test_errors__hierarchy__subclassing():
    assert issubclass(errors.InvalidStateTransition, errors.ConflictError)
    assert issubclass(errors.LLMTimeoutError, errors.InfrastructureError)
