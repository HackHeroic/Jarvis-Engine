"""The context note: everything about this turn that changes, in one system message.

Memories, "since we last spoke", the plate, the user's documents and the time change
from turn to turn, so none of them may sit in a system prompt: a byte changed there
and the provider's prompt cache is never read. They travel as one ``system`` entry
after the history instead (``with_context_note``), which every provider folds into the
final user turn. The layout, each section left out when it has nothing to say::

    <memory>
    About you:
    - (you said, 12 days ago) ...
    Relevant now:
    - (I noticed, 3 days ago; seen 4 times) ...
    </memory>
    <since>...</since>
    <plate>
    - up to 8 pending task titles, with due hints
    </plate>
    <documents>
    up to 3 chunks, 600 characters each
    </documents>
    <now>Mon 16 Nov 2026, 08:05 (Asia/Kolkata)</now>

The clock is always known, so the note always ends with ``<now>``.
"""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.domain.entities import Memory, MemorySource

_PLATE_LIMIT = 8
_DOCUMENT_LIMIT = 3
_DOCUMENT_CHARS = 600


class Recall(BaseModel):
    """What ``MemoryService.recall`` found for one message, within its token budgets."""

    profile: list[Memory] = Field(
        description="Standing constraints, preferences and goals, shown on every turn."
    )
    relevant: list[Memory] = Field(
        description="Memories that bear on this message, best first; none on a trivial one."
    )
    tokens: int = Field(
        ge=0, description="Estimated tokens the memory lines take in the note, never fewer."
    )

    def all(self) -> list[Memory]:
        """The profile, then the relevant memories: what the note shows, in its order."""
        return [*self.profile, *self.relevant]


def estimate_tokens(text: str) -> int:
    """A cheap token estimate: one token per 4 characters, rounded up."""
    return math.ceil(len(text) / 4)


def _age(then: datetime, now: datetime) -> str:
    days = max(0, (now - then).days)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


def render_memory_line(m: Memory, now: datetime) -> str:
    """One memory as the note shows it: where it came from, how long ago, then the words.

    "(you said, 12 days ago)" for the user's own words; "(I noticed, 3 days ago; seen
    4 times)" for what Jarvis inferred, with the count once it was seen more than once.
    The age runs from when it was last said, observed or confirmed. Whitespace is
    collapsed, so a memory is always exactly one line.
    """
    age = _age(m.last_confirmed_at or m.created_at, now)
    if m.provenance is MemorySource.SAID:
        source = f"you said, {age}"
    elif m.observation_count > 1:
        source = f"I noticed, {age}; seen {m.observation_count} times"
    else:
        source = f"I noticed, {age}"
    return f"- ({source}) {' '.join(m.content.split())}"


def _memory_section(recall: Recall, now: datetime) -> str | None:
    if not recall.profile and not recall.relevant:
        return None
    lines = ["<memory>"]
    if recall.profile:
        lines += ["About you:", *(render_memory_line(m, now) for m in recall.profile)]
    if recall.relevant:
        lines += ["Relevant now:", *(render_memory_line(m, now) for m in recall.relevant)]
    lines.append("</memory>")
    return "\n".join(lines)


def _plate_section(plate: list[str] | None) -> str | None:
    items = [" ".join(item.split()) for item in plate or [] if item and item.strip()]
    if not items:
        return None
    return "<plate>\n" + "\n".join(f"- {item}" for item in items[:_PLATE_LIMIT]) + "\n</plate>"


def _documents_section(documents: list[str] | None) -> str | None:
    chunks = [doc.strip()[:_DOCUMENT_CHARS] for doc in documents or [] if doc and doc.strip()]
    if not chunks:
        return None
    return "<documents>\n" + "\n---\n".join(chunks[:_DOCUMENT_LIMIT]) + "\n</documents>"


def _now_section(now: datetime, zone: ZoneInfo) -> str:
    local = now.astimezone(zone)
    return f"<now>{local:%a} {local.day} {local:%b %Y, %H:%M} ({zone.key})</now>"


def render_context_note(
    recall: Recall,
    *,
    now: datetime,
    zone: ZoneInfo,
    since: str | None = None,
    plate: list[str] | None = None,
    documents: list[str] | None = None,
) -> str | None:
    """The turn's context note, in the layout above; ``now`` is shown on ``zone``'s clock.

    ``plate`` holds task lines (title and due hint), of which the first 8 are shown;
    ``documents`` holds passages, of which the first 3 are shown, 600 characters each.
    Empty sections are left out. The clock is always known, so today this always
    returns a note; the result can go straight to ``with_context_note`` either way.
    """
    since_text = " ".join((since or "").split())
    sections = [
        _memory_section(recall, now),
        f"<since>{since_text}</since>" if since_text else None,
        _plate_section(plate),
        _documents_section(documents),
        _now_section(now, zone),
    ]
    return "\n".join(section for section in sections if section)


def with_documents(note: str | None, documents: list[str] | None) -> str | None:
    """``note`` with a ``<documents>`` section added where the layout puts it.

    For a note rendered before the user's documents were searched: the section goes
    just before the closing ``<now>`` line, or last if there is none. No documents:
    ``note`` unchanged.
    """
    section = _documents_section(documents)
    if section is None:
        return note
    if not note:
        return section
    head, _, last = note.rpartition("\n")
    if last.startswith("<now>"):
        return f"{head}\n{section}\n{last}" if head else f"{section}\n{last}"
    return f"{note}\n{section}"


def with_context_note(history: list[dict] | None, note: str | None) -> list[dict]:
    """A copy of ``history`` with ``note`` appended as its last entry, a system message.

    No note (None or blank): a copy of ``history`` as it is. The caller's list is never
    changed.
    """
    turns = list(history or [])
    if note and note.strip():
        turns.append({"role": "system", "content": note})
    return turns
