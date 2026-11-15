"""What an uploaded file is (its content type) and the name it is offered under. Pure."""

import unicodedata
from pathlib import PurePosixPath

# The content type of each kind ingestion accepts (UploadMediaType); "image" is read
# from the file's own first bytes.
_KIND_CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg"}
_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
_EXTENSIONS = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}
OCTET_STREAM = "application/octet-stream"
# The longest name a file is offered under (the upload's own name is at most 255).
_MAX_NAME_CHARS = 150
_FALLBACK_NAME = "document"


def content_type_for(media_type: str | None, content: bytes) -> str:
    """The MIME type to keep an upload under: from its declared kind, else (the "image"
    kind, or none) from its signature; octet-stream when neither says."""
    known = _KIND_CONTENT_TYPES.get((media_type or "").strip().lower())
    if known is not None:
        return known
    for signature, content_type in _SIGNATURES:
        if content.startswith(signature):
            return content_type
    return OCTET_STREAM


def download_name(file_name: str | None, content_type: str) -> str:
    """The name to offer a kept file under: the uploaded name without any folder part,
    quotes, backslashes, semicolons or control characters (each could break or forge the
    ``Content-Disposition`` header); ``document`` plus the type's extension when nothing
    is left, and the extension added when the name has none."""
    base = PurePosixPath((file_name or "").replace("\\", "/")).name
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '"\\;')
    base = base.strip().strip(".").strip()
    if not base:
        base = _FALLBACK_NAME
    extension = _EXTENSIONS.get(content_type, "")
    if extension and "." not in base:
        base += extension
    if len(base) > _MAX_NAME_CHARS:  # shortened, keeping its extension
        stem, dot, ext = base.rpartition(".")
        if dot and stem and len(ext) <= 10:
            base = f"{stem[: _MAX_NAME_CHARS - len(ext) - 1]}.{ext}"
        else:
            base = base[:_MAX_NAME_CHARS]
    return base


def ascii_name(name: str) -> str:
    """``name`` in plain ASCII, for the header's ``filename=`` (accents dropped, anything
    else not printable ASCII left out); ``document`` when nothing is left."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    folded = "".join(ch for ch in folded if 32 <= ord(ch) < 127 and ch not in '"\\;').strip()
    return folded or _FALLBACK_NAME
