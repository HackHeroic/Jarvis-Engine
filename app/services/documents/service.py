"""DocumentService: the user's documents, kept, listed, opened and deleted.

A document is kept in three places, each through its own collaborator: its record and
task links (``DocumentRepository``), its uploaded file (``DocumentFileStore``), and its
text, as chunks in the knowledge store. This service is the one place that knows all
three, so the documents routes stay routing and the workspace asks it which documents a
task has.

What is essential fails loudly; what only adds to a document is best effort. Listing the
documents fails with PersistenceError, but whether each has a file, and the tasks it is
linked to, are left out (logged) when their store cannot answer. Keeping a new upload
never fails the ingestion that read it.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.domain import errors
from app.domain.entities import IngestedDocument, StoredFile, TaskMaterialLink
from app.domain.ports import DocumentFileStore, DocumentRepository, TaskRepository
from app.schemas.documents import DocumentOut, LinkedDocument, LinkedTask
from app.services.documents.linker import DocumentLinker, WorkspacesChanged
from app.services.documents.uploads import download_name

logger = logging.getLogger(__name__)

_LIST_FAILED = "Could not load your documents."
_DELETE_FAILED = "Could not delete the document."
_OPEN_FAILED = "Could not open the document's file."
_NOT_FOUND = "Document not found."
_NO_FILE = "This document's file was not kept, so it cannot be opened."


@dataclass(frozen=True)
class DocumentFile:
    """A kept file, ready to send: its bytes, content type and the name to offer it under."""

    content: bytes
    content_type: str
    file_name: str


class DocumentService:
    """The user's documents across their record, their links and their file."""

    def __init__(
        self,
        documents: DocumentRepository,
        files: DocumentFileStore,
        tasks: TaskRepository,
        *,
        linker: DocumentLinker | None = None,
        on_links_changed: WorkspacesChanged | None = None,
    ) -> None:
        self._documents = documents
        self._files = files
        self._tasks = tasks
        self._linker = linker
        self._on_links_changed = on_links_changed

    async def add(
        self, user_id: str, document: IngestedDocument, *, file: StoredFile | None = None
    ) -> list[str]:
        """Keep a newly ingested document: its record, its file when one was uploaded, and
        links to the user's pending tasks it is material for (``DocumentLinker``).

        Best effort, never raising: the document's chunks are already stored, so a store
        that fails here is logged (by its exception type) and the rest goes on without
        it. When the record cannot be stored nothing else is kept: a file or a link with
        no record could never be listed or opened. Returns the ids of the tasks linked.
        """
        try:
            stored = await self._documents.save(user_id, document)
        except Exception as exc:
            logger.warning(
                "Document %s was read but its record could not be stored (%s)",
                document.source_id, type(exc).__name__,
            )
            return []
        if file is not None:
            try:
                await self._files.save(user_id, stored.source_id, file)
            except Exception as exc:
                logger.warning(
                    "Document %s was kept without its file: the file store failed (%s)",
                    stored.source_id, type(exc).__name__,
                )
        if self._linker is None:
            return []
        try:
            return await self._linker.link_document(user_id, stored)
        except Exception as exc:
            logger.warning(
                "Document %s was kept but linked to no task: linking failed (%s)",
                stored.source_id, type(exc).__name__,
            )
            return []

    async def list_documents(self, user_id: str) -> list[DocumentOut]:
        """The user's documents, newest first, each with whether its file is kept and the
        tasks it is linked to (tasks the user no longer has are left out).

        One read of the records, one of their links, one of the user's tasks, and one
        listing of the user's files, however many documents there are. PersistenceError
        when the records cannot be read.
        """
        try:
            documents = await self._documents.list_for_user(user_id)
        except errors.PersistenceError as exc:
            raise errors.PersistenceError(_LIST_FAILED) from exc
        if not documents:
            return []
        links = await self._links_with_titles(user_id, [d.source_id for d in documents])
        stored = await self._stored_ids(user_id)
        return [
            DocumentOut(
                source_id=d.source_id,
                id=d.id,
                user_id=d.user_id,
                file_name=d.file_name,
                media_type=d.media_type,
                document_topics=d.document_topics,
                chunk_count=d.chunk_count,
                created_at=d.created_at.isoformat() if d.created_at else None,
                has_file=d.source_id in stored,
                linked_tasks=links.get(d.source_id, []),
                linked_task_ids=[t.task_id for t in links.get(d.source_id, [])],
            )
            for d in documents
        ]

    async def open_file(self, user_id: str, source_id: str) -> DocumentFile:
        """The document's kept file, with its content type and a safe name to offer it under.

        NotFoundError when the user has no such document, or it has no file kept (pasted
        text, or an upload whose file could not be stored); PersistenceError when the
        record or the file store cannot be reached.
        """
        try:
            document = await self._documents.get(user_id, source_id)
            if document is None:
                raise errors.NotFoundError(_NOT_FOUND, details={"source_id": source_id})
            kept = await self._files.read(user_id, source_id)
        except errors.PersistenceError as exc:
            raise errors.PersistenceError(_OPEN_FAILED) from exc
        if kept is None:
            raise errors.NotFoundError(_NO_FILE, details={"source_id": source_id})
        return DocumentFile(
            content=kept.content,
            content_type=kept.content_type,
            file_name=download_name(document.file_name, kept.content_type),
        )

    async def delete(self, user_id: str, source_id: str) -> None:
        """Delete the document everywhere it is kept: its chunks, task links, record and file.

        The record decides: NotFoundError when the user has no such document (another
        user's included, whose chunks are never touched), PersistenceError when it cannot
        be deleted. The chunks, the file and the linked tasks' cached workspaces are
        cleaned up best effort around it.
        """
        await asyncio.to_thread(_remove_chunks, user_id, source_id)
        try:
            linked = await self._documents.links_for_documents(user_id, [source_id])
        except errors.PersistenceError:
            linked = []  # only for the workspaces to refresh
        try:
            existed = await self._documents.delete(user_id, source_id)
        except errors.PersistenceError as exc:
            raise errors.PersistenceError(_DELETE_FAILED) from exc
        if not existed:
            raise errors.NotFoundError(_NOT_FOUND, details={"source_id": source_id})
        try:
            await self._files.delete(user_id, source_id)
        except Exception as exc:
            logger.warning(
                "Document %s deleted; its file could not be removed (%s)",
                source_id, type(exc).__name__,
            )
        await self._links_changed(user_id, [link.task_id for link in linked])

    async def linked_documents(self, user_id: str, task_id: str) -> list[LinkedDocument]:
        """The documents linked to the task, oldest link first, each with whether its file
        is kept. Best effort: [] when the links cannot be read (logged)."""
        try:
            links = await self._documents.links_for_task(user_id, task_id)
            if not links:
                return []
            documents = {d.source_id: d for d in await self._documents.list_for_user(user_id)}
        except Exception as exc:
            logger.warning(
                "The documents of task %s could not be read (%s)", task_id, type(exc).__name__
            )
            return []
        stored = await self._stored_ids(user_id)
        found: list[LinkedDocument] = []
        for link in sorted(links, key=_linked_order):
            document = documents.get(link.source_id)
            if document is None or any(f.source_id == link.source_id for f in found):
                continue
            found.append(LinkedDocument(
                source_id=link.source_id,
                file_name=document.file_name,
                has_file=link.source_id in stored,
            ))
        return found

    # --- helpers ---------------------------------------------------------------------

    async def _links_with_titles(
        self, user_id: str, source_ids: list[str]
    ) -> dict[str, list[LinkedTask]]:
        """Each document's linked tasks the user still has, with titles: source_id -> tasks.

        Best effort: {} when the links or the tasks cannot be read (logged).
        """
        try:
            links = await self._documents.links_for_documents(user_id, source_ids)
            if not links:
                return {}
            titles = {t.task_id: t.title for t in await self._tasks.list_for_user(user_id)}
        except Exception as exc:
            logger.warning("Document links could not be read (%s)", type(exc).__name__)
            return {}
        found: dict[str, list[LinkedTask]] = {}
        for link in sorted(links, key=_linked_order):
            title = titles.get(link.task_id)
            tasks = found.setdefault(link.source_id, [])
            if title is not None and all(t.task_id != link.task_id for t in tasks):
                tasks.append(LinkedTask(task_id=link.task_id, title=title))
        return found

    async def _stored_ids(self, user_id: str) -> frozenset[str]:
        """The user's documents with a file kept; none when the file store cannot say."""
        try:
            return await self._files.stored_ids(user_id)
        except Exception as exc:
            logger.warning("The file store could not list the user's files (%s)", type(exc).__name__)
            return frozenset()

    async def _links_changed(self, user_id: str, task_ids: list[str]) -> None:
        """Tell the workspace cache, best effort, that these tasks' documents changed."""
        if not task_ids or self._on_links_changed is None:
            return
        try:
            await self._on_links_changed(user_id, sorted(set(task_ids)))
        except Exception as exc:
            logger.warning("Cached workspaces could not be refreshed (%s)", type(exc).__name__)


def _linked_order(link: TaskMaterialLink) -> tuple:
    """Oldest link first; links with no stamp first, by id, so the order is stable."""
    return (link.linked_at is not None, link.linked_at or 0, link.source_id, link.task_id)


def _remove_chunks(user_id: str, source_id: str) -> None:
    """Delete the document's chunks from the knowledge store, best effort (a worker thread).

    Only the caller's: a source id alone would also match another user's chunks.
    """
    try:
        from app.utils import chroma_client

        collection = chroma_client.get_chroma_client().get_or_create_collection(
            chroma_client.KNOWLEDGE_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        found = collection.get(where={"$and": [{"source_id": source_id}, {"user_id": user_id}]})
        if found["ids"]:
            collection.delete(ids=found["ids"])
    except Exception as exc:
        logger.warning(
            "Document %s: its chunks could not be removed (%s)", source_id, type(exc).__name__
        )
