"""Linking the user's documents to the tasks they are material for, both ways.

A document is linked to a task when one of its phrases (each topic read from it, and
the subject in its file name) means nearly the same as the task's title: the best
phrase-to-title cosine similarity, on the container's sentence embedder, reaches the
threshold (``DOCUMENT_LINK_SIMILARITY``). Each topic is compared on its own: ten topics
joined into one string blur into a vague average that no specific title comes close to.

Both directions run the same match: a new document against the user's open tasks
(``link_document``, when it is kept), and newly accepted tasks against the user's
documents (``link_tasks``, from ``DocumentLinkListener`` after the accept commits).
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Collection
from pathlib import PurePosixPath

from app.domain.entities import IngestedDocument, Task, TaskMaterialLink, TaskStatus
from app.domain.events import DomainEvent, DraftAccepted, EventListener
from app.domain.ports import DocumentRepository, TaskRepository, TextEmbedder
from app.utils.embedding import cosine_similarity

logger = logging.getLogger(__name__)

# Told to the workspace cache when the documents linked to some tasks changed:
# (user_id, task_ids). The composition root passes the workspace builder's invalidation.
WorkspacesChanged = Callable[[str, list[str]], Awaitable[None]]

# The tasks a new document is offered to: the ones still to do (or set aside and due a
# new slot). A finished task's workspace is not opened again.
_OPEN = frozenset({TaskStatus.PENDING, TaskStatus.MISSED})
# Words in a file name that say what kind of file it is, not what it is about.
_GENERIC_FILE_WORDS = frozenset({
    "syllabus", "notes", "note", "unit", "units", "lecture", "lectures", "slides", "slide",
    "final", "draft", "copy", "scan", "scanned", "document", "doc", "docs", "file", "pdf",
    "module", "chapter", "week", "sem", "semester", "img", "image", "screenshot", "page",
    "pages", "part", "new", "updated", "my", "the", "of", "and", "for", "at", "to", "in",
})


def file_subject(file_name: str | None) -> str:
    """The subject a file's name gives, or "": its stem without the extension, the words
    that only name the kind of file (``syllabus``, ``notes``, ``unit``) and any word with
    a digit in it (dates, versions, ``sem5``). "DBMS unit 3 notes.pdf" gives "DBMS"."""
    if not file_name:
        return ""
    stem = PurePosixPath(file_name.replace("\\", "/")).name
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    words = [w for w in re.split(r"[\s_\-.,()\[\]]+", stem) if w]
    kept = [
        w for w in words
        if w.lower() not in _GENERIC_FILE_WORDS and not any(ch.isdigit() for ch in w)
    ]
    return " ".join(kept)


def document_phrases(document: IngestedDocument) -> list[str]:
    """What the document is about, as short phrases to hold against a task title: each
    of its topics, and its file name's subject; each once, blanks left out."""
    phrases: list[str] = []
    for phrase in [*document.document_topics, file_subject(document.file_name)]:
        phrase = phrase.strip()
        if phrase and phrase.lower() not in {p.lower() for p in phrases}:
            phrases.append(phrase)
    return phrases


class DocumentLinker:
    """Decides, by meaning, which documents are material for which tasks, and links them.

    ``threshold`` is the cosine similarity a document's best phrase must reach against a
    task's title. ``on_links_changed`` is told the tasks that gained a link, so their
    cached workspaces are rebuilt with the document in them.
    """

    def __init__(
        self,
        documents: DocumentRepository,
        tasks: TaskRepository,
        embedder: TextEmbedder,
        *,
        threshold: float,
        on_links_changed: WorkspacesChanged | None = None,
    ) -> None:
        self._documents = documents
        self._tasks = tasks
        self._embedder = embedder
        self._threshold = threshold
        self._on_links_changed = on_links_changed

    async def link_document(self, user_id: str, document: IngestedDocument) -> list[str]:
        """Link a newly kept document to the user's open tasks it is material for.

        Returns the ids of the tasks linked, in the order the user's tasks are read
        (newest first). Raises what its stores and embedder raise; the caller decides
        whether that may fail it.
        """
        tasks = await self._tasks.list_by_status(user_id, _OPEN)
        return await self._link(user_id, [document], tasks)

    async def link_tasks(self, user_id: str, task_ids: Collection[str]) -> list[str]:
        """Link newly written tasks to the user's documents they are material for.

        Returns the ids of the tasks that gained a link. No task ids, or no documents:
        nothing is read beyond that, nothing is embedded.
        """
        wanted = set(task_ids)
        if not wanted:
            return []
        documents = await self._documents.list_for_user(user_id)
        if not documents:
            return []
        tasks = [t for t in await self._tasks.list_for_user(user_id) if t.task_id in wanted]
        return await self._link(user_id, documents, tasks)

    async def matches(
        self, documents: list[IngestedDocument], tasks: list[Task]
    ) -> list[tuple[IngestedDocument, Task, float]]:
        """Every (document, task, similarity) whose best phrase-to-title similarity
        reaches the threshold. One embedding call for all phrases and titles."""
        phrases = {d.source_id: document_phrases(d) for d in documents}
        titled = [t for t in tasks if t.title and t.title.strip()]
        texts = sorted({p for ps in phrases.values() for p in ps})
        titles = sorted({t.title for t in titled})
        if not texts or not titles:
            return []
        vectors = await asyncio.to_thread(self._embedder.embed, texts + titles)
        vector = dict(zip(texts + titles, vectors))
        found = []
        for document in documents:
            if not phrases[document.source_id]:
                continue
            for task in titled:
                score = max(
                    cosine_similarity(vector[p], vector[task.title])
                    for p in phrases[document.source_id]
                )
                if score >= self._threshold:
                    found.append((document, task, score))
        return found

    async def _link(
        self, user_id: str, documents: list[IngestedDocument], tasks: list[Task]
    ) -> list[str]:
        found = await self.matches(documents, tasks)
        if not found:
            return []
        await self._documents.link(user_id, [
            TaskMaterialLink(
                user_id=user_id,
                task_id=task.task_id,
                source_id=document.source_id,
                document_topics=document.document_topics,
            )
            for document, task, _score in found
        ])
        linked = list(dict.fromkeys(task.task_id for _document, task, _score in found))
        if self._on_links_changed is not None:
            try:
                await self._on_links_changed(user_id, linked)
            except Exception as exc:
                logger.warning("Cached workspaces could not be refreshed (%s)", type(exc).__name__)
        return linked


class DocumentLinkListener(EventListener):
    """After an accept commits, links the tasks it wrote to the user's documents.

    Subscribed to ``DraftAccepted``, which ``DraftService.accept`` publishes once the
    atomic write has committed: the link is made outside the transaction, and a failure
    here (a store, the embedder) is logged by its type and never reaches the accept.
    """

    def __init__(self, linker: DocumentLinker) -> None:
        self._linker = linker

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, DraftAccepted) or not event.user_id or not event.task_ids:
            return
        try:
            await self._linker.link_tasks(event.user_id, event.task_ids)
        except Exception as exc:
            logger.warning(
                "Draft %s accepted; its tasks could not be linked to documents (%s)",
                event.draft_id, type(exc).__name__,
            )
