"""The route table: one chat endpoint, and the retired architecture gone for good.

``POST /api/v1/chat/stream`` is the one turn handler. Its old path,
``/api/v1/chat/v2/stream``, is gone: nothing calls it any more.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

_APP_DIR = Path(__file__).resolve().parent.parent / "app"

RETIRED_MODULES = [
    "app.services.analytical.control_policy",
    "app.services.intent_registry",
    "app.services.analytical.task_editor",
    "app.services.analytical.task_rearranger",
    "app.services.analytical.task_retrieval",
    "app.services.memory.pearl",
    "app.services.memory.extractor",
    "app.services.memory.retriever",
    "app.services.memory.store",
]


def test_routes__chat__stream_is_the_one_turn_handler():
    from app.api.v1.endpoints import chat
    from app.main import app

    routes = {
        route.path: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/chat")
    }

    assert set(routes) == {"/api/v1/chat/stream"}
    stream = routes["/api/v1/chat/stream"]
    assert stream.endpoint is chat.chat_stream
    assert (stream.deprecated, stream.include_in_schema) == (None, True)


@pytest.mark.asyncio
async def test_routes__old_chat_path__404_envelope():
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat/v2/stream", json={"user_prompt": "hi", "user_id": "u1"}
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"


@pytest.mark.parametrize("name", RETIRED_MODULES)
def test_v1__retired_modules__not_importable(name):
    assert importlib.util.find_spec(name) is None


def _callers_of(function: str) -> list[str]:
    """Files under app/ that call ``function``: by bare name or as an attribute, called
    directly or handed to another callable (``asyncio.to_thread(function, ...)``).

    Its own ``def`` and the imports of it are not uses.
    """
    callers = []
    for path in sorted(_APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            used = (isinstance(node, ast.Name) and node.id == function) or (
                isinstance(node, ast.Attribute) and node.attr == function
            )
            if used:
                callers.append(path.relative_to(_APP_DIR).as_posix())
                break
    return callers


def test_off_turn_resolve__only_the_replanner_calls_resolve_from_now():
    """The one solve outside a planning turn is the replanner's re-slot from now, built on
    the planning module's own time slots. The draft-edit re-solves that rebuilt their
    constraints from habit text alone (/chat/modify-schedule, the v1 modify flow) are gone."""
    assert _callers_of("resolve_from_now") == ["modules/planning_replan.py"]
