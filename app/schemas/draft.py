# app/schemas/draft.py
"""Schemas for draft review/accept/reject API."""

from typing import Any, List, Optional
from pydantic import BaseModel, Field


class DraftComponentResponse(BaseModel):
    """A single reviewable component in a draft."""

    component_type: str = Field(description="habits, tasks, schedule, action_items, materials")
    data: Any = Field(description="Component-specific data")
    status: str = Field(description="pending, accepted, rejected, modified")


class DraftResponse(BaseModel):
    """Full draft state returned to frontend."""

    draft_id: str
    user_id: str
    components: dict[str, DraftComponentResponse] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DraftAcceptRequest(BaseModel):
    """Accept specific components of a draft."""

    user_id: str = Field(..., description="User identifier")
    components: Optional[List[str]] = Field(
        default=None,
        description="Component keys to accept. None = accept all pending.",
    )


class DraftRejectRequest(BaseModel):
    """Reject specific components of a draft."""

    user_id: str = Field(..., description="User identifier")
    components: List[str] = Field(..., description="Component keys to reject")


class DraftModifyRequest(BaseModel):
    """Modify a specific component's data."""

    user_id: str = Field(..., description="User identifier")
    component: str = Field(..., description="Component key to modify")
    data: Any = Field(..., description="Updated component data")
