"""What Jarvis remembers about the user, and the lifecycle each memory goes through.

A fact said again is reinforced, never stored twice. A contradiction supersedes the
old fact. Forget and dismiss stick: the row stays (a forgotten one loses its words but
keeps its embedding and pattern key), so the same fact, said or noticed again, is
recognised and not stored. Every memory carries its provenance: the user said it, or
Jarvis inferred it from what happened. Strength decays with time unused; a daily sweep
persists the decay and fades what has dropped below the retention floor. What the
model is told for a message (``recall``) is a fixed token budget, whatever the count.

DISMISSED and FORGOTTEN are terminal. Nothing automatic (a mention, a contradiction,
the decay sweep) ever moves a memory out of them, and every write that could is a
compare-and-set on the status, so a forget that lands between a read and its write
still wins. The one way out of DISMISSED is the user's own forget, which erases it; a
forgotten memory never changes again. Confirming or editing either is a conflict.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

import app.core.config as config
from app.domain import errors
from app.domain.entities import (
    SUPPRESSING_STATUSES,
    Memory,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.errors import EmbeddingUnavailableError
from app.domain.ports import MemoryRepository, Sweep, TextEmbedder
from app.services.memory.context import Recall, estimate_tokens, render_memory_line
from app.services.task_service import utc_now

logger = logging.getLogger(__name__)

# The entity's ceiling on stability: at 20 a memory loses a factor of e every 20 weeks.
_MAX_STABILITY = 20.0
# Confirming a memory makes Jarvis at least this sure of it.
_CONFIRMED_CONFIDENCE = 0.9
# Memories still in use: a mention reinforces one (a faded one comes back), the user may
# confirm or edit one. A dismissed or forgotten memory is never among them again.
_IN_USE = frozenset({MemoryStatus.ACTIVE, MemoryStatus.FADED})
# What the user's forget may erase: anything not already forgotten.
_FORGETTABLE = frozenset({MemoryStatus.ACTIVE, MemoryStatus.FADED, MemoryStatus.DISMISSED})
_ACTIVE = frozenset({MemoryStatus.ACTIVE})
# The memory that replaces memory X has an id derived from (user, X), so a supersede
# retried after a partial failure finds the replacement it already stored.
_SUPERSEDE_IDS = uuid.UUID("6f1d3c2a-6b0e-4f6a-9a51-2f0c8e7d4b19")
# A letter or digit in any script: text without one has nothing in it to remember.
_WORD = re.compile(r"[^\W_]")
# Recall's always-on profile: these types, in this order, from this sure up.
_PROFILE_TYPES = ("constraint", "preference", "goal")
_PROFILE_MIN_CONFIDENCE = 0.6
# How many of the nearest memories recall considers for the message.
_RECALL_CANDIDATES = 12


class MemoryCandidate(BaseModel):
    """Something that might be worth remembering, before Jarvis decides what to do with it."""

    memory_type: MemoryType = Field(description="What kind of thing it is.")
    content: str = Field(min_length=1, max_length=2000, description="The memory in words.")
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="How sure the source is that it holds, 0 to 1."
    )
    contradicts: str | None = Field(
        default=None, description="Id of the user's active memory this one replaces, if any."
    )
    expires_at: datetime | None = Field(
        default=None, description="After this instant it no longer applies (temporal events)."
    )


class RememberOutcome(BaseModel):
    """What ``MemoryService.remember`` did with a candidate."""

    action: Literal["created", "reinforced", "superseded", "suppressed"] = Field(
        description=(
            "created: stored as a new memory; reinforced: an existing memory of the same"
            " fact was strengthened; superseded: stored, replacing the memory it contradicts;"
            " suppressed: nothing was written (the user forgot or dismissed this fact, or"
            " the text has no words to remember)."
        )
    )
    memory: Memory | None = Field(
        description="The memory as stored now; None when nothing was written."
    )


def _has_words(text: str) -> bool:
    return _WORD.search(text) is not None


def _in_use_at(memory: Memory, now: datetime) -> bool:
    """Not expired and at least MEMORY_RETENTION_FLOOR strong at ``now``."""
    return (memory.expires_at is None or memory.expires_at > now) and (
        memory.strength_at(now) >= config.MEMORY_RETENTION_FLOOR
    )


def _within_budget(memories: list[Memory], now: datetime, budget: int) -> tuple[list[Memory], int]:
    """The memories whose note lines fit ``budget`` tokens, taken in order, and their cost.

    A line that does not fit is skipped, so one long memory never crowds out the
    shorter ones after it. Each line is counted with its newline, so the lines
    joined never come to more than the count.
    """
    chosen: list[Memory] = []
    used = 0
    for memory in memories:
        cost = estimate_tokens(render_memory_line(memory, now) + "\n")
        if used + cost <= budget:
            chosen.append(memory)
            used += cost
    return chosen, used


def _reinforcement(memory: Memory, now: datetime) -> dict:
    """The fields that reinforce ``memory`` at ``now``: active again, full strength, steadier."""
    return {
        "status": MemoryStatus.ACTIVE,
        "strength": 1.0,
        "strength_as_of": now,
        "stability": min(_MAX_STABILITY, memory.stability + 1.0),
        "observation_count": memory.observation_count + 1,
        "confidence": min(1.0, memory.confidence + 0.1),
    }


class MemoryService:
    """Remembers, reinforces, supersedes and lets go of what Jarvis knows about the user."""

    def __init__(
        self,
        memories: MemoryRepository,
        embedder: TextEmbedder,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._memories = memories
        self._embedder = embedder
        self._clock = clock

    async def remember(
        self,
        user_id: str,
        candidate: MemoryCandidate,
        *,
        provenance: MemorySource = MemorySource.SAID,
        origin: str = "conversation",
        source_session_id: str | None = None,
        source_message_id: str | None = None,
    ) -> RememberOutcome:
        """Store, reinforce or supersede a memory, or recognise one the user let go of.

        In order: a fact close to one the user forgot or dismissed, of any type, is
        suppressed and nothing is written; one that ``contradicts`` the user's active
        memory is added and the old one superseded (safe to retry); one close to an
        active or faded memory of the same
        type reinforces it (and confirms it when the user said it); anything else is
        created. Text with no words is never embedded: there is nothing to remember.
        """
        if not _has_words(candidate.content):
            return RememberOutcome(action="suppressed", memory=None)
        embedding = await self._embed(candidate.content)
        now = self._clock()

        # Any type: a fact the user let go of stays gone whatever it is offered as.
        if await self._memories.nearest(
            user_id,
            embedding,
            statuses=SUPPRESSING_STATUSES,
            limit=1,
            min_similarity=config.MEMORY_SUPPRESS_SIMILARITY,
        ):
            return RememberOutcome(action="suppressed", memory=None)

        new = Memory(
            user_id=user_id,
            memory_type=candidate.memory_type,
            content=candidate.content,
            provenance=provenance,
            origin=origin,
            confidence=candidate.confidence,
            strength_as_of=now,
            created_at=now,
            expires_at=candidate.expires_at,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
        )

        if candidate.contradicts is not None:
            old = await self._memories.get(user_id, candidate.contradicts)
            if old is not None and old.status is MemoryStatus.ACTIVE and old.superseded_by is None:
                return await self._supersede(user_id, old, new, embedding)

        matches = await self._memories.nearest(
            user_id,
            embedding,
            statuses=_IN_USE,
            types=frozenset({candidate.memory_type}),
            limit=1,
            min_similarity=config.MEMORY_DEDUPE_SIMILARITY,
        )
        if matches:
            existing = matches[0].memory
            fields = _reinforcement(existing, now)
            if provenance is MemorySource.SAID:  # saying it again confirms it
                fields["last_confirmed_at"] = now
            try:
                reinforced = await self._memories.update(
                    user_id, existing.id, only_if_status=_IN_USE, **fields
                )
            except errors.StaleVersionError:  # dismissed or forgotten since it was found
                return RememberOutcome(action="suppressed", memory=None)
            return RememberOutcome(action="reinforced", memory=reinforced)

        stored = await self._memories.add(user_id, new, embedding=embedding)
        return RememberOutcome(action="created", memory=stored)

    async def confirm(self, user_id: str, memory_id: str) -> Memory:
        """The user says it holds: reinforce it, at least 0.9 sure, confirmed now.

        A dismissed or forgotten memory is a ConflictError: it stays let go of.
        """
        memory = await self._in_use(user_id, memory_id, "confirmed")
        now = self._clock()
        fields = _reinforcement(memory, now)
        fields["confidence"] = max(fields["confidence"], _CONFIRMED_CONFIDENCE)
        fields["last_confirmed_at"] = now
        return await self._memories.update(user_id, memory_id, only_if_status=_IN_USE, **fields)

    async def dismiss(self, user_id: str, memory_id: str) -> Memory:
        """The user says it does not hold: hide it, keeping its words. It never comes back.

        Dismissing it again changes nothing; a forgotten memory is a ConflictError.
        """
        memory = await self._get(user_id, memory_id)
        if memory.status is not MemoryStatus.DISMISSED:
            try:
                return await self._memories.update(
                    user_id, memory_id, only_if_status=_IN_USE, status=MemoryStatus.DISMISSED
                )
            except errors.StaleVersionError:  # dismissed or forgotten meanwhile
                memory = await self._get(user_id, memory_id)
        if memory.status is MemoryStatus.FORGOTTEN:
            raise _let_go(memory, "dismissed")
        return memory

    async def forget(self, user_id: str, memory_id: str) -> None:
        """Erase the memory's words for good; a dismissed memory may be forgotten too.

        The row stays as a tombstone: its embedding and pattern key are kept, so the
        same fact said or noticed again is recognised and not stored. Forgetting a
        forgotten memory changes nothing; another user's memory is NotFoundError.
        """
        memory = await self._get(user_id, memory_id)
        if memory.status is MemoryStatus.FORGOTTEN:
            return
        try:
            await self._memories.update(
                user_id, memory_id, only_if_status=_FORGETTABLE,
                status=MemoryStatus.FORGOTTEN, content="",
            )
        except errors.StaleVersionError:  # forgotten meanwhile: the same end
            return

    async def edit(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> Memory:
        """Change the memory's words or type. New words are embedded again, so a later
        mention is matched against what it says now."""
        if content is None and memory_type is None:
            raise errors.ValidationError("Nothing to change: give new words or a new type.")
        await self._in_use(user_id, memory_id, "edited")
        fields: dict = {}
        if memory_type is not None:
            fields["memory_type"] = memory_type
        if content is not None:
            if not _has_words(content):
                raise errors.ValidationError("A memory needs at least one word.")
            fields["content"] = content
            fields["embedding"] = await self._embed(content)
        return await self._memories.update(user_id, memory_id, only_if_status=_IN_USE, **fields)

    async def list_active(
        self, user_id: str, *, types: frozenset[str] | None = None, now: datetime | None = None
    ) -> list[Memory]:
        """The user's memories in use at ``now`` (default: the clock), oldest first.

        Active, not superseded, not expired, and at least MEMORY_RETENTION_FLOOR strong.
        """
        now = now if now is not None else self._clock()
        return [
            memory
            for memory in await self._memories.list(user_id, types=types)
            if _in_use_at(memory, now)
        ]

    async def recall(
        self, user_id: str, query: str, *, trivial: bool = False, now: datetime | None = None
    ) -> Recall:
        """What to tell the model about the user for the message ``query``, within budget.

        The profile: active constraints, preferences and goals at least 0.6 sure, in
        that type order and newest (last confirmed, else created) first, added while
        their lines fit MEMORY_PROFILE_TOKENS. Then the relevant memories: the 12
        nearest active ones from MEMORY_RECALL_MIN_SIMILARITY up, less the profile,
        ranked by similarity x (0.5 + 0.5 x strength now) x confidence, added while
        their lines fit MEMORY_RECALL_TOKENS. A line too long for what is left of a
        budget is skipped, not the end of it. Ties go by id, so the order never depends
        on the store's. Superseded, expired and below-floor memories never appear, nor
        any memory the user let go of or another user's.

        A ``trivial`` message, or one with no words, gets the profile only, without an
        embedding call. An embedder that is down leaves the profile only, logged.
        """
        now = now if now is not None else self._clock()
        active = await self.list_active(user_id, types=frozenset(_PROFILE_TYPES), now=now)
        standing = [m for m in active if m.confidence >= _PROFILE_MIN_CONFIDENCE]
        # The id breaks ties, so the same memories always give the same note (and the
        # same cacheable bytes), whatever order the store returns them in.
        standing.sort(
            key=lambda m: (
                _PROFILE_TYPES.index(m.memory_type),
                -(m.last_confirmed_at or m.created_at).timestamp(),
                m.id or "",
            )
        )
        profile, profile_tokens = _within_budget(standing, now, config.MEMORY_PROFILE_TOKENS)
        if trivial or not _has_words(query):
            return Recall(profile=profile, relevant=[], tokens=profile_tokens)

        try:
            embedding = await self._embed(query)
        except EmbeddingUnavailableError:
            logger.warning("Recall without relevant memories: the text embedder is unavailable")
            return Recall(profile=profile, relevant=[], tokens=profile_tokens)
        matches = await self._memories.nearest(
            user_id,
            embedding,
            statuses=_ACTIVE,
            limit=_RECALL_CANDIDATES,
            min_similarity=config.MEMORY_RECALL_MIN_SIMILARITY,
        )
        shown = {m.id for m in profile}
        candidates = [
            match
            for match in matches
            if match.memory.id not in shown and _in_use_at(match.memory, now)
        ]
        candidates.sort(
            key=lambda match: (
                -match.similarity
                * (0.5 + 0.5 * match.memory.strength_at(now))
                * match.memory.confidence,
                match.memory.id or "",
            )
        )
        relevant, relevant_tokens = _within_budget(
            [match.memory for match in candidates], now, config.MEMORY_RECALL_TOKENS
        )
        return Recall(profile=profile, relevant=relevant, tokens=profile_tokens + relevant_tokens)

    async def fade_stale(self, user_id: str, now: datetime) -> int:
        """Persist each active memory's decay as of ``now``; return how many faded.

        Strength is stored as measured at ``now`` (with ``strength_as_of = now``), so
        reading it later never decays the same hours twice. A memory below
        MEMORY_RETENTION_FLOOR fades, unless it is a constraint. A memory measured at or
        after ``now`` has not decayed and is left alone.
        """
        faded = 0
        for memory in await self._memories.list(user_id):
            if memory.strength_as_of >= now:
                continue
            strength = memory.strength_at(now)
            fields: dict = {"strength": strength, "strength_as_of": now}
            fades = strength < config.MEMORY_RETENTION_FLOOR and memory.memory_type != "constraint"
            if fades:
                fields["status"] = MemoryStatus.FADED
            try:
                await self._memories.update(user_id, memory.id, only_if_status=_ACTIVE, **fields)
            except errors.StaleVersionError:  # dismissed, forgotten or faded meanwhile
                continue
            if fades:
                faded += 1
        return faded

    async def _supersede(
        self, user_id: str, old: Memory, new: Memory, embedding: list[float]
    ) -> RememberOutcome:
        """Store ``new`` and point ``old`` at it; safe to retry after a partial failure.

        ``new`` gets an id derived from (user, old), so a retry finds the replacement
        it already stored instead of adding a second. The link is guarded: if ``old``
        stopped being active meanwhile, ``new`` simply stands as a created memory.
        """
        replacement_id = str(uuid.uuid5(_SUPERSEDE_IDS, f"{user_id}:{old.id}"))
        try:
            stored = await self._memories.add(
                user_id, new.model_copy(update={"id": replacement_id}), embedding=embedding
            )
        except errors.ConflictError:  # stored by an earlier attempt
            stored = await self._memories.get(user_id, replacement_id)
            if stored is None:
                raise
        try:
            await self._memories.update(
                user_id, old.id, only_if_status=_ACTIVE, superseded_by=stored.id
            )
        except errors.StaleVersionError:
            return RememberOutcome(action="created", memory=stored)
        return RememberOutcome(action="superseded", memory=stored)

    async def _get(self, user_id: str, memory_id: str) -> Memory:
        """The user's memory, whatever its status; NotFoundError if absent or another user's."""
        memory = await self._memories.get(user_id, memory_id)
        if memory is None:
            raise errors.NotFoundError(
                "That memory does not exist.", details={"memory_id": memory_id}
            )
        return memory

    async def _in_use(self, user_id: str, memory_id: str, verb: str) -> Memory:
        """The user's memory if it is still in use; ConflictError if dismissed or forgotten."""
        memory = await self._get(user_id, memory_id)
        if memory.status not in _IN_USE:
            raise _let_go(memory, verb)
        return memory

    async def _embed(self, text: str) -> list[float]:
        """Embed ``text`` off the event loop (the model is CPU work)."""
        (vector,) = await asyncio.to_thread(self._embedder.embed, [text])
        return vector


def _let_go(memory: Memory, verb: str) -> errors.ConflictError:
    """The user let go of ``memory`` (dismissed or forgotten); it cannot be ``verb`` now."""
    return errors.ConflictError(
        f"This memory was {memory.status.value}, so it can't be {verb}.",
        details={"memory_id": memory.id, "status": memory.status.value},
    )


class MemoryDecaySweep(Sweep):
    """Runs ``MemoryService.fade_stale`` for every user, once a day."""

    name: ClassVar[str] = "memory_decay"
    interval: ClassVar[timedelta] = timedelta(hours=24)

    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def run(self, user_ids: list[str], now: datetime) -> int:
        """Persist each user's memory decay; return how many memories faded.

        One user's failure never stops the others; failures are logged once per run,
        counted by kind.
        """
        faded = 0
        failures: dict[str, int] = {}
        for user_id in user_ids:
            try:
                faded += await self._memory.fade_stale(user_id, now)
            except Exception as exc:  # one user's failure never stops the others
                kind = type(exc).__name__
                failures[kind] = failures.get(kind, 0) + 1
        if failures:
            logger.warning(
                "Memory decay sweep could not finish for %d of %d users (%s)",
                sum(failures.values()),
                len(user_ids),
                ", ".join(f"{kind} x{count}" for kind, count in sorted(failures.items())),
            )
        return faded
