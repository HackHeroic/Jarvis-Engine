"""Documents and tasks find each other by meaning, both ways.

``DocumentLinker`` over the in-memory repositories: a new document is linked to the
user's open tasks it is material for, and an accepted plan's tasks to the documents they
fit (``DocumentLinkListener`` on ``DraftAccepted``, after the commit, never failing it).

The matching is checked twice: with ``BagOfWordsEmbedder`` (deterministic, shared words
point the same way) for the mechanics, and with the real sentence model
(all-MiniLM-L6-v2, local, no network once downloaded; skipped when it is not on this
machine) for the threshold, on the cases it was calibrated against
(``DOCUMENT_LINK_SIMILARITY`` in ``app/core/config.py``).
"""

import logging

import pytest

from app.core.config import DOCUMENT_LINK_SIMILARITY
from app.domain.entities import IngestedDocument, TaskStatus
from app.domain.errors import EmbeddingUnavailableError
from app.domain.events import DraftAccepted, EventBus
from app.domain.ports import TextEmbedder
from app.infrastructure.persistence.factory import RepositoryFactory
from app.services.documents.linker import (
    DocumentLinker,
    DocumentLinkListener,
    document_phrases,
    file_subject,
)
from app.services.draft_service import DraftService
from tests.fakes import BagOfWordsEmbedder

U = "user-1"
ML_TOPICS = [
    "Linear regression", "Gradient descent", "Neural networks", "Support vector machines",
    "Logistic regression", "Decision trees", "K-means clustering", "Bias-variance tradeoff",
    "Regularization", "Backpropagation",
]


def _document(source_id: str, topics: list[str], file_name: str | None = None) -> IngestedDocument:
    return IngestedDocument(
        user_id=U, source_id=source_id, document_topics=topics, file_name=file_name
    )


async def _hold(repos, titles: dict[str, str], status: str = "pending") -> None:
    """The user holds these tasks (task_id -> title), written as an accept writes them."""
    draft = await repos.drafts.create(U, tasks=[], horizon_start=None, goal_id=None, schedule=None)
    await repos.drafts.accept_atomic(U, draft.id, [
        {"task_id": task_id, "title": title, "status": status, "duration_minutes": 25,
         "difficulty_weight": 0.5}
        for task_id, title in titles.items()
    ])


class _Recorder:
    def __init__(self) -> None:
        self.changed: list[tuple[str, list[str]]] = []

    async def __call__(self, user_id: str, task_ids: list[str]) -> None:
        self.changed.append((user_id, task_ids))


class _NoEmbedder(TextEmbedder):
    """An embedder that must not be reached (or that cannot run)."""

    dimensions = 384

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingUnavailableError("Text embeddings are unavailable.")


def _linker(repos, embedder: TextEmbedder | None = None, recorder=None, threshold=0.5):
    return DocumentLinker(
        repos.documents, repos.tasks, embedder or BagOfWordsEmbedder(),
        threshold=threshold, on_links_changed=recorder,
    )


# --- what a document is about -------------------------------------------------------------


@pytest.mark.parametrize(("file_name", "subject"), [
    ("DBMS unit 3 notes.pdf", "DBMS"),
    ("Machine Learning syllabus.pdf", "Machine Learning"),
    ("transcript_sem5.pdf", "transcript"),
    ("ML_syllabus_2026_v2.pdf", "ML"),
    ("IMG_2034.jpg", ""),
    ("C:\\scans\\Operating Systems - lecture 4.pdf", "Operating Systems"),
    (None, ""),
])
def test_file_subject__kind_words_and_numbers_dropped(file_name, subject):
    assert file_subject(file_name) == subject


def test_document_phrases__each_topic_then_the_file_subject_once_each():
    document = _document("d", ["Gradient descent", "gradient descent", " ", "SQL"], "SQL notes.pdf")

    assert document_phrases(document) == ["Gradient descent", "SQL"]


# --- a new document, against the user's open tasks ----------------------------------------


@pytest.mark.asyncio
async def test_link_document__one_topic_of_ten_matches__linked_where_the_blend_would_not():
    """Ten topics joined into one string share two of twenty words with the title; the
    one topic that is the title is what links them."""
    repos = RepositoryFactory.in_memory()
    await _hold(repos, {"t-gd": "gradient descent", "t-mom": "call mom"})
    document = _document("ml", [t.lower() for t in ML_TOPICS])
    blend, title = BagOfWordsEmbedder().embed([" ".join(document.document_topics), "gradient descent"])
    from app.utils.embedding import cosine_similarity

    linked = await _linker(repos).link_document(U, document)

    assert cosine_similarity(blend, title) < 0.5  # the old way: no link
    assert linked == ["t-gd"]
    assert [l.task_id for l in await repos.documents.links_for_documents(U, ["ml"])] == ["t-gd"]


@pytest.mark.asyncio
async def test_link_document__the_file_name_subject_alone__links_its_tasks():
    repos = RepositoryFactory.in_memory()
    await _hold(repos, {"t-db": "dbms quiz", "t-gym": "gym session"})

    linked = await _linker(repos).link_document(U, _document("db", [], "DBMS unit 3 notes.pdf"))

    assert linked == ["t-db"]


@pytest.mark.asyncio
async def test_link_document__finished_and_set_aside_tasks__left_out():
    """A set-aside task is linked if a plan brings it back: its accept offers it."""
    repos = RepositoryFactory.in_memory()
    await _hold(repos, {"t-done": "gradient descent"}, status="completed")
    await _hold(repos, {"t-aside": "gradient descent recap"}, status="missed")
    await _hold(repos, {"t-new": "gradient descent drills"})

    linked = await _linker(repos).link_document(U, _document("ml", ["gradient descent"]))

    assert linked == ["t-new"]


@pytest.mark.asyncio
async def test_link_document__linked__the_tasks_cached_workspaces_dropped():
    repos = RepositoryFactory.in_memory()
    recorder = _Recorder()
    await _hold(repos, {"t1": "graphs", "t2": "graphs revision", "t3": "essay"})

    await _linker(repos, recorder=recorder).link_document(U, _document("d", ["graphs"]))

    assert recorder.changed == [(U, ["t1", "t2"])] or recorder.changed == [(U, ["t2", "t1"])]


@pytest.mark.asyncio
async def test_link_tasks__user_has_no_documents__nothing_embedded():
    repos = RepositoryFactory.in_memory()
    await _hold(repos, {"t1": "graphs"})
    embedder = _NoEmbedder()

    assert await _linker(repos, embedder).link_tasks(U, ["t1"]) == []
    assert embedder.calls == 0


# --- an accepted plan's tasks, against the user's documents --------------------------------


def _accepting(repos, linker: DocumentLinker) -> DraftService:
    bus = EventBus()
    bus.subscribe(DraftAccepted, DocumentLinkListener(linker))
    return DraftService(repos.drafts, bus, task_events=repos.task_events, tasks=repos.tasks)


async def _plan(repos, titles: dict[str, str]):
    return await repos.drafts.create(
        U, tasks=[{"task_id": t, "title": title, "duration_minutes": 25} for t, title in titles.items()],
        horizon_start=None, goal_id="g", schedule=None,
    )


@pytest.mark.asyncio
async def test_accept__tasks_created_after_the_upload__linked_to_it():
    repos = RepositoryFactory.in_memory()
    recorder = _Recorder()
    await repos.documents.save(U, _document("ml", ["gradient descent", "neural networks"]))
    draft = await _plan(repos, {"g_t1": "gradient descent drills", "g_t2": "call mom"})

    written = await _accepting(repos, _linker(repos, recorder=recorder)).accept(U, draft.id)

    assert written == 2
    assert [l.task_id for l in await repos.documents.links_for_documents(U, ["ml"])] == ["g_t1"]
    assert recorder.changed == [(U, ["g_t1"])]


@pytest.mark.asyncio
async def test_accept__linking_fails__accept_stands_and_a_warning_names_the_error(caplog):
    repos = RepositoryFactory.in_memory()
    await repos.documents.save(U, _document("ml", ["gradient descent"]))
    draft = await _plan(repos, {"g_t1": "gradient descent drills"})

    with caplog.at_level(logging.WARNING):
        written = await _accepting(repos, _linker(repos, _NoEmbedder())).accept(U, draft.id)

    assert written == 1
    assert (await repos.tasks.get(U, "g_t1")).status is TaskStatus.PENDING
    assert "could not be linked to documents (EmbeddingUnavailableError)" in caplog.text
    assert "Event listener" not in caplog.text  # handled here: no traceback from the bus


@pytest.mark.asyncio
async def test_container__accept_through_its_draft_service__links_to_the_users_documents():
    from tests.fakes import build_test_container

    container = build_test_container()  # in-memory repositories, BagOfWordsEmbedder
    await container.repos.documents.save(U, _document("ml", ["gradient descent"]))
    draft = await _plan(container.repos, {"g_t1": "gradient descent drills"})

    await container.draft_service.accept(U, draft.id)

    links = await container.repos.documents.links_for_task(U, "g_t1")
    assert [l.source_id for l in links] == ["ml"]
    assert container.document_linker is not None


# --- the threshold, on the real sentence model --------------------------------------------


@pytest.fixture(scope="module")
def sentence_model():
    from app.infrastructure.embedding import MiniLMEmbedder

    embedder = MiniLMEmbedder()
    try:
        embedder.embed(["warm up"])
    except EmbeddingUnavailableError:
        pytest.skip("the local sentence model is not downloaded")
    return embedder


PLATE = {
    "t-gd": "Revise gradient descent for ML quiz",
    "t-nn": "Practice neural network problems",
    "t-mom": "Call mom",
    "t-gym": "Gym session",
    "t-dbms": "DBMS normalization assignment",
    "t-dsa": "DSA practice: graphs",
    "t-readme": "Course project: write the README",
    "t-os": "OS assignment: page replacement simulator",
}


async def _linked_on_the_real_model(sentence_model, document: IngestedDocument) -> set[str]:
    repos = RepositoryFactory.in_memory()
    await _hold(repos, PLATE)
    linker = _linker(repos, sentence_model, threshold=DOCUMENT_LINK_SIMILARITY)
    return set(await linker.link_document(U, document))


@pytest.mark.asyncio
async def test_real_model__ml_syllabus__linked_to_the_ml_tasks_only(sentence_model):
    syllabus = _document("ml", ML_TOPICS, "Machine Learning syllabus.pdf")

    assert await _linked_on_the_real_model(sentence_model, syllabus) == {"t-gd", "t-nn"}


@pytest.mark.asyncio
async def test_real_model__academic_transcript__linked_to_nothing(sentence_model):
    transcript = _document(
        "tr", ["Academic transcript", "Grades", "GPA", "Semester results", "Course credits"],
        "Academic transcript.pdf",
    )

    assert await _linked_on_the_real_model(sentence_model, transcript) == set()


@pytest.mark.asyncio
async def test_real_model__dbms_notes__linked_to_the_dbms_task_only(sentence_model):
    notes = _document(
        "db", ["Database normalization", "Functional dependencies", "SQL joins",
               "Transactions", "Indexing"],
        "DBMS unit 3 notes.pdf",
    )

    assert await _linked_on_the_real_model(sentence_model, notes) == {"t-dbms"}


@pytest.mark.asyncio
async def test_real_model__plan_accepted_after_the_upload__its_ml_task_linked(sentence_model):
    repos = RepositoryFactory.in_memory()
    await repos.documents.save(U, _document("ml", ML_TOPICS, "Machine Learning syllabus.pdf"))
    await repos.documents.save(U, _document(
        "tr", ["Academic transcript", "Grades", "GPA"], "Academic transcript.pdf"
    ))
    draft = await _plan(repos, {"g_t1": "Revise gradient descent for ML quiz", "g_t2": "Gym session"})
    linker = _linker(repos, sentence_model, threshold=DOCUMENT_LINK_SIMILARITY)

    await _accepting(repos, linker).accept(U, draft.id)

    assert [l.source_id for l in await repos.documents.links_for_task(U, "g_t1")] == ["ml"]
    assert await repos.documents.links_for_task(U, "g_t2") == []
