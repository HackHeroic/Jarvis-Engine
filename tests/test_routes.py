"""The route table: one chat endpoint, and the retired architecture gone for good.

``POST /api/v1/chat/stream`` is the one turn handler. Its old path,
``/api/v1/chat/v2/stream``, is gone: nothing calls it any more.
"""

import ast
import importlib
import re
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
    constraints from habit text alone (/chat/modify-schedule, the retired modify flow) are gone."""
    assert _callers_of("resolve_from_now") == ["modules/planning_replan.py"]


# Production code that only tests called (or nothing read), removed with the wave that
# found it: (module, name). A live reader would bring it back; these have none.
REMOVED_DEAD_CODE = [
    ("app.services.draft_actions", "accept_draft_and_persist"),
    ("app.services.analytical.mastery_tracker", "_get_reschedule_count"),
    ("app.services.analytical.voice_of_jarvis", "summary_capture"),
    ("app.utils.deadline_parser", "compute_horizon_from_deadlines"),
    ("app.utils.embedding", "embed_text"),
    ("app.core.config", "MAX_DEEP_WORK_MINUTES_PER_DAY"),
    ("app.core.config", "LM_STUDIO_NATIVE_URL"),
    ("app.core.config", "AWS_ACCESS_KEY_ID"),
    ("app.core.config", "AWS_SECRET_ACCESS_KEY"),
    # The S3 upload that never ran (boto3 was never installed): files go to Supabase Storage.
    ("app.core.config", "AWS_REGION"),
    ("app.core.config", "AWS_BUCKET_NAME"),
]


@pytest.mark.parametrize(("module", "name"), REMOVED_DEAD_CODE)
def test_dead_code__removed__absent_and_called_nowhere(module, name):
    assert not hasattr(importlib.import_module(module), name)
    assert _callers_of(name) == []


# What the retired architecture was called. Nothing in app/ or scripts/ names it any more,
# in code or in prose: a comment citing a deleted function sends a reader looking for it.
RETIRED_NAMES = re.compile(
    r"_run_plan_day_flow|execute_agentic_flow|control_policy|\bDraftStore\b|\bMemoryStore\b"
    r"|runtime\.set_container|v2 pipeline|both pipelines|\bv[12]\b(?! ?/)|chat/v2"
)


def _names_the_retired_architecture(path: Path) -> list[str]:
    hits = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        # The API's version (/api/v1/..., "API v1") and model names (all-MiniLM-L6-v2,
        # /v1/models) are not it.
        cleaned = re.sub(r"(?i)api[./ ]v1|MiniLM-L6-v2|/v1\b", "", line)
        if RETIRED_NAMES.search(cleaned):
            hits.append(f"{path.relative_to(_APP_DIR.parent).as_posix()}:{number}")
    return hits


def test_source__names_nothing_of_the_retired_architecture():
    root = _APP_DIR.parent
    files = [*sorted(_APP_DIR.rglob("*.py")), *sorted((root / "scripts").rglob("*.py"))]

    assert [hit for path in files for hit in _names_the_retired_architecture(path)] == []


def test_test_names__chat_tests_name_the_one_chat_stream():
    tests = Path(__file__).resolve().parent
    stale = sorted(
        f"{path.name}:{node.name}"
        for path in tests.rglob("test_*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_chat_v2__")
    )

    assert stale == []
