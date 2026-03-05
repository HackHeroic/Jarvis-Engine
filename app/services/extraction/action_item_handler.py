"""Action Item pipeline: Propose actions and handle user choice."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.context import ActionItemProposal
from pydantic import BaseModel, Field


class ActionItemExtraction(BaseModel):
    """LLM-extracted structure for action items."""

    title: str = Field(description="Short title")
    summary: str = Field(description="Brief description")
    suggested_actions: list[str] = Field(
        default_factory=lambda: ["remind_after_days", "add_to_evening_schedule"],
        description="e.g. remind_after_days, add_to_evening_schedule",
    )
    deadline_mentioned: bool = False


ACTION_ITEM_EXTRACTION_PROMPT = """Extract structured info from this action-item text (internship, apply, pitch, task with deadline).

Return JSON: title, summary, suggested_actions (list, e.g. ["remind_after_days", "add_to_evening_schedule"]), deadline_mentioned (bool).

If a deadline or "apply by" is mentioned, set deadline_mentioned=true.
"""


async def propose_action_item(extracted_text: str) -> ActionItemProposal:
    """Extract action item structure and return a proposal for user choice.

    Args:
        extracted_text: Raw text (e.g. internship email, Slack message).

    Returns:
        ActionItemProposal with id, title, summary, suggested_actions.
    """
    result = await hybrid_route_query(
        user_prompt=extracted_text,
        system_prompt=ACTION_ITEM_EXTRACTION_PROMPT,
        response_schema=ActionItemExtraction,
        model_override=None,  # Use 27B
    )

    if isinstance(result, dict):
        data = ActionItemExtraction.model_validate(result)
    else:
        data = ActionItemExtraction.model_validate_json(result)

    return ActionItemProposal(
        id=str(uuid.uuid4()),
        title=data.title,
        summary=data.summary,
        suggested_actions=data.suggested_actions,
        deadline_mentioned=data.deadline_mentioned,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
