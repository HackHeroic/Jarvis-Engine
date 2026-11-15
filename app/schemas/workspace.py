"""Task workspace schemas for Proactive Cognitive Workspace."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.documents import LinkedDocument

ASSET_TYPES = Literal[
    "pdf_chunk",
    "youtube_link",
    "article_link",
    "blog_link",
    "leetcode_link",
    "codeforces_link",
    "generated_quiz",
    "generated_content",
]


class StudyAsset(BaseModel):
    """A surfaced asset (RAG chunk, link, or generated content) for a task workspace."""

    asset_type: str = Field(
        description="One of pdf_chunk, youtube_link, article_link, blog_link, "
        "leetcode_link, codeforces_link, generated_quiz, generated_content",
    )
    title: str = Field(description="Short title or label for the asset")
    content_or_url: str = Field(
        description="URL for links; markdown/text for generated content or PDF chunks",
    )
    rationale: str = Field(
        default="",
        description="Why this was surfaced, e.g. 'Matches your visual learning style'",
    )
    metadata: Optional[dict] = Field(
        default=None,
        description="Optional metadata, e.g. difficulty, topic for problems",
    )


# How many questions a workspace quiz has.
QUIZ_MIN_QUESTIONS = 3
QUIZ_MAX_QUESTIONS = 5


class QuizQuestion(BaseModel):
    """One practice question with its answer, and an optional nudge toward it."""

    question: str = Field(min_length=1, description="The question, one or two sentences.")
    answer: str = Field(
        min_length=1, description="The expected answer, brief (one to three sentences)."
    )
    hint: str = Field(default="", description="A short nudge toward the answer, or empty.")

    @field_validator("hint", mode="before")
    @classmethod
    def _no_hint(cls, value: object) -> object:
        """A hint left null means there is no hint."""
        return "" if value is None else value


class Quiz(BaseModel):
    """A short practice quiz drawn from the task's material."""

    title: str = Field(min_length=1, description="A short title naming what the quiz covers.")
    questions: List[QuizQuestion] = Field(
        min_length=QUIZ_MIN_QUESTIONS,
        max_length=QUIZ_MAX_QUESTIONS,
        description=f"{QUIZ_MIN_QUESTIONS} to {QUIZ_MAX_QUESTIONS} questions, in order.",
    )


class QuizReply(BaseModel):
    """What the model answers when asked for a quiz: one quiz, or none when the material
    is not about the task."""

    assets: List[Quiz] = Field(
        description="The quiz, or empty when the material is not about the task.",
    )


class WoopCard(BaseModel):
    """Full MCII (Mental Contrasting with Implementation Intentions) card.

    All 4 stages of WOOP — research shows 60% more practice completion
    with all 4 stages vs if-then alone.
    """

    wish: str = Field(description="Goal objective (Wish stage)")
    outcome: str = Field(default="", description="Emotional outcome visualization (Outcome stage)")
    obstacle: str = Field(description="Personal barrier / obstacle trigger (Obstacle stage)")
    plan: str = Field(description="If-then behavioral response (Plan stage)")


class TaskWorkspace(BaseModel):
    """Proactive workspace assembled when user opens a scheduled task."""

    task_id: str = Field(description="Task identifier")
    task_title: str = Field(description="Display title of the task")
    primary_objective: str = Field(
        description="Derived from title + topic_keywords; main learning goal",
    )
    surfaced_assets: List[StudyAsset] = Field(
        default_factory=list,
        description="RAG chunks, curated links, and generated practice assets",
    )
    woop_card: Optional[WoopCard] = Field(
        default=None,
        description="MCII card with obstacle/response if available for this task",
    )
    notices: List[str] = Field(
        default_factory=list,
        description="Plain statements of what could not be surfaced, e.g. that web results are unavailable",
    )
    linked_documents: List[LinkedDocument] = Field(
        default_factory=list,
        description=(
            "The caller's documents linked to this task (\"From your documents\"), each with"
            " whether its file can be opened. Read fresh on every request, never cached."
        ),
    )
