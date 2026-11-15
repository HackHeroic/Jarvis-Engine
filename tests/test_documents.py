"""The user's documents: the uploaded file is kept, listed, opened and deleted.

``DocumentService`` over both repository families, the documents routes over the real
app and a fake database (``FakeSupabase``, with ``FakeStorage`` for the files), and the
ingestion pipeline keeping an upload's file.
"""

import logging

import httpx
import pytest
import pytest_asyncio

from app.domain import errors
from app.domain.entities import IngestedDocument, StoredFile, TaskMaterialLink
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.documents.service import DocumentService
from app.services.documents.uploads import ascii_name, content_type_for, download_name
from tests.fakes import FakeDBClient, FakeSupabase, build_test_container

U, OTHER = "user-1", "user-2"
PDF = StoredFile(content=b"%PDF-1.4 the syllabus", content_type="application/pdf")


def _doc(source_id: str, user_id: str = U, **fields) -> IngestedDocument:
    return IngestedDocument(user_id=user_id, source_id=source_id, **fields)


async def _accepted(repos, user_id: str, tasks: list[tuple[str, str]]) -> None:
    """The user holds these (task_id, title) tasks, written as an accepted draft writes them."""
    draft = await repos.drafts.create(user_id, tasks=[], horizon_start=None, goal_id=None, schedule=None)
    await repos.drafts.accept_atomic(user_id, draft.id, [
        {"task_id": task_id, "title": title, "status": "pending", "duration_minutes": 25,
         "difficulty_weight": 0.5}
        for task_id, title in tasks
    ])


class _KnowledgeStore:
    """The chunk collection, as the documents code calls it: get by filter, delete by id."""

    def __init__(self) -> None:
        self.chunks = [
            {"id": "c1", "user_id": U, "source_id": "doc-1"},
            {"id": "c2", "user_id": OTHER, "source_id": "doc-1"},
        ]

    def get_or_create_collection(self, *_args, **_kwargs):
        return self

    def get(self, where):
        wanted = {k: v for clause in where["$and"] for k, v in clause.items()}
        return {"ids": [c["id"] for c in self.chunks
                        if all(c[k] == v for k, v in wanted.items())]}

    def delete(self, ids):
        self.chunks = [c for c in self.chunks if c["id"] not in ids]


@pytest.fixture(autouse=True)
def knowledge(monkeypatch) -> _KnowledgeStore:
    """No Chroma in these tests: deleting a document removes chunks from this one."""
    store = _KnowledgeStore()
    monkeypatch.setattr("app.utils.chroma_client.get_chroma_client", lambda: store)
    return store


@pytest.fixture(params=["in_memory", "supabase_fake"])
def repos(request):
    if request.param == "in_memory":
        return RepositoryFactory.in_memory()
    return RepositoryFactory.supabase(FakeSupabase())


class _Recorder:
    """Stands in for the workspace cache: records the tasks whose documents changed."""

    def __init__(self) -> None:
        self.changed: list[tuple[str, list[str]]] = []

    async def __call__(self, user_id: str, task_ids: list[str]) -> None:
        self.changed.append((user_id, task_ids))


def _service(repos, recorder: _Recorder | None = None) -> DocumentService:
    return DocumentService(
        repos.documents, repos.document_files, repos.tasks, on_links_changed=recorder
    )


class _FailingFiles:
    """A file store that cannot be reached."""

    def __getattr__(self, _name):
        async def fail(*_args, **_kwargs):
            raise errors.PersistenceError("Could not reach the document store.")

        return fail


# --- keeping an upload --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add__with_a_file__record_and_file_kept(repos):
    service = _service(repos)

    linked = await service.add(U, _doc("ingestion_aaa", file_name="ML.pdf"), file=PDF)

    assert linked == []  # no linker, no links
    assert await repos.document_files.read(U, "ingestion_aaa") == PDF
    assert (await repos.documents.get(U, "ingestion_aaa")).file_name == "ML.pdf"


@pytest.mark.asyncio
async def test_add__file_store_fails__record_kept_warning_logged_never_raises(repos, caplog):
    service = DocumentService(repos.documents, _FailingFiles(), repos.tasks)

    with caplog.at_level(logging.WARNING):
        await service.add(U, _doc("ingestion_aaa"), file=PDF)

    assert await repos.documents.get(U, "ingestion_aaa") is not None
    assert "kept without its file" in caplog.text and "PersistenceError" in caplog.text


@pytest.mark.asyncio
async def test_add__record_store_fails__no_file_kept_no_link(caplog):
    repos = RepositoryFactory.in_memory()

    class _NoRecords:
        async def save(self, *_args):
            raise errors.PersistenceError("Could not reach the database.")

    service = DocumentService(_NoRecords(), repos.document_files, repos.tasks)

    with caplog.at_level(logging.WARNING):
        assert await service.add(U, _doc("ingestion_aaa"), file=PDF) == []

    assert await repos.document_files.stored_ids(U) == frozenset()
    assert "record could not be stored" in caplog.text


# --- listing ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list__has_file_and_linked_tasks_with_titles__a_gone_task_left_out(repos):
    service = _service(repos)
    await _accepted(repos, U, [("t-gd", "Revise gradient descent"), ("t-nn", "Practice neural nets")])
    await service.add(U, _doc("with-file", file_name="ML.pdf"), file=PDF)
    await service.add(U, _doc("pasted"))
    await repos.documents.link(U, [
        TaskMaterialLink(user_id=U, task_id="t-gd", source_id="with-file"),
        TaskMaterialLink(user_id=U, task_id="t-nn", source_id="with-file"),
        TaskMaterialLink(user_id=U, task_id="t-replanned-away", source_id="with-file"),
    ])

    listed = {d.source_id: d for d in await service.list_documents(U)}

    assert listed["with-file"].has_file is True
    assert listed["pasted"].has_file is False
    assert sorted((t.task_id, t.title) for t in listed["with-file"].linked_tasks) == [
        ("t-gd", "Revise gradient descent"), ("t-nn", "Practice neural nets"),
    ]
    assert sorted(listed["with-file"].linked_task_ids) == ["t-gd", "t-nn"]
    assert listed["pasted"].linked_tasks == [] and listed["pasted"].linked_task_ids == []
    assert await service.list_documents(OTHER) == []


@pytest.mark.asyncio
async def test_list__many_documents__one_listing_of_the_users_files():
    client = FakeSupabase()
    repos = RepositoryFactory.supabase(client)
    service = _service(repos)
    for n in range(6):
        await service.add(U, _doc(f"doc-{n}"), file=PDF if n % 2 else None)
    client.storage.listings = 0

    listed = await service.list_documents(U)

    assert client.storage.listings == 1
    assert sorted(d.source_id for d in listed if d.has_file) == ["doc-1", "doc-3", "doc-5"]


@pytest.mark.asyncio
async def test_list__file_store_down__documents_listed_with_has_file_false(caplog):
    client = FakeSupabase()
    repos = RepositoryFactory.supabase(client)
    service = _service(repos)
    await service.add(U, _doc("doc-1"), file=PDF)
    client.storage.down = True

    with caplog.at_level(logging.WARNING):
        listed = await service.list_documents(U)

    assert [(d.source_id, d.has_file) for d in listed] == [("doc-1", False)]
    assert "could not list" in caplog.text


# --- opening the file ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_file__kept__bytes_type_and_a_safe_name(repos):
    service = _service(repos)
    await service.add(U, _doc("doc-1", file_name='../../etc/"ML"\r\nsyllabus.pdf'), file=PDF)

    kept = await service.open_file(U, "doc-1")

    assert kept.content == PDF.content and kept.content_type == "application/pdf"
    assert kept.file_name == "MLsyllabus.pdf"


@pytest.mark.asyncio
async def test_open_file__no_such_document_or_another_users__not_found(repos):
    service = _service(repos)
    await service.add(OTHER, _doc("doc-1", user_id=OTHER), file=PDF)

    with pytest.raises(errors.NotFoundError) as info:
        await service.open_file(U, "doc-1")

    assert info.value.code == "not_found" and info.value.message == "Document not found."


@pytest.mark.asyncio
async def test_open_file__document_without_a_kept_file__not_found_says_so(repos):
    service = _service(repos)
    await service.add(U, _doc("pasted"))

    with pytest.raises(errors.NotFoundError) as info:
        await service.open_file(U, "pasted")

    assert info.value.code == "not_found"
    assert "file was not kept" in info.value.message


@pytest.mark.asyncio
async def test_open_file__file_store_down__persistence_error_chained():
    client = FakeSupabase()
    repos = RepositoryFactory.supabase(client)
    service = _service(repos)
    await service.add(U, _doc("doc-1"), file=PDF)
    client.storage.down = True

    with pytest.raises(errors.PersistenceError) as info:
        await service.open_file(U, "doc-1")

    assert info.value.http_status == 503 and info.value.code == "persistence_unavailable"
    assert "storage.internal" not in info.value.message


# --- deleting -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete__chunks_file_and_record_removed_linked_workspaces_dropped(repos, knowledge):
    recorder = _Recorder()
    service = _service(repos, recorder)
    await service.add(U, _doc("doc-1"), file=PDF)
    await repos.documents.link(U, [TaskMaterialLink(user_id=U, task_id="t1", source_id="doc-1")])

    await service.delete(U, "doc-1")

    assert await repos.documents.get(U, "doc-1") is None
    assert await repos.document_files.read(U, "doc-1") is None
    assert [c["id"] for c in knowledge.chunks] == ["c2"]  # the other user's chunk stays
    assert recorder.changed == [(U, ["t1"])]


@pytest.mark.asyncio
async def test_delete__file_store_down__document_still_deleted(caplog):
    client = FakeSupabase()
    repos = RepositoryFactory.supabase(client)
    service = _service(repos)
    await service.add(U, _doc("doc-1"), file=PDF)
    client.storage.down = True

    with caplog.at_level(logging.WARNING):
        await service.delete(U, "doc-1")

    assert await repos.documents.get(U, "doc-1") is None
    assert "file could not be removed" in caplog.text


@pytest.mark.asyncio
async def test_delete__another_users_document__not_found_and_their_file_kept(repos):
    service = _service(repos)
    await service.add(OTHER, _doc("doc-1", user_id=OTHER), file=PDF)

    with pytest.raises(errors.NotFoundError):
        await service.delete(U, "doc-1")

    assert await repos.document_files.read(OTHER, "doc-1") == PDF


# --- the routes ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(monkeypatch):
    """The app over one fake database: the container's repositories and its files."""
    from app.main import app

    db = FakeSupabase()
    container = build_test_container(db)
    monkeypatch.setattr(app.state, "container", container, raising=False)
    monkeypatch.setattr(app.state, "db_client", FakeDBClient(db), raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, container, db


@pytest.mark.asyncio
async def test_list_route__documents_with_has_file_and_linked_tasks(api):
    client, container, _db = api
    await _accepted(container.repos, U, [("t1", "Revise gradient descent")])
    await container.documents.add(U, _doc("doc-1", file_name="ML.pdf", media_type="pdf"), file=PDF)
    await container.repos.documents.link(U, [TaskMaterialLink(user_id=U, task_id="t1", source_id="doc-1")])

    response = await client.get("/api/v1/documents/", params={"user_id": U})

    assert response.status_code == 200
    (document,) = response.json()["documents"]
    assert document["source_id"] == "doc-1" and document["file_name"] == "ML.pdf"
    assert document["has_file"] is True
    assert document["linked_tasks"] == [{"task_id": "t1", "title": "Revise gradient descent"}]
    assert document["linked_task_ids"] == ["t1"]


@pytest.mark.asyncio
async def test_file_route__kept__raw_bytes_inline_with_its_type(api):
    client, container, _db = api
    await container.documents.add(U, _doc("doc-1", file_name="ML syllabus.pdf"), file=PDF)

    response = await client.get("/api/v1/documents/doc-1/file", params={"user_id": U})

    assert response.status_code == 200
    assert response.content == PDF.content
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'inline; filename="ML syllabus.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_file_route__non_ascii_name__ascii_filename_plus_utf8_filename_star(api):
    client, container, _db = api
    png = StoredFile(content=b"\x89PNG\r\n\x1a\n...", content_type="image/png")
    await container.documents.add(U, _doc("doc-1", file_name="Résumé"), file=png)

    response = await client.get("/api/v1/documents/doc-1/file", params={"user_id": U})

    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == (
        "inline; filename=\"Resume.png\"; filename*=UTF-8''R%C3%A9sum%C3%A9.png"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("source_id", "owner"), [("doc-1", OTHER), ("pasted", U), ("absent", U)])
async def test_file_route__not_the_callers_or_no_file__404_not_found_envelope(api, source_id, owner):
    client, container, _db = api
    await container.documents.add(OTHER, _doc("doc-1", user_id=OTHER), file=PDF)
    await container.documents.add(U, _doc("pasted"))

    response = await client.get(f"/api/v1/documents/{source_id}/file", params={"user_id": U})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_file_route__file_store_down__503_envelope_no_driver_text(api):
    client, container, db = api
    await container.documents.add(U, _doc("doc-1"), file=PDF)
    db.storage.down = True

    response = await client.get("/api/v1/documents/doc-1/file", params={"user_id": U})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "persistence_unavailable"
    assert "storage.internal" not in response.text


@pytest.mark.asyncio
async def test_file_route__no_database__503_persistence_unavailable(api, monkeypatch):
    from app.main import app

    client, _container, _db = api
    monkeypatch.setattr(app.state, "db_client", None, raising=False)

    response = await client.get("/api/v1/documents/doc-1/file", params={"user_id": U})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "persistence_unavailable"


@pytest.mark.asyncio
async def test_delete_route__removes_the_kept_file(api):
    client, container, db = api
    await container.documents.add(U, _doc("doc-1"), file=PDF)

    response = await client.delete("/api/v1/documents/doc-1", params={"user_id": U})

    assert response.status_code == 200
    assert db.storage.objects["jarvis-documents"] == {}


@pytest.mark.asyncio
async def test_delete__knowledge_store_down__document_still_deleted(repos, monkeypatch, caplog):
    def unreachable():
        raise ConnectionError("[SSL] could not reach chroma.internal")

    monkeypatch.setattr("app.utils.chroma_client.get_chroma_client", unreachable)
    service = _service(repos)
    await service.add(U, _doc("doc-1"), file=PDF)

    with caplog.at_level(logging.WARNING):
        await service.delete(U, "doc-1")

    assert await repos.documents.get(U, "doc-1") is None
    assert "chunks could not be removed (ConnectionError)" in caplog.text


# --- ingestion keeps the uploaded file ----------------------------------------------------


async def _ingest_a_pdf(monkeypatch, container, file_name="ML syllabus.pdf"):
    import app.services.extraction.orchestrator as orchestrator
    from app.schemas.context import IntentType
    from app.services.extraction.knowledge_ingester import KnowledgeIngestionResult

    async def extracted(*_args):
        return "Unit 1: gradient descent. Unit 2: neural networks.", None

    async def ingest(**_kwargs):
        return KnowledgeIngestionResult(
            stored_chunk_count=2, suggested_actions=[], metadata={}, action_items=[],
            document_topics=["Gradient descent", "Neural networks"], deadlines=[],
        )

    monkeypatch.setattr(orchestrator, "_extract_text", extracted)
    monkeypatch.setattr(orchestrator, "ingest_knowledge", ingest)
    return await orchestrator.process_ingestion(
        file_bytes=PDF.content, media_type="pdf", file_name=file_name, user_id=U,
        intent_override=IntentType.KNOWLEDGE_INGESTION, db_client=FakeDBClient(),
        tasks=container.repos.tasks, documents=container.documents,
    )


@pytest.mark.asyncio
async def test_process_ingestion__an_uploaded_pdf__its_file_kept_under_the_document(monkeypatch):
    db = FakeSupabase()
    container = build_test_container(db)

    await _ingest_a_pdf(monkeypatch, container)

    (document,) = await container.repos.documents.list_for_user(U)
    assert document.file_name == "ML syllabus.pdf" and document.chunk_count == 2
    held = db.storage.objects["jarvis-documents"][f"{U}/{document.source_id}"]
    assert held["content"] == PDF.content and held["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_process_ingestion__file_store_down__ingestion_still_succeeds_and_logs(
    monkeypatch, caplog
):
    db = FakeSupabase()
    db.storage.down = True
    container = build_test_container(db)

    with caplog.at_level(logging.WARNING):
        result = await _ingest_a_pdf(monkeypatch, container)

    assert result.knowledge_result["stored_chunk_count"] == 2
    assert [d.file_name for d in await container.repos.documents.list_for_user(U)] == [
        "ML syllabus.pdf"
    ]
    assert "kept without its file" in caplog.text


# --- content types and names --------------------------------------------------------------


@pytest.mark.parametrize(("media_type", "content", "expected"), [
    ("pdf", b"anything", "application/pdf"),
    ("png", b"anything", "image/png"),
    ("jpeg", b"anything", "image/jpeg"),
    ("image", b"\x89PNG\r\n\x1a\nrest", "image/png"),
    ("image", b"\xff\xd8\xff\xe0rest", "image/jpeg"),
    ("image", b"GIF89a", "application/octet-stream"),
    (None, b"%PDF-1.7", "application/pdf"),
])
def test_content_type_for__declared_kind_else_signature(media_type, content, expected):
    assert content_type_for(media_type, content) == expected


@pytest.mark.parametrize(("file_name", "content_type", "expected"), [
    ("ML syllabus.pdf", "application/pdf", "ML syllabus.pdf"),
    ("C:\\Users\\me\\notes.pdf", "application/pdf", "notes.pdf"),
    ("../../etc/passwd", "application/pdf", "passwd.pdf"),
    ('a";b\r\nSet-Cookie: x.pdf', "application/pdf", "abSet-Cookie: x.pdf"),
    (None, "image/png", "document.png"),
    ("...", "application/octet-stream", "document"),
    ("scan", "image/jpeg", "scan.jpg"),
    ("x" * 300 + ".pdf", "application/pdf", "x" * 146 + ".pdf"),
])
def test_download_name__no_folder_quotes_or_control_characters(file_name, content_type, expected):
    assert download_name(file_name, content_type) == expected


def test_ascii_name__accents_folded_the_rest_dropped():
    assert ascii_name("Résumé.pdf") == "Resume.pdf"
    assert ascii_name("日本語") == "document"


# --- the task workspace lists the task's documents ----------------------------------------


def _cached_workspace(monkeypatch, written: list | None = None):
    """The workspace builder with a cached workspace for every task, and no network."""
    import app.services.analytical.workspace_builder as wb
    from app.schemas.workspace import TaskWorkspace

    async def cached(user_id, task_id):
        return TaskWorkspace(task_id=task_id, task_title="Revise gradient descent",
                             primary_objective="Revise gradient descent")

    async def record(*args):
        if written is not None:
            written.append(args)

    monkeypatch.setattr(wb, "_read_cached_workspace", cached)
    monkeypatch.setattr(wb, "_write_cached_workspace", record)
    return wb


@pytest.mark.asyncio
async def test_workspace__linked_documents_read_fresh_after_the_cache(monkeypatch, repos):
    wb = _cached_workspace(monkeypatch)
    service = _service(repos)
    await service.add(U, _doc("with-file", file_name="ML syllabus.pdf"), file=PDF)
    await service.add(U, _doc("pasted", file_name=None))
    await service.add(U, _doc("unlinked", file_name="Other.pdf"), file=PDF)
    await repos.documents.link(U, [
        TaskMaterialLink(user_id=U, task_id="t1", source_id="with-file"),
        TaskMaterialLink(user_id=U, task_id="t1", source_id="pasted"),
    ])

    ws = await wb.get_or_build_task_workspace(U, "t1", bus=None, documents=service)

    assert sorted((d.source_id, d.file_name, d.has_file) for d in ws.linked_documents) == [
        ("pasted", None, False), ("with-file", "ML syllabus.pdf", True),
    ]
    await service.delete(U, "with-file")
    ws = await wb.get_or_build_task_workspace(U, "t1", bus=None, documents=service)
    assert [d.source_id for d in ws.linked_documents] == ["pasted"]


@pytest.mark.asyncio
async def test_workspace__links_unreadable__workspace_served_with_no_documents(monkeypatch, caplog):
    wb = _cached_workspace(monkeypatch)
    repos = RepositoryFactory.in_memory()

    class _Unreadable:
        async def links_for_task(self, *_args):
            raise errors.PersistenceError("Could not reach the database.")

    service = DocumentService(_Unreadable(), repos.document_files, repos.tasks)

    with caplog.at_level(logging.WARNING):
        ws = await wb.get_or_build_task_workspace(U, "t1", bus=None, documents=service)

    assert ws.task_title == "Revise gradient descent" and ws.linked_documents == []
    assert "could not be read" in caplog.text


@pytest.mark.asyncio
async def test_workspace_route__linked_documents_in_the_response(api, monkeypatch):
    client, container, _db = api
    _cached_workspace(monkeypatch)
    await container.documents.add(U, _doc("doc-1", file_name="ML syllabus.pdf"), file=PDF)
    await container.repos.documents.link(U, [TaskMaterialLink(user_id=U, task_id="t1", source_id="doc-1")])

    response = await client.get("/api/v1/tasks/t1/workspace", params={"user_id": U})
    other = await client.get("/api/v1/tasks/t2/workspace", params={"user_id": U})

    assert response.status_code == 200
    assert response.json()["linked_documents"] == [
        {"source_id": "doc-1", "file_name": "ML syllabus.pdf", "has_file": True}
    ]
    assert other.json()["linked_documents"] == []


@pytest.mark.asyncio
async def test_workspace__built_fresh__cached_without_its_documents(monkeypatch):
    import app.services.analytical.workspace_builder as wb
    from app.schemas.workspace import TaskWorkspace

    written: list = []
    _cached_workspace(monkeypatch, written)
    repos = RepositoryFactory.in_memory()
    service = _service(repos)
    await service.add(U, _doc("doc-1"), file=PDF)
    await repos.documents.link(U, [TaskMaterialLink(user_id=U, task_id="t1", source_id="doc-1")])

    async def built(**_kwargs):
        return TaskWorkspace(task_id="t1", task_title="t", primary_objective="t")

    monkeypatch.setattr(wb, "build_task_workspace", built)

    ws = await wb.get_or_build_task_workspace(U, "t1", refresh=True, bus=None, documents=service)

    assert [d.source_id for d in ws.linked_documents] == ["doc-1"]
    ((_user, _task, cached),) = written
    assert cached.linked_documents == []  # never cached: read fresh each time
