"""The decomposition contract: a goal broken into sub-25-minute micro-tasks.

The strict Pydantic schemas the Socratic chunker's model answers in, and that the
planning module, the scheduler and the task rows read: the JSON contract between the
probabilistic LLM reasoning layer and the deterministic Python backend (Cognitive Load
Theory, WOOP, Implementation Intentions). Here, not in the reasoning endpoint, so that
services may use them without importing the API layer.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# Where each part of the day starts on the user's clock: the not_before of work the
# user asks to do "tomorrow evening". The one table; the decomposition prompt and the
# not_before field's description both read it.
DAY_PART_START_HOURS: Dict[str, int] = {
    "morning": 8,
    "afternoon": 12,
    "evening": 18,
    "night": 20,
}
DAY_PART_STARTS_TEXT = ", ".join(
    f"{part} {hour:02d}:00" for part, hour in DAY_PART_START_HOURS.items()
)


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
    not_before: Optional[str] = Field(
        default=None,
        description=(
            "Optional earliest start, set only when the user says when to do the work "
            "('tomorrow evening', 'on Saturday', 'after 6 PM'): the start of that window "
            "as a local date or date-time in the user's zone, YYYY-MM-DD or "
            "YYYY-MM-DDTHH:MM (a part of the day starts at "
            f"{DAY_PART_STARTS_TEXT}). The scheduler never starts the chunk "
            "earlier. Null when no time is stated. A deadline ('by Friday') is never a "
            "not_before. A value with a UTC offset is read as that instant."
        ),
    )


class GoalMetadata(BaseModel):
    """Maps the WOOP framework's Wish and Outcome stages to structured
    fields, providing the emotional saliency needed to activate
    physiological arousal for goal pursuit."""

    goal_id: str = Field(
        default="",
        description="Unique identifier for the high-level goal.",
    )
    objective: str = Field(
        default="",
        description=(
            "The 'Wish' — a clear, one-sentence statement of the user's "
            "desired achievement."
        ),
    )
    outcome_visualization: str = Field(
        default="",
        description=(
            "The 'Outcome' — a vivid description of the emotional and "
            "tangible state the user will experience upon success.  This "
            "increases emotional saliency and the arousal necessary to "
            "bridge the intention-action gap."
        ),
    )
    mastery_level_target: int = Field(
        default=3,
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
        min_length=1,
        description=(
            "Ordered list of atomic micro-tasks produced by Socratic "
            "decomposition.  Each chunk is sized to the 25-minute "
            "Pomodoro ceiling and includes verifiable completion criteria. "
            "For decompose-goal output, at least 5 tasks are required "
            "(enforced by endpoint logic). For schedule input, 1+ tasks "
            "are accepted."
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
