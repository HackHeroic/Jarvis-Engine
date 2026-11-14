"""A stored conversation summary as text: read, checked, fitted to a budget, written back.

The stored form is the model's paragraph, then one ``- `` line per open thread. Read
back, the paragraph splits into sentences; each sentence and thread is a unit that is
shown, budgeted and checked on its own.

``SummaryFilter`` vets the units before anything is shown or stored. A model wrote
them from the user's own messages, so they can repeat a fact the user later told Jarvis
to forget, or slip into blame:

* **Forgotten or dismissed facts.** Each unit is checked whole and clause by clause
  (``check_pieces``), so a fact buried in a long run-on sentence is not diluted by the
  rest of it, against the memories the user let go of
  (``MemoryService.drop_suppressed``, at ``SUMMARY_SUPPRESS_SIMILARITY``). A unit with
  any close piece is dropped whole: what is left of a sentence with a clause cut out
  could say something else. If the check cannot run, the filter returns None and the
  caller shows nothing unchecked.
* **Blame.** A unit with a phrase from ``app.domain.anti_guilt.BLAME_PHRASES``, or the
  word "missed", is dropped (no rewording: a sentence edited word by word could say
  something else).

Kept units have their angle brackets made look-alikes (``as_data``), so the text is
safe inside any prompt's tags, wherever it ends up.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.anti_guilt import blame_phrases_in
from app.domain.entities import Memory
from app.schemas.memory import SessionSummary
from app.services.memory.context import as_data, estimate_tokens
from app.services.memory.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Stored when the filter left nothing of a summary: a summary column cannot be blank
# while it says where the verbatim window starts. Read back, it is no summary at all.
NOTHING_CARRIED = "Nothing from earlier in this conversation needs carrying forward."

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# Where a sentence splits into clauses for the forget check (calibrated with this rule,
# see SUMMARY_SUPPRESS_SIMILARITY): semicolons, colons, dashes, commas and the joining
# words that string clauses into run-ons. A piece under 3 words is too short to judge.
_CLAUSE_BREAK = re.compile(
    r"\s*(?:;|:|—|,?\s+(?:and|but|so|because|since|while|which|although|though|"
    r"after|before|when|until|as)\s+|,\s+)\s*",
    re.IGNORECASE,
)
_MIN_PIECE_WORDS = 3
_MISSED = re.compile(r"(?<!\w)missed(?!\w)", re.IGNORECASE)
THREAD_CHARS = 200


def stored_summary(result: SessionSummary) -> str:
    """The summary as chat_sessions stores it: the paragraph, then a '- ' line per thread.

    Raises ValueError for a summary with no text: storing it would erase the old one.
    """
    paragraph = " ".join(result.summary.split())
    if not paragraph:
        raise ValueError("the model wrote an empty summary")
    threads = (" ".join(t.split())[:THREAD_CHARS].rstrip(" .;") for t in result.open_threads)
    return stored_text([paragraph], [thread for thread in threads if thread])


def stored_text(sentences: list[str], threads: list[str]) -> str:
    """Sentences and threads back in the stored form; NOTHING_CARRIED if both are empty."""
    lines = ([" ".join(sentences)] if sentences else []) + [f"- {t}" for t in threads]
    return "\n".join(lines) or NOTHING_CARRIED


def summary_parts(text: str) -> tuple[list[str], list[str]]:
    """A stored summary as its sentences and its open threads, in order."""
    sentences: list[str] = []
    threads: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if line == NOTHING_CARRIED:
            continue
        if line.startswith("- "):
            if line[2:].strip():
                threads.append(line[2:].strip())
        elif line:
            sentences.extend(part for part in _SENTENCE_END.split(line) if part)
    return sentences, threads


def sentence(text: str) -> str:
    """``text`` ending in a full stop, unless it already ends a sentence."""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def summary_text(sentences: list[str], threads: list[str]) -> str:
    """The summary as one line: its sentences, then 'Still open: a; b.'."""
    parts = list(sentences)
    if threads:
        parts.append(sentence("Still open: " + "; ".join(threads)))
    return " ".join(parts)


def fit(
    sentences: list[str], threads: list[str], frame: Callable[[str], str], budget: int
) -> str:
    """The summary, keeping sentences then threads in order while ``frame(text)`` fits
    ``budget`` tokens. One that does not fit is skipped, never cut. '' if none fits."""
    kept: list[str] = []
    open_threads: list[str] = []
    for unit in sentences:
        if estimate_tokens(frame(summary_text([*kept, unit], open_threads))) <= budget:
            kept.append(unit)
    for thread in threads:
        if estimate_tokens(frame(summary_text(kept, [*open_threads, thread]))) <= budget:
            open_threads.append(thread)
    return summary_text(kept, open_threads)


def check_pieces(unit: str) -> list[str]:
    """What the forget check compares: the unit itself, then each clause of 3+ words."""
    parts = [part for part in _CLAUSE_BREAK.split(unit) if part and part.strip()]
    if len(parts) <= 1:
        return [unit]
    clauses = [p.strip(" .") for p in parts if len(p.split()) >= _MIN_PIECE_WORDS]
    return [unit, *clauses]


def _blames(unit: str) -> bool:
    return bool(blame_phrases_in(unit) or _MISSED.search(unit))


@dataclass(frozen=True)
class CleanSummary:
    """What is left of a summary after the filter, and whether anything was dropped."""

    sentences: list[str]
    threads: list[str]
    dropped: bool

    def stored(self) -> str:
        """The kept units in the stored form."""
        return stored_text(self.sentences, self.threads)


class SummaryFilter:
    """Drops summary units that repeat a forgotten or dismissed fact, or blame."""

    def __init__(self, memory: MemoryService | None) -> None:
        """Without ``memory`` only blame is checked."""
        self._memory = memory

    async def clean(
        self, user_id: str, summary: str, *, let_go: list[Memory] | None = None
    ) -> CleanSummary | None:
        """``summary``'s units less any the user let go of or that blame; None if the
        forget check could not run (the caller must then show and store nothing of it).
        ``let_go``: the user's forgotten and dismissed memories, if already read."""
        sentences, threads = summary_parts(summary)
        units = [*sentences, *threads]
        kept = {unit for unit in units if not _blames(unit)}
        if self._memory is not None and kept:
            pieces = {unit: check_pieces(unit) for unit in kept}
            checked = list(dict.fromkeys(p for unit_pieces in pieces.values() for p in unit_pieces))
            try:
                survived = set(
                    await self._memory.drop_suppressed(user_id, checked, let_go=let_go)
                )
            except Exception as exc:  # fail closed: unchecked text is never shown
                logger.warning(
                    "Summary withheld: it could not be checked against forgotten memories (%s)",
                    type(exc).__name__,
                )
                return None
            kept = {unit for unit in kept if all(p in survived for p in pieces[unit])}
        kept_sentences = [as_data(s) for s in sentences if s in kept]
        kept_threads = [as_data(t) for t in threads if t in kept]
        return CleanSummary(
            sentences=kept_sentences,
            threads=kept_threads,
            dropped=len(kept_sentences) + len(kept_threads) < len(units),
        )
