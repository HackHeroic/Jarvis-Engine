""""Since we last spoke": where the last conversation left off, for a new one.

When a conversation opens with no messages and the user has an earlier one, a
deterministic line (no model call): how long ago, on the user's clock; the earlier
conversation's summary (or its title); and what changed since, counted from the outcome
history (each slot once, by its latest outcome), the patterns noticed and the
suggestions waiting. "Set aside", never anything that blames. The summary goes through
``SummaryFilter`` first: nothing the user let go of, nothing that blames.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import app.core.config as config
from app.domain.clock import utc_now
from app.domain.entities import ConversationSession, Memory, SuggestionStatus, TaskEventType
from app.domain.ports import ConversationRepository, SuggestionRepository, TaskEventRepository
from app.services.memory.context import as_data
from app.services.memory.memory_service import MemoryService, MemorySnapshot
from app.services.memory.summary_text import CleanSummary, SummaryFilter, fit, sentence
from app.services.planning.local_time import local_date
from app.services.task_service import as_aware, latest_outcome_per_slot

logger = logging.getLogger(__name__)

_OUTCOMES = frozenset({TaskEventType.COMPLETED, TaskEventType.SKIPPED, TaskEventType.MISSED})
_SET_ASIDE = frozenset({TaskEventType.SKIPPED, TaskEventType.MISSED})
_SUGGESTION_READ_LIMIT = 100


def gap(then: datetime, now: datetime, zone: ZoneInfo) -> str:
    """How long ago ``then`` was, in words, counting days on the user's clock."""
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 2:
        return "a minute ago"
    if minutes < 60:
        return f"{minutes} minutes ago"
    days = (local_date(now, zone) - local_date(then, zone)).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class SinceLastSpoke:
    """Writes the "since we last spoke" line for a conversation that has just opened."""

    def __init__(
        self,
        conversations: ConversationRepository,
        task_events: TaskEventRepository,
        summaries: SummaryFilter,
        *,
        clock: Callable[[], datetime] = utc_now,
        memory: MemoryService | None = None,
        suggestions: SuggestionRepository | None = None,
    ) -> None:
        self._conversations = conversations
        self._events = task_events
        self._summaries = summaries
        self._clock = clock
        self._memory = memory
        self._suggestions = suggestions

    async def line(
        self,
        user_id: str,
        session_id: str,
        *,
        zone: ZoneInfo,
        known_empty: bool = False,
        snapshot: MemorySnapshot | None = None,
    ) -> str | None:
        """``Since we last spoke ({gap}): {summary, or 'you were talking about "title"'}.
        Since then: {n} done, {m} set aside[, k new patterns noticed][, s suggestions
        waiting].`` A zero clause is left out; with nothing to say, None.

        Only for a conversation with no messages yet: ``known_empty`` says the caller
        already knows (its history read found none), which saves a read; otherwise the
        conversation is checked first. Counted from the previous conversation's last
        activity; the gap in days on ``zone``'s clock; within SINCE_LAST_SPOKE_TOKENS.
        The counts and the summary check run at the same time. ``snapshot``: the user's
        memories as the turn read them, for the pattern count and the forget check.
        """
        if not known_empty and await self._conversations.messages(user_id, session_id, limit=1):
            return None
        previous = await self._conversations.previous_session(user_id, session_id)
        if previous is None:
            return None
        now = self._clock()
        clauses, subject = await asyncio.gather(
            self._since_then(user_id, as_aware(previous.updated_at), now, snapshot),
            self._subject(user_id, previous, snapshot.let_go if snapshot is not None else None),
        )
        head = f"Since we last spoke ({gap(previous.updated_at, now, zone)}): "
        then = f" Since then: {', '.join(clauses)}." if clauses else ""

        def frame(body: str) -> str:
            return f"{head}{sentence(body)}{then}"

        if isinstance(subject, CleanSummary):
            body = fit(subject.sentences, subject.threads, frame, config.SINCE_LAST_SPOKE_TOKENS)
        else:
            body = subject or ""
        if body:
            return frame(body)
        return f"{head}{', '.join(clauses)}." if clauses else None

    async def _subject(
        self, user_id: str, previous: ConversationSession, let_go: list[Memory] | None
    ) -> CleanSummary | str | None:
        """What the previous conversation was about: its summary (what the filter left),
        else its title; None when nothing may be said about it."""
        if previous.summary:
            clean = await self._summaries.clean(user_id, previous.summary, let_go=let_go)
            if clean is None:  # could not be checked: say nothing about it
                return None
            if clean.sentences or clean.threads:
                return clean
        title = " ".join((previous.title or "").split()).rstrip(" .!?")
        if not title:
            return None
        checked = await self._summaries.clean(user_id, title, let_go=let_go)
        if checked is None or checked.dropped:
            return None
        return f'you were talking about "{as_data(title)}"'

    async def _since_then(
        self, user_id: str, since: datetime, now: datetime, snapshot: MemorySnapshot | None
    ) -> list[str]:
        events, patterns, waiting = await asyncio.gather(
            self._events.list_for_user(user_id, since=since, types=set(_OUTCOMES)),
            self._new_patterns(user_id, since, now, snapshot),
            self._waiting_suggestions(user_id, since, now),
        )
        outcomes = latest_outcome_per_slot(events)
        done = sum(1 for e in outcomes if e.event_type is TaskEventType.COMPLETED)
        set_aside = sum(1 for e in outcomes if e.event_type in _SET_ASIDE)
        clauses = []
        if done:
            clauses.append(f"{done} done")
        if set_aside:
            clauses.append(f"{set_aside} set aside")
        if patterns:
            clauses.append(_count(patterns, "new pattern noticed", "new patterns noticed"))
        if waiting:
            clauses.append(_count(waiting, "suggestion waiting", "suggestions waiting"))
        return clauses

    async def _new_patterns(
        self, user_id: str, since: datetime, now: datetime, snapshot: MemorySnapshot | None
    ) -> int:
        types = frozenset({"behavioral_pattern"})
        if snapshot is not None:
            return sum(1 for p in snapshot.in_use(now, types=types) if as_aware(p.created_at) > since)
        if self._memory is None:
            return 0
        try:
            patterns = await self._memory.list_active(user_id, types=types, now=now)
        except Exception as exc:  # the count is left out, never the line
            logger.warning("New patterns not counted (%s)", type(exc).__name__)
            return 0
        return sum(1 for p in patterns if as_aware(p.created_at) > since)

    async def _waiting_suggestions(self, user_id: str, since: datetime, now: datetime) -> int:
        if self._suggestions is None:
            return 0
        try:
            pending = await self._suggestions.list(
                user_id, status=SuggestionStatus.PENDING, limit=_SUGGESTION_READ_LIMIT
            )
        except Exception as exc:  # the count is left out, never the line
            logger.warning("Waiting suggestions not counted (%s)", type(exc).__name__)
            return 0
        return sum(
            1
            for s in pending
            if as_aware(s.created_at) > since
            and (s.expires_at is None or as_aware(s.expires_at) > now)
        )
