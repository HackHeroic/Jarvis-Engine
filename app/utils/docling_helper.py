"""PDF and image extraction using Docling (L6). Preserves table structure for timetables."""

import tempfile
from pathlib import Path

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import PdfFormatOption


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
