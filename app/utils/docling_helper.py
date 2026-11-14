"""PDF and image extraction using Docling (L6). Preserves table structure for timetables."""

import asyncio
import tempfile
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.domain.errors import DocumentReadTimeoutError

T = TypeVar("T")

try:
    from docling.document_converter import DocumentConverter
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import PdfFormatOption
    DOCLING_AVAILABLE = True
except ImportError:
    DOCLING_AVAILABLE = False


def extract_document(
    bytes_data: bytes,
    media_type: str,
    *,
    do_table_structure: bool = True,
    do_ocr: bool = True,
) -> str:
    """Extract text and tables from PDF or image.

    Args:
        bytes_data: Raw document bytes.
        media_type: One of "pdf", "image", "png", "jpeg", "jpg".
        do_table_structure: Preserve table structure (critical for timetables).
        do_ocr: Enable OCR for scanned documents.

    Returns:
        Extracted text in markdown format, preserving table structure.
    """
    if not DOCLING_AVAILABLE:
        raise ImportError("docling is not installed. Install with: pip install -e '.[ingestion]'")
    media_type_lower = media_type.lower()
    if media_type_lower in ("image", "png", "jpeg", "jpg"):
        media_type_lower = "image"
    elif media_type_lower != "pdf":
        media_type_lower = "pdf"  # Default for unknown

    if media_type_lower == "pdf":
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_table_structure = do_table_structure
        pipeline_options.do_ocr = do_ocr
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
    else:
        converter = DocumentConverter()  # Default handles images

    suffix = ".pdf" if media_type_lower == "pdf" else ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        try:
            f.write(bytes_data)
            f.flush()
            result = converter.convert(f.name)
            return result.document.export_to_markdown()
        finally:
            Path(f.name).unlink(missing_ok=True)


def _item_provenance(item) -> tuple[int, list[float]]:
    """Extract page_no and bbox from item.prov. Returns (page_no, bbox) or (0, [])."""
    prov = getattr(item, "prov", None) or []
    if not prov:
        return 0, []
    p = prov[0]
    page_no = getattr(p, "page_no", 0) or 0
    bbox = []
    if hasattr(p, "bbox") and p.bbox is not None:
        b = p.bbox
        bbox = [getattr(b, "l", 0), getattr(b, "t", 0), getattr(b, "r", 0), getattr(b, "b", 0)]
    return page_no, bbox


def extract_document_with_provenance(
    bytes_data: bytes,
    media_type: str,
    *,
    do_table_structure: bool = True,
    do_ocr: bool = True,
) -> list[dict]:
    """Extract text with provenance (page_no, bbox) from PDF or image.

    Uses doc.texts + doc.tables (avoids doc.iterate_items() which would duplicate
    table content since tables and their cells are extracted separately).

    Args:
        bytes_data: Raw document bytes.
        media_type: One of "pdf", "image", "png", "jpeg", "jpg".
        do_table_structure: Preserve table structure.
        do_ocr: Enable OCR for scanned documents.

    Returns:
        List of {"text": "...", "metadata": {"page_no": int, "bbox": [l,t,r,b]}}.
    """
    media_type_lower = media_type.lower()
    if media_type_lower in ("image", "png", "jpeg", "jpg"):
        media_type_lower = "image"
    elif media_type_lower != "pdf":
        media_type_lower = "pdf"

    if media_type_lower == "pdf":
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_table_structure = do_table_structure
        pipeline_options.do_ocr = do_ocr
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
    else:
        converter = DocumentConverter()

    suffix = ".pdf" if media_type_lower == "pdf" else ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        try:
            f.write(bytes_data)
            f.flush()
            result = converter.convert(f.name)
            doc = result.document
        finally:
            Path(f.name).unlink(missing_ok=True)

    items: list[dict] = []
    tables = getattr(doc, "tables", []) or []

    # Process tables (each table once, no cell duplication)
    for item in tables:
        try:
            text = item.export_to_markdown(doc) if hasattr(item, "export_to_markdown") else ""
        except Exception:
            text = ""
        page_no, bbox = _item_provenance(item)
        items.append({
            "text": text or "",
            "metadata": {"page_no": page_no, "bbox": bbox},
        })

    # Process texts (skip those whose parent is a table - table cells)
    texts = getattr(doc, "texts", []) or []
    for item in texts:
        parent = getattr(item, "parent", None)
        if parent is not None:
            cref = getattr(parent, "cref", None) or getattr(parent, "$ref", None) or ""
            cref = str(cref)
            if cref.startswith("#/tables/"):
                continue
        text = getattr(item, "text", "") or ""
        if not text.strip():
            continue
        page_no, bbox = _item_provenance(item)
        items.append({
            "text": text,
            "metadata": {"page_no": page_no, "bbox": bbox},
        })

    return items


# --- Reading a document from async code ---------------------------------------------

# Docling parses on worker threads of its own and can take tens of seconds on a first
# use (models load). Run on the event loop it stops every other request for that time,
# and a parse that never returns stops the server for good.
DOCUMENT_READ_TIMEOUT_S = 120.0
DOCUMENT_READ_TIMED_OUT = "That document took too long to read. Try again, or a smaller file."

_read_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _read_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _read_locks.get(loop)
    if lock is None:
        lock = _read_locks[loop] = asyncio.Lock()
    return lock


async def _read_off_the_loop(read: Callable[..., T], file_bytes: bytes, media_type: str) -> T:
    """``read(file_bytes, media_type)`` on a worker thread: one document at a time (the
    reader's models are loaded once and are not built for two parses at once), and never
    longer than ``DOCUMENT_READ_TIMEOUT_S`` (``DocumentReadTimeoutError``, a 503)."""
    async with _read_lock():
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(read, file_bytes, media_type), DOCUMENT_READ_TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            raise DocumentReadTimeoutError(DOCUMENT_READ_TIMED_OUT) from exc


async def read_document(file_bytes: bytes, media_type: str) -> str:
    """``extract_document`` for async callers: off the event loop, bounded."""
    return await _read_off_the_loop(extract_document, file_bytes, media_type)


async def read_document_with_provenance(file_bytes: bytes, media_type: str) -> list[dict]:
    """``extract_document_with_provenance`` for async callers: off the event loop, bounded."""
    return await _read_off_the_loop(extract_document_with_provenance, file_bytes, media_type)
