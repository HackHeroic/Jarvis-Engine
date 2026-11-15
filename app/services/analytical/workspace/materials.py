"""A task's material: the chunks of the user's documents that are about the task.

Linked documents first (``DocumentRepository.links_for_task``, the links
``DocumentLinker`` made): each one's chunks are ranked against the task and its best
few kept. Only when no linked document gives any does an unlinked search run, with a
stricter bar than general document search (``WORKSPACE_MATERIAL_MIN_SIMILARITY``). When
nothing passes, the task has no material, which is an honest answer.

Every chunk carries the document it came from, so an excerpt can name its file and link
to the document viewer. A chunk whose document the user no longer has (or never had a
record of) is left out: it could not be named or opened.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.core.config import WORKSPACE_MATERIAL_MIN_SIMILARITY
from app.domain.ports import DocumentRepository
from app.schemas.workspace import StudyAsset

logger = logging.getLogger(__name__)

# The chunks of one linked document kept for a task: its best, ranked against the task.
CHUNKS_PER_LINKED_DOCUMENT = 5
# How many chunks the unlinked search asks for before the relevance bar.
UNLINKED_SEARCH_RESULTS = 5
# The most chunks a task's material holds (the quiz reads them all; excerpts show fewer).
MAX_MATERIAL_CHUNKS = 8
# The excerpts a workspace shows, and how much of each.
MAX_EXCERPTS = 5
EXCERPT_MAX_CHARS = 3000
# A linked document is material by its link, so its chunks are only ranked, never cut:
# cosine distance is at most 2.
_NO_CUTOFF = 2.0
# What a document with no file name (pasted text) is called.
PASTED_NOTES = "Pasted notes"


@dataclass(frozen=True)
class MaterialChunk:
    """A chunk of one of the user's documents, with the document it came from."""

    text: str
    source_id: str
    file_name: str


def unlinked_max_cosine_distance() -> float:
    """The unlinked search's cut-off, as cosine distance (1 - similarity)."""
    return 1.0 - WORKSPACE_MATERIAL_MIN_SIMILARITY


async def fetch_chunks_for_task(
    user_id: str,
    task_id: str,
    task_title: str = "",
    topic_keywords: list[str] | None = None,
    *,
    documents: DocumentRepository | None,
) -> list[MaterialChunk]:
    """The task's material, most relevant first, at most ``MAX_MATERIAL_CHUNKS``.

    ``documents`` (the composition root's repository) gives the task's links and each
    document's name; without it there is no material. Every read is the user's own: the
    links and records by ``user_id``, the chunks by a ``user_id`` filter. Best effort:
    a store that fails is logged by its exception type and gives no material.
    """
    query = f"{task_title} {' '.join(topic_keywords or [])}".strip()
    if not user_id or not query or documents is None:
        return []
    from app.utils import chroma_client  # chromadb is heavy: loaded on first use

    try:
        links = await documents.links_for_task(user_id, task_id)
    except Exception as exc:
        logger.warning("The documents of task %s could not be read (%s)", task_id, type(exc).__name__)
        links = []
    hits = []
    for source_id in dict.fromkeys(link.source_id for link in links if link.source_id):
        hits += await asyncio.to_thread(
            chroma_client.search_knowledge, user_id, query, CHUNKS_PER_LINKED_DOCUMENT,
            max_cosine_distance=_NO_CUTOFF, source_id=source_id,
        )
    if not hits:
        hits = await asyncio.to_thread(
            chroma_client.search_knowledge, user_id, query, UNLINKED_SEARCH_RESULTS,
            max_cosine_distance=unlinked_max_cosine_distance(),
        )
    hits.sort(key=lambda hit: hit.distance)
    names = await _document_names(user_id, {h.source_id for h in hits if h.source_id}, documents)
    material = [
        MaterialChunk(text=hit.text, source_id=hit.source_id, file_name=names[hit.source_id])
        for hit in hits
        if hit.source_id in names
    ]
    return material[:MAX_MATERIAL_CHUNKS]


async def _document_names(
    user_id: str, source_ids: set[str], documents: DocumentRepository
) -> dict[str, str]:
    """source_id -> the name to show, for each of these documents the user still has."""
    names: dict[str, str] = {}
    for source_id in sorted(source_ids):
        try:
            document = await documents.get(user_id, source_id)
        except Exception as exc:
            logger.warning("Document %s could not be read (%s)", source_id, type(exc).__name__)
            continue
        if document is not None:
            names[source_id] = (document.file_name or "").strip() or PASTED_NOTES
    return names


def excerpt_assets(material: list[MaterialChunk]) -> list[StudyAsset]:
    """The first ``MAX_EXCERPTS`` chunks as excerpts, each named after its document
    ("DBMS.pdf: excerpt 2", numbered per document) and carrying its ``source_id``."""
    assets: list[StudyAsset] = []
    numbered: dict[str, int] = {}
    for chunk in material[:MAX_EXCERPTS]:
        numbered[chunk.source_id] = numbered.get(chunk.source_id, 0) + 1
        assets.append(
            StudyAsset(
                asset_type="pdf_chunk",
                title=f"{chunk.file_name}: excerpt {numbered[chunk.source_id]}",
                content_or_url=chunk.text[:EXCERPT_MAX_CHARS],
                rationale=f"From {chunk.file_name}",
                metadata={"source_id": chunk.source_id, "file_name": chunk.file_name},
            )
        )
    return assets


def sources_phrase(material: list[MaterialChunk]) -> str:
    """The documents the material came from, for a rationale: "DBMS.pdf",
    "DBMS.pdf and OS.pdf", "A.pdf, B.pdf and 2 more"."""
    names = list(dict.fromkeys(chunk.file_name for chunk in material))
    if len(names) <= 2:
        return " and ".join(names)
    if len(names) == 3:
        return f"{names[0]}, {names[1]} and {names[2]}"
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"
