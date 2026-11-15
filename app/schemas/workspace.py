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


class OutlineSection(BaseModel):
    """One section of a piece of writing's outline."""

    heading: str = Field(description="The section's heading, a few words.")
    note: str = Field(default="", description="One short line on what goes in the section.")


class TaskOutline(BaseModel):
    """What the model answers for a writing, building or admin task: an outline and/or a
    checklist, shaped by the kind of task (see ``workspace/outline.py``)."""

    title: str = Field(default="", description="A short title for the outline or checklist.")
    sections: List[OutlineSection] = Field(
        default_factory=list,
        description="For a piece of writing: its sections, in order. Empty for other work.",
    )
    checklist: List[str] = Field(
        default_factory=list,
        description=(
            "Short imperative items, in order. For building, the first is the smallest"
            " first step."
        ),
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


# The kinds of work a workspace is shaped for (``TaskKind`` in
# app/services/analytical/task_kind.py has the same values).
TASK_KINDS = Literal["practice", "study", "write", "build", "admin"]


class TaskWorkspace(BaseModel):
    """Proactive workspace assembled when user opens a scheduled task.

    Every field added since workspaces were first cached has a default, so a cached
    workspace built before it still loads.
    """

    task_id: str = Field(description="Task identifier")
    task_title: str = Field(description="Display title of the task")
    primary_objective: str = Field(
        description="Derived from title + topic_keywords; main learning goal",
    )
    task_kind: TASK_KINDS = Field(
        default="study",
        description=(
            "The kind of work the task is, which decides what the workspace holds: practice"
            " (problems), study (excerpts, a quiz), write (an outline), build (a step"
            " checklist), admin (a checklist of what to have ready)."
        ),
    )
    approach: str = Field(
        default="",
        description=(
            "How to spend this session on the task, in a sentence or two, when the workspace"
            " was composed for it; empty when it was built from the fixed recipe of its kind."
        ),
    )
    completion_criteria: str = Field(
        default="",
        description=(
            "What the user does to show the task is done, as it was planned; empty when the"
            " task has none (never the title again)."
        ),
    )
    duration_minutes: Optional[int] = Field(
        default=None, description="The task's planned length in minutes."
    )
    deadline_hint: Optional[str] = Field(
        default=None, description="The deadline the task was planned against, as written."
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


# --- A workspace the model composes for one task (``workspace.composer``) ----------------

BLOCK_TYPES = Literal[
    "questions", "flashcards", "outline", "checklist", "steps", "key_points",
    "worked_example", "note",
]
MAX_COMPOSED_BLOCKS = 4


class WorkspaceItem(BaseModel):
    """One line of a block: a question, a card, a heading, a step or a point."""

    text: str = Field(description="The question, card front, section heading, step or point.")
    answer: str = Field(
        default="",
        description="For a question or card: the answer. For a heading or point: one line "
        "of explanation. Otherwise empty.",
    )
    hint: str = Field(default="", description="For a question: a short nudge. Otherwise empty.")


class WorkspaceBlock(BaseModel):
    """One piece of help the model chose for the task."""

    type: BLOCK_TYPES = Field(
        description="questions: things to answer or solve, with answers. flashcards: quick "
        "recall cards. outline: sections to write. checklist: things to have or verify. "
        "steps: what to do in order, smallest first. key_points: what to hold in mind. "
        "worked_example: one example worked through, in body. note: a short explanation, "
        "in body."
    )
    title: str = Field(description="A short, specific title for this block.")
    why: str = Field(description="One short phrase: why this helps with this task.")
    items: list[WorkspaceItem] = Field(
        default_factory=list, description="The block's lines; empty for worked_example and note."
    )
    body: str = Field(
        default="", description="Markdown, for worked_example and note; otherwise empty."
    )


class WorkspaceSearch(BaseModel):
    """The one web search worth making for the task."""

    query: str = Field(description="What to search the web for, specific to this task.")
    want: Literal["video", "article", "practice_problems", "documentation", "example"] = Field(
        description="The kind of result that would help most."
    )


class WorkspaceComposition(BaseModel):
    """What the model decided this task's workspace should hold."""

    kind: Literal["practice", "study", "write", "build", "admin"] = Field(
        description="The kind of work the task is."
    )
    approach: str = Field(
        description="One or two sentences to the user: how to spend this session on this "
        "task, given its length and what you know about them."
    )
    material_is_relevant: bool = Field(
        default=True,
        description="False when the material given is not about this task; then it is not used.",
    )
    blocks: list[WorkspaceBlock] = Field(
        description="One to three blocks, the most useful first, each specific to the task."
    )
    search: WorkspaceSearch | None = Field(
        default=None,
        description="One web search that would really help, or null when none would "
        "(an errand, or the blocks are enough).",
    )
