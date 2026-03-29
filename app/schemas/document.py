# app/schemas/document.py
"""Pydantic schemas for the document intelligence pipeline."""

from pydantic import BaseModel, Field


class DocumentClassification(BaseModel):
    """LLM classifies uploaded document into a registered type.

    The document_type field is a free string validated against the
    document registry at runtime — not a hardcoded Literal.
    This allows new document types to be added via registration
    without changing this schema.
    """

    document_type: str = Field(
        description="One of the registered document types from the registry"
    )

    confidence: float = Field(ge=0, le=1)

    topics_covered: list[str] = Field(
        default_factory=list,
        description="Granular topic tags: 'CNN architectures', 'backpropagation', 'Adam optimizer'",
    )

    problem_count: int | None = Field(
        default=None,
        description="Number of individual problems/questions found (if applicable)",
    )

    deadline_detected: str | None = Field(
        default=None,
        description="ISO date if a deadline is mentioned",
    )

    difficulty_estimate: float | None = Field(
        default=None, ge=0, le=1,
        description="Estimated difficulty 0-1 based on content complexity",
    )


class ExtractedProblem(BaseModel):
    """A single problem extracted from a practice problem document."""

    problem_number: int
    problem_text: str = Field(description="The actual question text")
    topic_tags: list[str] = Field(description="Knowledge component tags")
    difficulty_estimate: float = Field(ge=0, le=1, default=0.5)
    expected_time_minutes: int = Field(default=10)
    has_solution: bool = False
    solution_text: str | None = None


class ProblemSetExtraction(BaseModel):
    """Full extraction result from a practice problem document."""

    problems: list[ExtractedProblem] = Field(default_factory=list)
    overall_topics: list[str] = Field(default_factory=list)
    source_document_id: str = ""
