"""Phase 1 — Socratic Task Chunker.

Decomposes monolithic goals into sub-25-minute micro-tasks using
Cognitive Load Theory, the WOOP framework, and Implementation Intentions.
The strict Pydantic schemas (``app/schemas/decomposition.py``, where services use them
too) are the JSON contract between the probabilistic LLM reasoning layer and the
deterministic Python backend. A model reply that never becomes a valid graph is
``LLMOutputParseError`` (503 ``llm_output_parse`` in the envelope).
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Body
from pydantic import ValidationError

from app.domain.entities import LLMLocation
from app.domain.errors import LLMOutputParseError, LLMUnavailableError
from app.schemas.decomposition import ExecutionGraph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM response sanitization
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL,
)


def _sanitize_llm_json(raw: str) -> str:
    """Strip markdown code fences that local models (e.g. Gemma)
    sometimes wrap around otherwise valid JSON."""
    stripped = raw.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


# ---------------------------------------------------------------------------
# System prompt — encodes CLT, WOOP, and Anti-Guilt philosophy
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are the Jarvis Reasoning Engine, a proactive preparation engine "
    "designed to eliminate the user's mental overhead. The user's prefrontal "
    "cortex is a finite resource. You must recursively decompose their "
    "monolithic goal into sub-25-minute micro-tasks. This 25-minute limit "
    "is a strict neurobiological Pomodoro constraint to prevent working "
    "memory depletion. For every obstacle you identify, you must generate a "
    "deterministic 'If-Then' Implementation Intention to bypass human "
    "procrastination.\n\n"
    "Additional rules you MUST follow:\n"
    "1. Every task must have a specific, verifiable completion_criteria that "
    "forces the user into active schema construction (germane cognitive "
    "load). Good: 'Can explain the three laws of thermodynamics without "
    "notes.' Bad: 'Review thermodynamics.'\n"
    "2. Assign each task a difficulty_weight between 0.0 and 1.0 "
    "representing its intrinsic cognitive load.\n"
    "3. Populate goal_metadata with a vivid outcome_visualization that "
    "increases emotional saliency.\n"
    "4. mastery_level_target must be an integer from 1 to 5 inclusive "
    "(1 = awareness, 5 = teaching proficiency). Never use 0.\n"
    "5. Provide a cognitive_load_estimate with at least an 'intrinsic_load' "
    "float (0.0-1.0) for the overall goal.\n"
    "6. When the user mentions a deadline (exam date, due date, 'by Friday'), "
    "include it in deadline_hint as ISO-8601 (YYYY-MM-DD) when possible. "
    "Example: 'exam on March 20' -> deadline_hint: '2026-03-20'. "
    "Put the deadline on the final or most relevant chunk.\n"
    "7. Output ONLY strictly valid JSON matching the ExecutionGraph schema. "
    "No markdown fences, no commentary, no text outside the JSON object.\n"
    "8. The decomposition array MUST contain at least 5 TaskChunk objects. "
    "An empty or undersized decomposition is invalid and indicates failure "
    "to complete the task.\n\n"
    "Example of one TaskChunk in the decomposition array:\n"
    '{"task_id":"task_1","title":"Master SARIMA notation","duration_minutes":15,'
    '"difficulty_weight":0.5,"dependencies":[],"completion_criteria":"Write the full '
    'SARIMA(p,d,q)(P,D,Q)s equation and explain each parameter","implementation_intention":'
    '{"obstacle_trigger":"Confusion between seasonal and non-seasonal terms","behavioral_response":'
    '"If confused, then draw a 2x2 table: non-seasonal vs seasonal, p vs P, d vs D"}}'
)

# ---------------------------------------------------------------------------
# Router and endpoint
# ---------------------------------------------------------------------------

router = APIRouter()

# Fixed texts: an exception's own message never goes on the wire (it is logged).
_UNPARSEABLE = (
    "LLM Reasoning Error: the model returned content that "
    "could not be parsed into a valid ExecutionGraph."
)
_TOO_FEW_TASKS = (
    "Model returned insufficient tasks (need at least 5). "
    "Try again or set GEMINI_API_KEY for last-resort cloud fallback."
)


@router.post(
    "/decompose-goal",
    response_model=ExecutionGraph,
    summary="Socratic Task Chunker",
    description=(
        "Accepts a high-level goal and uses the LiteLLM Hybrid Router to "
        "perform Socratic decomposition into sub-25-minute micro-tasks.  "
        "The response is validated against the ExecutionGraph schema, which "
        "encodes Cognitive Load Theory constraints and the WOOP behavioural "
        "framework."
    ),
)
async def decompose_goal(
    user_prompt: str = Body(
        ...,
        embed=True,
        description="The user's raw goal or objective to be decomposed.",
    ),
) -> ExecutionGraph:
    """Decompose a monolithic goal into an ExecutionGraph of atomic,
    sub-25-minute micro-tasks with WOOP Implementation Intentions."""

    from app.core.model_router import get_chain, route_llm_call

    async def _decompose(force_cloud: bool) -> ExecutionGraph:
        result = await route_llm_call(
            task="socratic_chunker",
            prompt=user_prompt,
            system_prompt=SYSTEM_PROMPT,
            response_schema=ExecutionGraph,
            force_cloud=force_cloud,
        )
        return ExecutionGraph.model_validate(result)

    try:
        # Local-First: the PRIMARY model first. A reply that fails ExecutionGraph
        # validation falls over to the cloud inside the provider chain.
        graph = await _decompose(force_cloud=False)

        # Last-resort fallback for a valid but undersized decomposition: one more
        # try on the cloud, when a cloud model is configured.
        if len(graph.decomposition) < 5 and get_chain().has_available(LLMLocation.CLOUD):
            graph = await _decompose(force_cloud=True)

        if len(graph.decomposition) < 5:
            raise LLMOutputParseError(_TOO_FEW_TASKS)
        return graph
    except LLMOutputParseError:
        raise
    except (LLMUnavailableError, RuntimeError) as exc:
        if not _unparseable(exc):
            raise
        raise LLMOutputParseError(_UNPARSEABLE) from exc
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        logger.warning("Decomposition reply did not parse as an ExecutionGraph", exc_info=exc)
        raise LLMOutputParseError(_UNPARSEABLE) from exc


def _unparseable(exc: BaseException | None) -> bool:
    """True when the last model asked did answer, but not with a valid ExecutionGraph."""
    while exc is not None:
        if isinstance(exc, LLMOutputParseError):
            return True
        exc = exc.__cause__
    return False
