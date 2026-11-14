"""Observation loop: what Jarvis learns from a turn, while the turn is still running.

On every non-trivial turn, after the reply and before the stream's ``complete`` frame:

1. The lasting facts the user stated (``brain_dump.memories``, from the turn's one
   extraction call) are remembered through ``MemoryService``: provenance "said", with
   the conversation and the user's message as their source. A fact the user forgot or
   dismissed is suppressed; a cited ``[mN]`` handle supersedes the memory it names.
   The writes share a cap of MEMORY_PERSIST_TIMEOUT_S; what landed by then counts.
2. One ``memory_extracted`` frame per memory created, reinforced or superseded.
3. PEARL looks at the outcome history (``PatternService.observe``) and cognitive state
   updates, together capped at 500 ms: one ``pattern_detected`` frame per pattern
   created or strengthened.

Nothing is left running once the loop returns. Without a registered composition root
(tests, scripts) no memory is learned. Failures are logged by type and never shown.
"""

import asyncio
import json
import uuid
from datetime import date, datetime

from pydantic import ValidationError

from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.core.runtime import registered_container
from app.domain.entities import MemorySource
from app.domain.errors import EmbeddingUnavailableError
from app.services.memory.memory_service import MemoryCandidate, RememberOutcome
from app.services.planning.local_time import end_of_local_day, user_zone

# Remembering the turn's memories never holds the stream open longer than this.
MEMORY_PERSIST_TIMEOUT_S = 2.0
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


async def _expiry(value: str | None, user_id: str, container) -> datetime | None:
    """When a temporal memory stops applying, as an instant; None if absent or unreadable.

    A date alone ends with that day on the user's clock; a time without a zone is theirs.
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
    zone = user_zone(await container.repos.profiles.get_or_create(user_id))
    return end_of_local_day(day, zone) if day is not None else moment.replace(tzinfo=zone)


async def persist_turn_memories(state: dict) -> list[RememberOutcome]:
    """Remember each of the turn's ``brain_dump.memories``; return what came of each.

    Provenance SAID, sourced to the turn's conversation and user message. A cited handle
    becomes the id of the memory it was shown for; any other citation is ignored. A
    memory with no words is skipped before anything is embedded. All of it shares
    MEMORY_PERSIST_TIMEOUT_S: past it the write in flight is abandoned and the outcomes
    that landed are returned. No composition root, nothing to remember, or an embedder
    that is down: an empty or partial list, never an exception.
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

    async def remember_all() -> None:
        for memory in extracted:
            content = (memory.content or "").strip()
            if not content:
                continue
            try:
                candidate = MemoryCandidate(
                    memory_type=memory.type,
                    content=content,
                    confidence=memory.confidence,
                    contradicts=_cited_memory_id(memory.contradicts, handles),
                    expires_at=await _expiry(memory.expires_at, user_id, container),
                )
                outcome = await container.memory_service.remember(
                    user_id, candidate, provenance=MemorySource.SAID, origin="conversation",
                    **sources,
                )
            except EmbeddingUnavailableError:  # nothing else will embed either
                logger.warning("Memories not learned this turn: the text embedder is unavailable")
                return
            except ValidationError:
                logger.warning("A memory from this turn was not learned: it did not fit a memory")
                continue
            except Exception as e:
                logger.warning(f"A memory from this turn was not learned ({type(e).__name__})")
                continue
            landed.append(outcome)

    try:
        await asyncio.wait_for(remember_all(), timeout=MEMORY_PERSIST_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            f"Remembering this turn's memories passed {MEMORY_PERSIST_TIMEOUT_S}s;"
            f" {len(landed)} of {len(extracted)} landed"
        )
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


async def detect_pearl_patterns(state: dict) -> list[dict]:
    """Detect recurring behavioral patterns from task history.

    With the composition root registered, PEARL reads the outcome history
    (``PatternService.observe``) and returns one learning-moment frame per pattern it
    created or strengthened: nothing when there is no new evidence, and never a
    pattern the user dismissed or forgot. Without one (tests, scripts), the legacy
    detectors over ``user_tasks``.
    """
    user_model = state.get("user_model")
    if not user_model:
        return []
    container = registered_container()
    if container is not None:
        try:
            changes = await container.patterns.observe(user_model.user_id)
        except Exception as e:
            logger.warning(f"PEARL pattern detection failed (non-fatal): {e!r}")
            return []
        return [
            {
                "type": change.pattern.detector,
                "content": change.pattern.description,
                "confidence": change.pattern.confidence,
                "occurrence_count": change.pattern.occurrences,
                "action": change.action,
                "pattern_key": change.pattern.pattern_key,
                "memory_id": change.memory_id,
            }
            for change in changes
        ]
    try:
        db = getattr(user_model, "_db", None)
        supabase = db.supabase if db and hasattr(db, "supabase") else None
        if not supabase:
            return []
        memory_store = await user_model.get_memory_store()
        if not memory_store:
            return []
        from app.services.memory.pearl import detect_patterns
        patterns = await asyncio.to_thread(detect_patterns, user_model.user_id, supabase, memory_store)
        return patterns if isinstance(patterns, list) else []
    except Exception as e:
        logger.warning(f"PEARL pattern detection failed (non-fatal): {e}")
        return []


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
    - Remembers the turn's memories (capped at MEMORY_PERSIST_TIMEOUT_S), then emits
      their frames.
    - PEARL + cognitive update run next with a real 500ms cap (no shield —
      cancellation must propagate so we honor the cap).
    - Spawns nothing: every learning frame is on the queue before this returns.
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
    """Cheap inline work — PEARL stats + cognitive math. Targets <500ms."""
    patterns = await detect_pearl_patterns(state)
    for pattern in patterns:
        _emit_event(state, "pattern_detected", {
            "type": pattern.get("pattern", pattern.get("type", "behavioral_pattern")),
            "content": pattern.get("inference", pattern.get("content", "")),
            "confidence": pattern.get("rate", pattern.get("confidence", 0.0)),
            "occurrence_count": pattern.get("total", pattern.get("occurrence_count", 1)),
            "action": pattern.get("action", "detected"),
            "pattern_key": pattern.get("pattern_key"),
            "memory_id": pattern.get("memory_id"),
        })

    await update_cognitive_state(state)
    await bridge_patterns_to_constraints(state, patterns)

    return {
        "needs_followup": False,
        "patterns_detected_count": len(patterns),
    }
