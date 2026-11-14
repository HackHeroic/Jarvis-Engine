"""Document search (ChromaDB) answers a user from that user's chunks only, and only close ones.

The search runs on every chat turn and in ``decompose_goal``, and whatever it
returns is pasted into the model's prompt. A fake collection stands in for
ChromaDB: it filters on ``where`` the way Chroma does and reports a fixed
cosine distance per chunk.
"""

import threading
from types import SimpleNamespace

import pytest

import app.utils.chroma_client as chroma


class _FakeCollection:
    """Chunks with metadata; ``query`` honours ``where`` and returns per-chunk distances."""

    def __init__(self, chunks: list[dict], space: str = "cosine") -> None:
        self.chunks = chunks  # {"id", "document", "metadata", "distance"}
        self.metadata = {"hnsw:space": space}
        self.queries: list[dict] = []
        self.gets: list[dict] = []

    @staticmethod
    def _matches(meta: dict, where: dict | None) -> bool:
        if not where:
            return True
        if "$and" in where:
            return all(_FakeCollection._matches(meta, clause) for clause in where["$and"])
        return all(meta.get(k) == v for k, v in where.items())

    def query(self, query_texts, n_results=10, where=None, include=None):
        self.queries.append({"query_texts": query_texts, "n_results": n_results, "where": where})
        hits = sorted(
            (c for c in self.chunks if self._matches(c["metadata"], where)),
            key=lambda c: c["distance"],
        )[:n_results]
        return {
            "ids": [[c["id"] for c in hits]],
            "documents": [[c["document"] for c in hits]],
            "metadatas": [[c["metadata"] for c in hits]],
            "distances": [[c["distance"] for c in hits]],
        }

    def get(self, where=None, **_kwargs):
        self.gets.append({"where": where})
        hits = [c for c in self.chunks if self._matches(c["metadata"], where)]
        return {"ids": [c["id"] for c in hits], "documents": [c["document"] for c in hits]}

    def delete(self, ids):
        self.chunks = [c for c in self.chunks if c["id"] not in ids]


def _chunk(cid: str, user: str, text: str, distance: float, source: str = "src-a") -> dict:
    return {
        "id": cid,
        "document": text,
        "metadata": {"user_id": user, "source_id": source},
        "distance": distance,
    }


@pytest.fixture
def collection(monkeypatch) -> _FakeCollection:
    col = _FakeCollection(
        [
            _chunk("a1", "alice", "Alice's syllabus: graph algorithms unit", 0.20),
            _chunk("a2", "alice", "Alice's notes on Dijkstra", 0.35),
        ]
    )
    client = SimpleNamespace(get_or_create_collection=lambda *_a, **_k: col)
    monkeypatch.setattr(chroma, "get_chroma_client", lambda: client)
    return col


def test_query_knowledge__user_without_documents__gets_nothing_from_other_users(collection):
    assert chroma.query_knowledge("bob", "graph algorithms") == []
    assert len(collection.queries) == 1  # no second, unfiltered query


def test_query_knowledge__owner__gets_their_close_chunks(collection):
    assert chroma.query_knowledge("alice", "graph algorithms") == [
        "Alice's syllabus: graph algorithms unit",
        "Alice's notes on Dijkstra",
    ]


@pytest.mark.parametrize("user_id", [None, ""])
def test_query_knowledge__no_user_id__raises(collection, user_id):
    with pytest.raises(ValueError):
        chroma.query_knowledge(user_id, "graph algorithms")
    assert collection.queries == []


def test_query_knowledge__match_past_the_distance_cutoff__not_returned(collection, monkeypatch):
    monkeypatch.setattr(chroma, "RAG_MAX_COSINE_DISTANCE", 0.3)

    assert chroma.query_knowledge("alice", "graph algorithms") == [
        "Alice's syllabus: graph algorithms unit"
    ]


def test_query_knowledge__weak_match__not_returned_at_the_default_cutoff(monkeypatch):
    col = _FakeCollection([_chunk("a1", "alice", "Alice's grocery list", 0.85)])
    monkeypatch.setattr(
        chroma, "get_chroma_client",
        lambda: SimpleNamespace(get_or_create_collection=lambda *_a, **_k: col),
    )

    assert chroma.query_knowledge("alice", "graph algorithms") == []


def test_query_knowledge__l2_collection__cutoff_converted_to_squared_l2(monkeypatch):
    """Chroma's l2 is squared L2: 2 x cosine distance for the normalised default embeddings."""
    col = _FakeCollection(
        [_chunk("a1", "alice", "close", 0.8), _chunk("a2", "alice", "far", 1.6)], space="l2"
    )
    monkeypatch.setattr(
        chroma, "get_chroma_client",
        lambda: SimpleNamespace(get_or_create_collection=lambda *_a, **_k: col),
    )

    assert chroma.query_knowledge("alice", "graph algorithms") == ["close"]


def test_query_knowledge__every_query__carries_the_user_filter(collection):
    chroma.query_knowledge("alice", "graphs")
    chroma.query_knowledge("bob", "graphs", n_results=3)

    assert [q["where"] for q in collection.queries] == [{"user_id": "alice"}, {"user_id": "bob"}]


def test_get_chroma_client__called_twice__builds_one_client(monkeypatch):
    built: list[object] = []

    def factory(*_args, **_kwargs):
        built.append(object())
        return built[-1]

    monkeypatch.setattr(chroma, "_client", None)
    monkeypatch.setattr(chroma, "CHROMA_API_KEY", None)
    monkeypatch.setattr(chroma.chromadb, "Client", factory)

    first, second = chroma.get_chroma_client(), chroma.get_chroma_client()

    assert first is second
    assert len(built) == 1


# --- callers ----------------------------------------------------------------------------


@pytest.fixture
def recorded_search(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def search(user_id, query, n_results=5):
        on_loop_thread = threading.current_thread() is threading.main_thread()
        calls.append({"user_id": user_id, "on_loop_thread": on_loop_thread})
        return []

    monkeypatch.setattr("app.utils.chroma_client.query_knowledge", search)
    return calls


@pytest.mark.asyncio
async def test_chat_turn__document_search__runs_off_the_event_loop(no_llm, recorded_search):
    from app.modules.conversation import run_general_chat

    await run_general_chat(
        {
            "user_message": "what is in my syllabus?",
            "user_model": SimpleNamespace(user_id="alice"),
            "memory_context": "x",
        }
    )

    assert recorded_search == [{"user_id": "alice", "on_loop_thread": False}]


@pytest.mark.asyncio
async def test_chat_turn__no_user_id__document_search_skipped(no_llm, recorded_search):
    from app.modules.conversation import run_general_chat

    await run_general_chat(
        {"user_message": "hello there", "user_model": SimpleNamespace(user_id=None), "memory_context": "x"}
    )

    assert recorded_search == []


@pytest.mark.asyncio
async def test_decompose_goal__document_search__user_scoped_off_the_loop_and_skipped_without_a_user(
    no_llm, recorded_search
):
    from app.modules.planning_graph import decompose_goal

    await decompose_goal({"planning_goal": "Learn graph algorithms", "user_id": "alice"})
    await decompose_goal({"planning_goal": "Learn graph algorithms"})

    assert recorded_search == [{"user_id": "alice", "on_loop_thread": False}]


# --- the documents API reads and deletes the caller's chunks only -----------------------


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.fakes import FakeDBClient, FakeSupabase

    col = _FakeCollection(
        [
            _chunk("a1", "alice", "Alice's chunk", 0.2, source="src-a"),
            _chunk("b1", "bob", "Bob's chunk", 0.2, source="src-b"),
        ]
    )
    monkeypatch.setattr(
        chroma, "get_chroma_client",
        lambda: SimpleNamespace(get_or_create_collection=lambda *_a, **_k: col),
    )
    db = FakeSupabase({"ingested_documents": [{"user_id": "bob", "source_id": "src-b"}]})
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(db), raising=False)
    return TestClient(app, raise_server_exceptions=False), col


def test_document_chunks__another_users_source__returns_nothing(api):
    client, _col = api

    resp = client.get("/api/v1/documents/src-b/chunks", params={"user_id": "alice"})

    assert resp.status_code == 200
    assert resp.json()["chunks"] == []


def test_delete_document__another_users_source__their_chunks_survive(api):
    client, col = api

    resp = client.delete("/api/v1/documents/src-b", params={"user_id": "alice"})

    assert resp.status_code == 404
    assert [c["id"] for c in col.chunks] == ["a1", "b1"]


@pytest.mark.asyncio
async def test_workspace_chunks__no_linked_materials__falls_back_to_the_users_own_search(
    collection, monkeypatch
):
    import app.services.analytical.workspace_builder as wb

    monkeypatch.setattr(wb, "_get_supabase", lambda: None)  # no task_materials links

    assert await wb.fetch_chunks_for_task("bob", "t1", "graph algorithms") == []
    assert await wb.fetch_chunks_for_task("alice", "t1", "graph algorithms") == [
        "Alice's syllabus: graph algorithms unit",
        "Alice's notes on Dijkstra",
    ]
    assert [q["where"] for q in collection.queries] == [{"user_id": "bob"}, {"user_id": "alice"}]
