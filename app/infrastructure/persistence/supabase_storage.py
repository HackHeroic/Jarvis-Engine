"""Uploaded files in Supabase Storage: the ``DocumentFileStore`` port (an Adapter).

One private bucket (``jarvis-documents`` by default) holds every user's files, each at a
key that is a pure function of its document: ``{user_id}/{source_id}``. So the database
needs no column to find a file, and a user's files are one folder, listed in one call.

The storage client (storage3, under supabase-py) is synchronous HTTP, so every call runs
on a worker thread via ``asyncio.to_thread``. The bucket is created on first use if it
is missing, private and sized for the largest upload ingestion accepts. Any failure but
"not there" surfaces as PersistenceError with the driver's error chained, never repeated.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from app.domain import errors
from app.domain.entities import StoredFile
from app.domain.ports import DocumentFileStore
from app.infrastructure.persistence._mapping import file_key_part

DOCUMENT_BUCKET = "jarvis-documents"
# One listing page: a folder of more files than this is read in several pages.
LIST_PAGE_SIZE = 1000
_UNREACHABLE = "Could not reach the document store."
_FALLBACK_CONTENT_TYPE = "application/octet-stream"

T = TypeVar("T")


def _is_missing(exc: Exception) -> bool:
    """Storage's "no such bucket / object": a 404 in the error body (storage3 keeps the
    body's ``statusCode``, a string, as ``status``)."""
    return str(getattr(exc, "status", "")) == "404"


def _is_duplicate(exc: Exception) -> bool:
    """Creating a bucket another process created first: a 409 (or 400 "Duplicate")."""
    return str(getattr(exc, "status", "")) == "409" or "Duplicate" in str(
        getattr(exc, "code", "")
    )


class SupabaseDocumentFileStore(DocumentFileStore):
    """Files in one private Supabase Storage bucket, at ``{user_id}/{source_id}``.

    ``client`` is a supabase-py ``Client`` (or a stand-in with its ``storage``), read only
    when a file is first touched. ``max_file_bytes`` is the bucket's size limit: set when
    the bucket is created, raised if an existing bucket's is lower. None leaves it to the
    project's own limit.
    """

    def __init__(
        self, client: Any, *, bucket: str = DOCUMENT_BUCKET, max_file_bytes: int | None = None
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._max_file_bytes = max_file_bytes
        self._bucket_ready = False
        self._bucket_lock = threading.Lock()  # calls arrive on several worker threads

    # --- the port ------------------------------------------------------------------

    async def save(self, user_id: str, source_id: str, file: StoredFile) -> None:
        key = self._key(user_id, source_id)
        await self._call(
            lambda files: files.upload(
                key, file.content, {"content-type": file.content_type, "upsert": "true"}
            )
        )

    async def read(self, user_id: str, source_id: str) -> StoredFile | None:
        key = self._key(user_id, source_id)

        def fetch(files: Any) -> StoredFile | None:
            try:
                info = files.info(key)
                content = files.download(key)
            except Exception as exc:
                if _is_missing(exc):
                    return None
                raise
            return StoredFile(content=content, content_type=_content_type_of(info))

        return await self._call(fetch)

    async def delete(self, user_id: str, source_id: str) -> bool:
        key = self._key(user_id, source_id)
        removed = await self._call(lambda files: files.remove([key]))
        return bool(removed)

    async def stored_ids(self, user_id: str) -> frozenset[str]:
        folder = file_key_part(user_id, "user id")

        def listing(files: Any) -> frozenset[str]:
            names: set[str] = set()
            offset = 0
            while True:
                page = files.list(folder, {"limit": LIST_PAGE_SIZE, "offset": offset})
                # A file has an id; a sub-folder (or a placeholder) has none.
                names.update(entry["name"] for entry in page if entry.get("id"))
                if len(page) < LIST_PAGE_SIZE:
                    return frozenset(names)
                offset += LIST_PAGE_SIZE

        return await self._call(listing)

    # --- plumbing ------------------------------------------------------------------

    @staticmethod
    def _key(user_id: str, source_id: str) -> str:
        return f"{file_key_part(user_id, 'user id')}/{file_key_part(source_id, 'document id')}"

    async def _call(self, operation: Callable[[Any], T]) -> T:
        """Run ``operation`` on the bucket's file API off the event loop, the bucket made
        sure of first; any failure but a missing bucket or object is PersistenceError."""

        def run() -> T:
            self._ensure_bucket()
            return operation(self._client.storage.from_(self._bucket))

        try:
            return await asyncio.to_thread(run)
        except errors.JarvisError:
            raise
        except Exception as exc:  # any transport or storage API failure
            raise errors.PersistenceError(_UNREACHABLE) from exc

    def _ensure_bucket(self) -> None:
        """Create the bucket, private and sized, if it is missing; once per process.

        An existing bucket that is public, or limited below ``max_file_bytes``, is made
        private and given the limit: a file must never be readable without the key, and
        no upload ingestion accepts may be refused by the store.
        """
        if self._bucket_ready:
            return
        with self._bucket_lock:
            if self._bucket_ready:
                return
            storage = self._client.storage
            wanted: dict[str, Any] = {"public": False}
            if self._max_file_bytes is not None:
                wanted["file_size_limit"] = self._max_file_bytes
            try:
                bucket = storage.get_bucket(self._bucket)
            except Exception as exc:
                if not _is_missing(exc):
                    raise
                try:
                    storage.create_bucket(self._bucket, options=wanted)
                except Exception as create_exc:
                    if not _is_duplicate(create_exc):
                        raise
            else:
                limit = getattr(bucket, "file_size_limit", None)
                too_small = (
                    self._max_file_bytes is not None
                    and limit is not None
                    and limit < self._max_file_bytes
                )
                if getattr(bucket, "public", False) or too_small:
                    storage.update_bucket(self._bucket, wanted)
            self._bucket_ready = True


def _content_type_of(info: Any) -> str:
    """The content type an object was stored with, from its info (``content_type``, or
    the ``mimetype`` of its metadata); octet-stream when it names none."""
    if not isinstance(info, dict):
        return _FALLBACK_CONTENT_TYPE
    metadata = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    found = info.get("content_type") or info.get("contentType") or metadata.get("mimetype")
    return found if isinstance(found, str) and found else _FALLBACK_CONTENT_TYPE
