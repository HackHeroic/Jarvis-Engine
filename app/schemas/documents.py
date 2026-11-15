"""The documents page's response models, and the task workspace's view of a document."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LinkedTask(BaseModel):
    """One of the caller's tasks a document is material for."""

    task_id: str = Field(description="The task's id.")
    title: str = Field(description="What the task is, as the plan names it.")


class DocumentOut(BaseModel):
    """One ingested document, as the documents page shows it."""

    source_id: str = Field(description="The document's id (its chunks carry it too).")
    id: Optional[str] = Field(default=None, description="The row's id.")
    user_id: Optional[str] = Field(default=None, description="The document's owner.")
    file_name: Optional[str] = Field(default=None, description="The uploaded file's name.")
    media_type: Optional[str] = Field(default=None, description="'pdf', 'png', 'jpeg'...")
    document_topics: Optional[list[str]] = Field(
        default=None, description="The topics read from it."
    )
    chunk_count: Optional[int] = Field(default=None, description="How many chunks it became.")
    created_at: Optional[str] = Field(default=None, description="When it was ingested.")
    has_file: bool = Field(
        default=False,
        description=(
            "Whether the uploaded file itself is kept, so GET /documents/{source_id}/file"
            " opens it. False for pasted text, and while the file store cannot be reached."
        ),
    )
    linked_tasks: list[LinkedTask] = Field(
        default_factory=list,
        description="The caller's tasks it is linked to, with their titles (tasks still held).",
    )
    linked_task_ids: list[str] = Field(
        default_factory=list,
        description="The ids of linked_tasks, for older clients.",
    )


class DocumentListResponse(BaseModel):
    documents: list[DocumentOut] = Field(description="The caller's documents, newest first.")


class DocumentDeleteResponse(BaseModel):
    status: Literal["deleted"] = Field(description="Always 'deleted'.")
    source_id: str = Field(
        description="The document deleted, with its chunks, its task links and its file."
    )


class DocumentChunksResponse(BaseModel):
    source_id: str = Field(description="The document.")
    chunks: list[str] = Field(description="Its extracted text chunks (the caller's only).")
    count: int = Field(ge=0, description="How many chunks.")


class LinkedDocument(BaseModel):
    """A document linked to a task, as its workspace lists it ("From your documents")."""

    source_id: str = Field(description="The document's id.")
    file_name: Optional[str] = Field(default=None, description="The uploaded file's name.")
    has_file: bool = Field(
        description="Whether its file is kept, so GET /documents/{source_id}/file opens it."
    )
