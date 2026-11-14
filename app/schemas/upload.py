"""An uploaded file's bounds, shared by every request that carries one.

A file arrives base64-encoded (``file_base64``) with its kind (``media_type``). Only the
kinds the document reader takes are accepted, and a file that decodes to more than
``MAX_CHAT_UPLOAD_BYTES`` is refused with a 422 before any work starts.
"""

from typing import Literal

from app.core.config import MAX_CHAT_UPLOAD_BYTES

# The kinds of file the document reader (Docling) takes.
UploadMediaType = Literal["pdf", "image", "png", "jpeg"]


def decoded_size(encoded: str) -> int:
    """The bytes a base64 string decodes to (exact for well-formed base64)."""
    padding = len(encoded) - len(encoded.rstrip("="))
    return len(encoded) * 3 // 4 - padding


def within_upload_limit(value: str | None) -> str | None:
    """``value`` itself; ValueError (a 422) when it decodes to more than the limit."""
    if value is not None and decoded_size(value) > MAX_CHAT_UPLOAD_BYTES:
        limit_mb = MAX_CHAT_UPLOAD_BYTES // (1024 * 1024)
        raise ValueError(f"The file can be at most {limit_mb} MB.")
    return value
