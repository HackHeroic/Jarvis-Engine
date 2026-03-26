"""Task workspace schemas for Proactive Cognitive Workspace."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

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
