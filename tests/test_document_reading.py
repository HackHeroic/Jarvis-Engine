"""Reading an uploaded document never runs on, or stalls, the event loop."""

import asyncio
import threading
import time

import pytest

from app.domain.errors import DocumentReadTimeoutError
from app.utils import docling_helper


@pytest.mark.asyncio
async def test_read_document__parse_takes_a_while__the_loop_keeps_serving(monkeypatch):
    main_thread = threading.get_ident()
    seen: dict = {}

    def slow_parse(file_bytes: bytes, media_type: str) -> str:
        seen["thread"] = threading.get_ident()
        time.sleep(0.3)
        return "parsed"

    monkeypatch.setattr(docling_helper, "extract_document", slow_parse)
    ticks = 0

    async def other_requests() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    serving = asyncio.create_task(other_requests())
    try:
        text = await docling_helper.read_document(b"%PDF", "pdf")
    finally:
        serving.cancel()

    assert text == "parsed"
    assert seen["thread"] != main_thread  # parsed on a worker thread
    assert ticks >= 10  # the loop kept answering while the parse ran


@pytest.mark.asyncio
async def test_read_document__parse_never_returns__times_out_as_a_503(monkeypatch):
    release = threading.Event()

    def stuck_parse(file_bytes: bytes, media_type: str) -> list[dict]:
        release.wait(5)
        return []

    monkeypatch.setattr(docling_helper, "extract_document_with_provenance", stuck_parse)
    monkeypatch.setattr(docling_helper, "DOCUMENT_READ_TIMEOUT_S", 0.05)

    try:
        with pytest.raises(DocumentReadTimeoutError) as raised:
            await docling_helper.read_document_with_provenance(b"%PDF", "pdf")
    finally:
        release.set()

    assert raised.value.http_status == 503
    assert raised.value.code == "document_read_timeout"


@pytest.mark.asyncio
async def test_read_document__two_uploads_at_once__parsed_one_at_a_time(monkeypatch):
    running = 0
    most_at_once = 0

    def parse(file_bytes: bytes, media_type: str) -> str:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        time.sleep(0.05)
        running -= 1
        return "ok"

    monkeypatch.setattr(docling_helper, "extract_document", parse)

    await asyncio.gather(
        docling_helper.read_document(b"a", "pdf"), docling_helper.read_document(b"b", "pdf")
    )

    assert most_at_once == 1
