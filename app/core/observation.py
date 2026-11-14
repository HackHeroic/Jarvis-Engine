"""Observation loop: what Jarvis learns from a turn, while the turn is still running.

On every non-trivial turn, after the reply and before the stream's ``complete`` frame:

1. The lasting facts the user stated (``brain_dump.memories``, from the turn's one
   extraction call) are remembered through ``MemoryService``: provenance "said", with
   the conversation and the user's message as their source. A fact the user forgot or
   dismissed is suppressed; a cited ``[mN]`` handle supersedes the memory it names.
   No memory write starts after MEMORY_PERSIST_TIMEOUT_S, and the one in flight is
   never cut short (a supersede is two writes).
2. One ``memory_extracted`` frame per memory created, reinforced or superseded.
3. PEARL looks at the outcome history (``PatternService.observe``) and cognitive state
   updates, together capped at 500 ms: one ``pattern_detected`` frame per learning
   moment held for the user, this turn's and those PEARL learned in the background
   since the last turn (after a Complete, a Skip or a passed slot).

Without a registered composition root (tests, scripts) nothing is learned: the legacy
detectors read ``user_tasks`` and would bring back patterns the user dismissed.
Failures are logged by type and never shown.
"""

import asyncio
import json
import uuid
from collections.abc import Coroutine
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.core.runtime import registered_container
from app.domain.entities import MemorySource
from app.domain.errors import EmbeddingUnavailableError
from app.services.memory.memory_service import MemoryCandidate, RememberOutcome
from app.services.memory.patterns import PatternChange
from app.services.planning.local_time import end_of_local_day, user_zone

# No new memory write starts once this much of the turn has gone to remembering.
MEMORY_PERSIST_TIMEOUT_S = 2.0
# The longest the turn waits for the write in flight. Past it the write is not cancelled
# (that could leave a supersede half-written): it finishes on its own, unannounced.
MEMORY_WRITE_TIMEOUT_S = 5.0
# PEARL's insights for the complete frame: one read, skipped past this.
PEARL_INSIGHTS_TIMEOUT_S = 0.5
# The outcomes the user is told about; a suppressed memory is told nothing.
_ANNOUNCED = ("created", "reinforced", "superseded")


def _emit_event(state: dict, event_type: str, data: dict) -> None:
    """Emit a typed SSE event directly onto the progress_queue."""
    queue = state.get("progress_queue")
    if not queue:
        return
    queue.put_nowait(json.dumps({"_event_type": event_type, **data}))


def _uuid_or_none(value: object) -> str | None:
    """``value`` if it is a UUID string (a memory's source ids are soft uuid references)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        uuid.UUID(value)
    except ValueError:
        return None
    return value


def _cited_memory_id(citation: str | None, handles: dict[str, str]) -> str | None:
    """The memory a cited handle ("m3", "[m3]") names; None for none or one never shown."""
    if not citation:
        return None
    return handles.get(citation.strip().strip("[]").strip().lower())


async def _user_zone_of(state: dict, timeout_s: float) -> ZoneInfo:
    """The user's zone, from the turn's ``UserModel.get_profile``: a plain read, cached
    for the turn, that never creates a profile. No facade or no profile: the default zone.
    Raises if the read fails or takes longer than ``timeout_s``."""
    get_profile = getattr(state.get("user_model"), "get_profile", None)
    profile = await asyncio.wait_for(get_profile(), timeout=timeout_s) if get_profile else None
    return user_zone(profile)


async def _expiry(value: str | None, state: dict, timeout_s: float) -> datetime | None:
    """When a temporal memory stops applying, as an instant; None if absent or unreadable.

    A date alone ends with that day on the user's clock; a time without a zone is theirs.
    When the user's zone cannot be read (within ``timeout_s``), None: the memory is
    stored without an expiry rather than not at all.
    """
    text = (value or "").strip()
    if not text:
        return None
    day: date | None = None
    moment: datetime | None = None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is not None:
            return moment
    try:
        zone = await _user_zone_of(state, max(timeout_s, 0.0))
    except Exception as e:  # asyncio.TimeoutError included
        logger.warning(f"Profile unreadable ({type(e).__name__}); memory kept without an expiry")
        return None
    return end_of_local_day(day, zone) if day is not None else moment.replace(tzinfo=zone)


async def _to_completion(write: Coroutine[Any, Any, RememberOutcome]) -> RememberOutcome:
    """Await ``write`` without ever cancelling it, for at most MEMORY_WRITE_TIMEOUT_S.

    A supersede stores the new memory, then retires the old one; cut between the two,
    both would stay active. So the write is shielded: when the turn stops waiting
    (TimeoutError) or is itself cancelled (the client left), it still runs to the end.
    """
    task = asyncio.ensure_future(write)
    # Retrieve the outcome, so a write nobody awaits any more that then fails is not
    # reported as an unretrieved task exception.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.wait_for(asyncio.shield(task), timeout=MEMORY_WRITE_TIMEOUT_S)


async def persist_turn_memories(state: dict) -> list[RememberOutcome]:
    """Remember each of the turn's ``brain_dump.memories``; return what came of each.

    Provenance SAID, sourced to the turn's conversation and user message. A cited handle
    becomes the id of the memory it was shown for; any other citation is ignored. A
    memory with no words is skipped before anything is embedded. No write starts after
    MEMORY_PERSIST_TIMEOUT_S; the one in flight then is awaited to its end (at most
    MEMORY_WRITE_TIMEOUT_S, never cancelled). No composition root, nothing to remember,
    or an embedder that is down: an empty or partial list, never an exception.
    """
    container = registered_container()
    user_id = state.get("user_id")
    extracted = list(getattr(state.get("brain_dump"), "memories", None) or [])
    if container is None or not user_id or not extracted:
        return []
    handles = state.get("memory_handles") or {}
    sources = {
        "source_session_id": _uuid_or_none(state.get("session_id")),
        "source_message_id": _uuid_or_none(state.get("user_message_id")),
    }
    landed: list[RememberOutcome] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MEMORY_PERSIST_TIMEOUT_S

    for index, memory in enumerate(extracted):
        if loop.time() >= deadline:
            logger.warning(
                f"Remembering this turn's memories passed {MEMORY_PERSIST_TIMEOUT_S}s;"
                f" {len(extracted) - index} not started"
            )
            break
        content = (memory.content or "").strip()
        if not content:
            continue
        try:
            candidate = MemoryCandidate(
                memory_type=memory.type,
                content=content,
                confidence=memory.confidence,
                contradicts=_cited_memory_id(memory.contradicts, handles),
                expires_at=await _expiry(memory.expires_at, state, deadline - loop.time()),
            )
            outcome = await _to_completion(container.memory_service.remember(
                user_id, candidate, provenance=MemorySource.SAID, origin="conversation",
                **sources,
            ))
        except asyncio.TimeoutError:  # the write goes on by itself; start no more
            logger.warning(
                f"A memory write outlasted {MEMORY_WRITE_TIMEOUT_S}s; it finishes unannounced"
            )
            break
        except EmbeddingUnavailableError:  # nothing else will embed either
            logger.warning("Memories not learned this turn: the text embedder is unavailable")
            break
        except ValidationError:
            logger.warning("A memory from this turn was not learned: it did not fit a memory")
            continue
        except Exception as e:
            logger.warning(f"A memory from this turn was not learned ({type(e).__name__})")
            continue
        landed.append(outcome)
    return landed


def memory_frame(outcome: RememberOutcome) -> dict | None:
    """The ``memory_extracted`` frame for ``outcome``; None when there is nothing to tell."""
    memory = outcome.memory
    if outcome.action not in _ANNOUNCED or memory is None or not memory.content.strip():
        return None
    return {
        "memory_id": memory.id,
        "type": memory.memory_type,
        "content": memory.content,
        "confidence": memory.confidence,
        "provenance": "said",
        "action": outcome.action,
    }


async def pearl_insights(user_id: str) -> list[dict] | None:
    """PEARL's insights for the turn's ``complete`` frame (``PatternService.insights``).

    None when there are none, there is no composition root, or they took longer than
    PEARL_INSIGHTS_TIMEOUT_S.
    """
    container = registered_container()
    if container is None or not user_id:
        return None
    try:
        insights = await asyncio.wait_for(
            container.patterns.insights(user_id), timeout=PEARL_INSIGHTS_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning("PEARL insights took too long; the turn completes without them")
        return None
    except Exception as e:
        logger.warning(f"PEARL insights unavailable this turn ({type(e).__name__})")
        return None
    return insights or None


def _pattern_frame(change: PatternChange) -> dict:
    """The ``pattern_detected`` frame for one held learning moment.

    ``confidence`` and ``occurrence_count`` are numbers: the frontend notes every moment
    inline and toasts only at confidence >= 0.7 with 3 or more occurrences.
    """
    return {
        "type": change.pattern.detector,
        "content": change.pattern.description,
        "confidence": float(change.pattern.confidence),
        "occurrence_count": int(change.pattern.occurrences),
        "action": change.action,
        "pattern_key": change.pattern.pattern_key,
        "memory_id": change.memory_id,
    }


async def detect_pearl_patterns(state: dict) -> list[dict]:
    """This turn's PEARL learning moments, as ``pattern_detected`` frames.

    PEARL reads the outcome history (``PatternService.observe``); then every moment
    held for the user (``drain_pending``) is returned, once: the ones this look found
    and the ones PEARL learned in the background since the last turn. Only the drained
    moments are told, never observe's own return as well, which holds the same moments.
    A failed look still tells what was held. Never a pattern the user dismissed or
    forgot. No composition root: nothing.
    """
    container = registered_container()
    user_id = state.get("user_id") or getattr(state.get("user_model"), "user_id", None)
    if container is None or not user_id:
        return []
    try:
        await container.patterns.observe(user_id)
    except Exception as e:
        logger.warning(f"PEARL pattern detection failed (non-fatal): {type(e).__name__}")
    return [_pattern_frame(change) for change in container.patterns.drain_pending(user_id)]


async def update_cognitive_state(state: dict) -> None:
    """Update estimated cognitive energy for this user."""
    user_model = state.get("user_model")
    if not user_model:
        return
    energy = await user_model.get_estimated_energy()
    # Future: update UserModel with fresh energy estimate


async def bridge_patterns_to_constraints(state: dict, patterns: list[dict]) -> int:
    """Persist high-confidence PEARL patterns as memory constraints.

    Patterns with rate/confidence >= 0.7 are already stored in the memory store
    by the PEARL detectors themselves. This function logs the count and returns it
    so the caller can report how many were bridged.

    The actual memory → TimeSlot conversion happens in planning_graph.memory_to_constraints
    (called during _run_plan_day_flow before solve_schedule).
    """
    bridged = 0
    for pattern in patterns:
        # Normalise: PEARL detectors use "rate"; generic patterns use "confidence"
        confidence = pattern.get("rate", pattern.get("confidence", 0.0))
        if confidence < 0.7:
            continue
        pattern_type = pattern.get("pattern", pattern.get("type", ""))
        logger.debug(f"PEARL pattern '{pattern_type}' (conf={confidence:.2f}) stored in memory — will apply on next replan")
        bridged += 1
    return bridged


async def run_observation_loop(state: dict) -> dict:
    """Learn from the turn, inline, before the stream completes.

    - Emits phase_start + phase_end SSE so frontend timing is accurate.
    - Skips entirely on trivial inputs (greetings/emotional) — no memories from "hi".
    - Remembers the turn's memories (no write starts after MEMORY_PERSIST_TIMEOUT_S;
      the one in flight is never cut short), then emits their frames.
    - PEARL + cognitive update run next with a real 500ms cap (no shield —
      cancellation must propagate so we honor the cap).
    - Every learning frame is on the queue before this returns. The only work that can
      outlive it is a memory write past MEMORY_WRITE_TIMEOUT_S, left to finish.
    """
    modules_invoked = state.get("modules_invoked", [])
    if len(modules_invoked) >= 10:
        return {"needs_followup": False}

    user_msg = (state.get("user_message") or "").strip().lower()
    is_trivial = state.get("trivial_input") or len(user_msg) < 6
    if is_trivial:
        _emit_event(state, "phase_end", {"phase": "learning", "skipped": True})
        return {"needs_followup": False, "memories_extracted_count": 0, "patterns_detected_count": 0}

    _emit_event(state, "phase_start", {"phase": "learning"})

    frames = [
        frame
        for frame in (memory_frame(outcome) for outcome in await persist_turn_memories(state))
        if frame is not None
    ]
    for frame in frames:
        _emit_event(state, "memory_extracted", frame)

    # PEARL + cognitive state are cheap stats/math; run inline with a REAL 500ms cap.
    # No asyncio.shield — we want the work cancelled when the cap fires, not leaked.
    try:
        result = await asyncio.wait_for(_run_inline_observation(state), timeout=0.5)
    except asyncio.TimeoutError:
        logger.warning("Inline observation (PEARL+cognitive) exceeded 500ms — cancelled and skipped")
        _emit_event(state, "phase_end", {"phase": "learning", "timeout": True})
        return {"needs_followup": False, "memories_extracted_count": len(frames)}
    _emit_event(state, "phase_end", {"phase": "learning"})
    return {**result, "memories_extracted_count": len(frames)}


async def _run_inline_observation(state: dict) -> dict:
    """Cheap inline work — PEARL stats + cognitive math. Targets <500ms.

    The moments are drained and put on the queue with no await in between, so the cap
    can never take a drained moment away before it is sent: a look the cap cuts short
    leaves its moments held for the next turn.
    """
    patterns = await detect_pearl_patterns(state)
    for frame in patterns:
        _emit_event(state, "pattern_detected", frame)

    await update_cognitive_state(state)
    await bridge_patterns_to_constraints(state, patterns)

    return {
        "needs_followup": False,
        "patterns_detected_count": len(patterns),
    }
