"""Shared test fixtures for Jarvis Engine tests."""

import socket

import pytest
from unittest.mock import MagicMock

from tests.fakes import CANNED_LLM_REPLY

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


@pytest.fixture(scope="session", autouse=True)
def _no_network():
    """Fail loudly on any non-local socket connect during the test suite.

    A real GEMINI_API_KEY, CHROMA_API_KEY and SUPABASE_URL are present in the
    dev .env, so an unmocked call is a billed call, not just a slow one. Two
    tests were silently reaching Gemini and ChromaDB Cloud before this guard
    existed; they only looked harmless because DNS happens to be dead on this
    machine. Localhost stays open (LM Studio, SQLite checkpointer).
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address):
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, str) and host not in _LOCAL_HOSTS:
            raise RuntimeError(
                f"Blocked outbound network call to {host!r} during tests. "
                "Mock the boundary (see the `no_llm` fixture in tests/conftest.py) "
                "— tests must never hit real LLMs, Supabase or ChromaDB."
            )

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex


@pytest.fixture
def no_llm(monkeypatch):
    """Replace every outbound model/RAG boundary with canned in-memory results.

    Returns the running call log so tests can assert the mocked path was the
    one actually taken. Patch targets:

    * ``app.core.model_router.route_llm_call`` — modules import it
      function-locally, so the module attribute is the live target.
    * ``app.services.analytical.voice_of_jarvis.hybrid_route_query`` — bound at
      import time inside that module.
    * ``app.utils.chroma_client.query_knowledge`` — the conversation module's
      RAG lookup, which dials ChromaDB Cloud.
    """
    calls = []

    async def fake_route_llm_call(*args, **kwargs):
        calls.append(("route_llm_call", kwargs.get("task")))
        return CANNED_LLM_REPLY

    async def fake_hybrid_route_query(*args, **kwargs):
        calls.append(("hybrid_route_query", kwargs.get("model_override")))
        return CANNED_LLM_REPLY

    def fake_query_knowledge(*args, **kwargs):
        calls.append(("query_knowledge", None))
        return []

    monkeypatch.setattr("app.core.model_router.route_llm_call", fake_route_llm_call)
    monkeypatch.setattr(
        "app.services.analytical.voice_of_jarvis.hybrid_route_query",
        fake_hybrid_route_query,
    )
    monkeypatch.setattr("app.utils.chroma_client.query_knowledge", fake_query_knowledge)
    return calls


@pytest.fixture(autouse=True)
def _registered_modules():
    """Populate the global module registry before any test builds a graph.

    ``build_jarvis_graph`` adds one node per registered module and then wires
    conditional edges to ``planning_module`` / ``research_agent`` /
    ``knowledge_module`` unconditionally. In the app those names exist because
    lifespan startup calls ``register_default_modules()``; under pytest nothing
    does, so the graph compiles against an empty registry and LangGraph raises
    ``ValueError: ... unknown target 'planning_module'``.
    """
    from app.modules import module_registry, register_default_modules

    if not module_registry.registered_names():
        register_default_modules()
    yield


@pytest.fixture
def mock_supabase():
    """Mock Supabase client that returns empty results by default."""
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table

    empty_result = MagicMock()
    empty_result.data = []

    for method in ["select", "insert", "update", "delete", "upsert"]:
        chained = getattr(table, method).return_value
        chained.execute.return_value = empty_result
        chained.eq.return_value = chained
        chained.neq.return_value = chained
        chained.gt.return_value = chained
        chained.lt.return_value = chained
        chained.is_.return_value = chained
        chained.order.return_value = chained
        chained.limit.return_value = chained

    return client


@pytest.fixture
def mock_llm_response():
    """Factory for mocking hybrid_route_query responses."""
    def _make(response_data):
        async def _mock_route(*args, **kwargs):
            schema = kwargs.get("response_schema")
            if schema and isinstance(response_data, dict):
                return schema.model_validate(response_data)
            return response_data
        return _mock_route
    return _make


@pytest.fixture
def sample_tasks():
    """Sample TaskChunk data for testing."""
    return [
        {
            "task_id": "goal1_t1",
            "title": "Study CNNs - convolution layers",
            "duration_minutes": 25,
            "difficulty_weight": 0.5,
            "dependencies": [],
            "completion_criteria": "Explain convolution operation",
            "deadline_hint": None,
        },
        {
            "task_id": "goal1_t2",
            "title": "Study backpropagation math",
            "duration_minutes": 25,
            "difficulty_weight": 0.7,
            "dependencies": ["goal1_t1"],
            "completion_criteria": "Derive backprop gradient",
            "deadline_hint": None,
        },
        {
            "task_id": "goal1_t3",
            "title": "Practice: implement basic neural network",
            "duration_minutes": 25,
            "difficulty_weight": 0.6,
            "dependencies": ["goal1_t2"],
            "completion_criteria": "Working forward pass in Python",
            "deadline_hint": None,
        },
    ]
