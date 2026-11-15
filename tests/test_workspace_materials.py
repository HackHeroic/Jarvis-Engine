"""A task's workspace shows only material that is about the task, named after its file.

The knowledge store is ``FakeKnowledgeCollection`` (fixed distance per chunk, real
``where`` filtering); document records and task links are the in-memory repositories.
"""

import pytest

import app.services.analytical.workspace_builder as wb
import app.utils.chroma_client as chroma
from app.domain.entities import IngestedDocument, TaskMaterialLink
from app.domain.events import NullEventBus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.analytical.workspace import materials
from app.services.analytical.workspace.quiz import generate_quiz
from tests.fakes import FakeKnowledgeCollection, install_scripted_model, knowledge_chunk

ALICE, BOB = "alice", "bob"
SYLLABUS = "DBMS-Syllabus-CS304.pdf"


@pytest.fixture
def repos():
    return RepositoryFactory.in_memory()


async def _keep(repos, user_id: str, source_id: str, file_name: str | None) -> None:
    await repos.documents.save(
        user_id, IngestedDocument(user_id=user_id, source_id=source_id, file_name=file_name)
    )


async def _link(repos, user_id: str, task_id: str, source_id: str) -> None:
    await repos.documents.link(
        user_id, [TaskMaterialLink(user_id=user_id, task_id=task_id, source_id=source_id)]
    )


@pytest.fixture
def offline(monkeypatch):
    """No database, no cache, a reader, and a web search that answers one link."""

    async def reader(*_args, **_kwargs):
        return "reader"

    async def one_link(*, user_prompt: str, system_prompt: str) -> str:
        return "https://example.org/guide | A guide"

    monkeypatch.setattr(wb, "_get_supabase", lambda: None)
    monkeypatch.setattr(wb, "get_learning_style", reader)
    monkeypatch.setattr(wb, "gemini_web_search", one_link)


async def _build(task_id: str, repos):
    return await wb.build_task_workspace(
        ALICE, task_id, materials=repos.documents, bus=NullEventBus()
    )


# --- which chunks are material ---------------------------------------------------------


@pytest.mark.asyncio
async def test_materials__no_linked_document_and_nothing_relevant__no_chunks_and_no_quiz(
    monkeypatch, repos, offline
):
    # Close enough for general document search (cosine distance <= 0.6), not for a workspace.
    FakeKnowledgeCollection([
        knowledge_chunk("t1", ALICE, "Semester 5 marks: DBMS 8, OS 9", 0.58, source="transcript"),
    ]).install(monkeypatch)
    await _keep(repos, ALICE, "transcript", "Marks-Transcript.pdf")
    model = install_scripted_model()  # nothing scripted: a quiz call would be recorded

    chunks = await wb.fetch_chunks_for_task(
        ALICE, "readme_task", "Course project: write the README", documents=repos.documents
    )
    ws = await _build("readme_task", repos)

    assert chunks == []
    assert chroma.query_knowledge(ALICE, "Course project: write the README") != []  # general bar
    assert [a for a in ws.surfaced_assets if a.asset_type in ("pdf_chunk", "generated_quiz")] == []
    assert "practice_generation" not in model.tasks


@pytest.mark.asyncio
async def test_materials__linked_document__excerpts_named_after_the_file_with_source_id(
    monkeypatch, repos, offline
):
    FakeKnowledgeCollection([
        knowledge_chunk("d1", ALICE, "Unit 3: normal forms, 1NF to BCNF", 0.30, source="dbms"),
        knowledge_chunk("d2", ALICE, "Functional dependencies and closures", 0.40, source="dbms"),
        # Closer, but not linked to the task: the linked document wins.
        knowledge_chunk("n1", ALICE, "Normalisation cheat sheet", 0.05, source="other"),
    ]).install(monkeypatch)
    await _keep(repos, ALICE, "dbms", SYLLABUS)
    await _keep(repos, ALICE, "other", "Other.pdf")
    await _link(repos, ALICE, "dbms_task", "dbms")
    monkeypatch.setattr(wb, "generate_practice_assets", _no_quiz)

    ws = await _build("dbms_task", repos)

    excerpts = [a for a in ws.surfaced_assets if a.asset_type == "pdf_chunk"]
    assert [(a.title, a.content_or_url, a.metadata) for a in excerpts] == [
        (f"{SYLLABUS}: excerpt 1", "Unit 3: normal forms, 1NF to BCNF",
         {"source_id": "dbms", "file_name": SYLLABUS}),
        (f"{SYLLABUS}: excerpt 2", "Functional dependencies and closures",
         {"source_id": "dbms", "file_name": SYLLABUS}),
    ]


async def _no_quiz(*_args, **_kwargs):
    return []


@pytest.mark.asyncio
async def test_materials__linked_document__its_best_chunks_not_the_first_of_the_file(
    monkeypatch, repos
):
    collection = FakeKnowledgeCollection([
        knowledge_chunk(f"c{i}", ALICE, f"chunk {i}", distance, source="dbms")
        for i, distance in enumerate([0.9, 0.8, 0.2, 0.1, 0.3, 0.5, 0.4])
    ]).install(monkeypatch)
    await _keep(repos, ALICE, "dbms", SYLLABUS)
    await _link(repos, ALICE, "dbms_task", "dbms")

    chunks = await wb.fetch_chunks_for_task(
        ALICE, "dbms_task", "Revise DBMS normalisation", documents=repos.documents
    )

    assert [c.text for c in chunks] == ["chunk 3", "chunk 2", "chunk 4", "chunk 6", "chunk 5"]
    assert collection.queries[0]["where"] == {"$and": [{"source_id": "dbms"}, {"user_id": ALICE}]}


@pytest.mark.asyncio
async def test_materials__another_users_document__never_returned(monkeypatch, repos):
    collection = FakeKnowledgeCollection([
        knowledge_chunk("b1", BOB, "Bob's DBMS notes: 3NF", 0.05, source="bobs-notes"),
    ]).install(monkeypatch)
    await _keep(repos, BOB, "bobs-notes", "Bob DBMS.pdf")
    await _link(repos, ALICE, "dbms_task", "bobs-notes")  # a link naming Bob's document

    chunks = await wb.fetch_chunks_for_task(
        ALICE, "dbms_task", "Revise DBMS normalisation", documents=repos.documents
    )

    assert chunks == []
    assert [q["where"] for q in collection.queries] == [
        {"$and": [{"source_id": "bobs-notes"}, {"user_id": ALICE}]},  # the link: Alice's chunks only
        {"user_id": ALICE},  # then the unlinked search, Alice's only
    ]


@pytest.mark.asyncio
async def test_materials__unlinked_chunk_within_the_strict_bar__kept_with_its_file(
    monkeypatch, repos
):
    FakeKnowledgeCollection([
        knowledge_chunk("n1", ALICE, "Normal forms explained", 0.30, source="notes"),
        knowledge_chunk("n2", ALICE, "Pasted: 2NF removes partial dependencies", 0.35, source="pasted"),
    ]).install(monkeypatch)
    await _keep(repos, ALICE, "notes", "DBMS notes.pdf")
    await _keep(repos, ALICE, "pasted", None)

    chunks = await wb.fetch_chunks_for_task(
        ALICE, "dbms_task", "Revise DBMS normalisation", documents=repos.documents
    )

    assert [(c.source_id, c.file_name) for c in chunks] == [
        ("notes", "DBMS notes.pdf"), ("pasted", materials.PASTED_NOTES),
    ]


@pytest.mark.asyncio
async def test_materials__chunk_of_a_document_the_user_no_longer_has__left_out(
    monkeypatch, repos
):
    FakeKnowledgeCollection([
        knowledge_chunk("g1", ALICE, "Normal forms explained", 0.10, source="deleted-doc"),
    ]).install(monkeypatch)

    chunks = await wb.fetch_chunks_for_task(
        ALICE, "dbms_task", "Revise DBMS normalisation", documents=repos.documents
    )

    assert chunks == []  # its excerpt could not be named or opened


@pytest.mark.asyncio
async def test_materials__strict_bar__is_a_named_constant_in_the_search(monkeypatch, repos):
    FakeKnowledgeCollection([
        knowledge_chunk("n1", ALICE, "Normal forms explained", 0.30, source="notes"),
    ]).install(monkeypatch)
    await _keep(repos, ALICE, "notes", "DBMS notes.pdf")
    monkeypatch.setattr(materials, "WORKSPACE_MATERIAL_MIN_SIMILARITY", 0.8)  # distance <= 0.2

    assert await wb.fetch_chunks_for_task(
        ALICE, "dbms_task", "Revise DBMS normalisation", documents=repos.documents
    ) == []


@pytest.mark.asyncio
async def test_materials__links_unreadable__unlinked_search_still_applies_the_strict_bar(
    monkeypatch, repos
):
    from app.domain import errors

    FakeKnowledgeCollection([
        knowledge_chunk("n1", ALICE, "Normal forms explained", 0.30, source="notes"),
        knowledge_chunk("n2", ALICE, "Grocery list", 0.58, source="notes"),
    ]).install(monkeypatch)
    await _keep(repos, ALICE, "notes", "DBMS notes.pdf")

    async def unreadable(*_args):
        raise errors.PersistenceError("Could not reach the database.")

    monkeypatch.setattr(repos.documents, "links_for_task", unreadable)

    chunks = await wb.fetch_chunks_for_task(
        ALICE, "dbms_task", "Revise DBMS normalisation", documents=repos.documents
    )

    assert [c.text for c in chunks] == ["Normal forms explained"]


# --- the quiz -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiz__model_says_material_is_unrelated__no_quiz_asset():
    model = install_scripted_model(practice_generation='{"assets": []}')
    transcript = [materials.MaterialChunk("Semester 5 marks: DBMS 8", "transcript", "Marks.pdf")]

    assets = await generate_quiz(transcript, "Course project: write the README")

    assert assets == []
    assert model.tasks == ["practice_generation"]
    assert 'return {"assets": []}' in model.calls[0].prompt
