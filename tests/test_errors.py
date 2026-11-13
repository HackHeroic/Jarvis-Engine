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
