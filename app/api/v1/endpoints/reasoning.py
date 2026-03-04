"""Phase 1 — Socratic Task Chunker.

Decomposes monolithic goals into sub-25-minute micro-tasks using
Cognitive Load Theory, the WOOP framework, and Implementation Intentions.
The strict Pydantic schemas below act as the JSON contract between the
probabilistic LLM reasoning layer and the deterministic Python backend.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.core.config import GEMINI_API_KEY
from app.models.brain.litellm_conf import hybrid_route_query

# ---------------------------------------------------------------------------
# Pydantic schemas — the JSON contract
# ---------------------------------------------------------------------------


class ImplementationIntention(BaseModel):
    """WOOP 'Plan' stage: a deterministic If-Then trigger that bypasses
    the user's procrastination circuitry by pre-loading a behavioral
    response for an anticipated obstacle."""

    obstacle_trigger: str = Field(
        ...,
        description=(
            "The specific internal or external friction point the user is "
            "likely to encounter (e.g., 'Urge to check social media during "
            "a deep-focus block'). Identified via Mental Contrasting."
        ),
    )
    behavioral_response: str = Field(
        ...,
        description=(
            "The deterministic 'If-Then' action to execute when the obstacle "
            "fires (e.g., 'If the urge arises, then close all browser tabs "
            "and perform 2 minutes of box breathing'). Must be concrete and "
            "immediately actionable."
        ),
    )


class TaskChunk(BaseModel):
    """A single atomic unit of work sized to fit within the biological
    limits of sustained attention.  The 25-minute ceiling is a strict
    neurobiological Pomodoro constraint — not a suggestion — designed to
    prevent working-memory depletion in the prefrontal cortex."""

    task_id: str = Field(
        ...,
        description="Unique identifier for this chunk (e.g., 'task_1').",
    )
    title: str = Field(
        ...,
        description="Short, action-oriented title for the micro-task.",
    )
    duration_minutes: int = Field(
        ...,
        ge=1,
        le=25,
        description=(
            "Estimated duration in minutes.  Hard-capped at 25 to align "
            "with the Pomodoro technique's neurobiological window for "
            "sustained concentration before working-memory depletion sets in."
        ),
    )
    difficulty_weight: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Cognitive weight on a 0-1 scale representing intrinsic load.  "
            "Fed downstream into the Deep Knowledge Tracing (DKT) module and "
            "the Reinforcement Learning (RL) pathfinder for adaptive "
            "scheduling and mastery calibration."
        ),
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description=(
            "List of task_ids that must be completed before this chunk.  "
            "Encodes the precedence graph consumed by the Phase 2 "
            "OR-Tools CSP solver for deterministic calendar math."
        ),
    )
    completion_criteria: str = Field(
        ...,
        description=(
            "A specific, verifiable action the user can perform to prove "
            "task mastery (e.g., 'Solve 3 practice problems without "
            "referencing notes').  Vague criteria like 'Review chapter' are "
            "invalid.  This transforms the user from a passive recipient of "
            "a schedule into an active schema builder — maximizing germane "
            "cognitive load per Cognitive Load Theory."
        ),
    )
    implementation_intention: Optional[ImplementationIntention] = Field(
        default=None,
        description=(
            "Optional WOOP Plan: an If-Then trigger to pre-empt a likely "
            "obstacle for this specific chunk."
        ),
    )
    deadline_hint: Optional[str] = Field(
        default=None,
        description=(
            "Optional ISO-8601 date or natural-language deadline for this "
            "chunk (e.g., '2026-03-07' or 'before Friday exam').  Fed into "
            "the Phase 2 TMT priority formula: Motivation = (Expectancy * "
            "Value) / (Impulsiveness * Delay)."
        ),
    )


class GoalMetadata(BaseModel):
    """Maps the WOOP framework's Wish and Outcome stages to structured
    fields, providing the emotional saliency needed to activate
    physiological arousal for goal pursuit."""

    goal_id: str = Field(
        ...,
        description="Unique identifier for the high-level goal.",
    )
    objective: str = Field(
        ...,
        description=(
            "The 'Wish' — a clear, one-sentence statement of the user's "
            "desired achievement."
        ),
    )
    outcome_visualization: str = Field(
        ...,
        description=(
            "The 'Outcome' — a vivid description of the emotional and "
            "tangible state the user will experience upon success.  This "
            "increases emotional saliency and the arousal necessary to "
            "bridge the intention-action gap."
        ),
    )
    mastery_level_target: int = Field(
        ...,
        ge=1,
        le=5,
        description=(
            "Target mastery on a 1-5 scale (1 = awareness, 5 = teaching "
            "proficiency).  Drives how aggressively the DKT module "
            "schedules review and practice sessions."
        ),
    )


class ExecutionGraph(BaseModel):
    """The master response model — the complete contract between the
    probabilistic LLM reasoning layer and the deterministic Python
    backend.  Every field is designed to feed directly into a downstream
    engine (CSP solver, DKT, RL agent)."""

    goal_metadata: GoalMetadata
    decomposition: List[TaskChunk] = Field(
        ...,
        min_length=5,
        description=(
            "Ordered list of atomic micro-tasks produced by Socratic "
            "decomposition.  Each chunk is sized to the 25-minute "
            "Pomodoro ceiling and includes verifiable completion criteria. "
            "MUST contain at least 5 TaskChunk objects."
        ),
    )
    cognitive_load_estimate: Dict[str, float] = Field(
        ...,
        description=(
            "Estimated cognitive load for the overall goal.  Must include "
            "'intrinsic_load' (0.0-1.0) representing the inherent "
            "complexity before any instructional design is applied."
        ),
    )


# ---------------------------------------------------------------------------
# LLM response sanitization
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL,
)


def _sanitize_llm_json(raw: str) -> str:
    """Strip markdown code fences that local models (e.g. Qwen-27B)
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
    "6. Output ONLY strictly valid JSON matching the ExecutionGraph schema. "
    "No markdown fences, no commentary, no text outside the JSON object.\n"
    "7. The decomposition array MUST contain at least 5 TaskChunk objects. "
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

    async def _call_and_get_data(force_cloud: bool = False) -> dict:
        result = await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=SYSTEM_PROMPT,
            response_schema=ExecutionGraph,
            force_cloud=force_cloud,
            lenient_validation=True,
        )
        if isinstance(result, dict):
            return result
        sanitized = _sanitize_llm_json(result)
        return json.loads(sanitized)

    try:
        # Local-First: always try local Qwen first (academic topics like SARIMAX
        # are handled locally—the model is capable; formatting constraints were
        # the prior bottleneck, now mitigated by sanitization and max_tokens).
        data = await _call_and_get_data(force_cloud=False)

        # Last-resort fallback: only when local model fails (undersized decomposition
        # or Pydantic validation). Cloud Gemini reserved for L9 Real-Time Research
        # and this fallback only—never for proactive routing of decomposition.
        if len(data.get("decomposition", [])) < 5 and GEMINI_API_KEY:
            data = await _call_and_get_data(force_cloud=True)

        graph = ExecutionGraph(**data)
        if len(graph.decomposition) < 5:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Model returned insufficient tasks (need at least 5). "
                    "Try again or set GEMINI_API_KEY for last-resort cloud fallback."
                ),
            )
        return graph
    except HTTPException:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"LLM Reasoning Error: the model returned content that "
                f"could not be parsed into a valid ExecutionGraph. {exc}"
            ),
        ) from exc
