"""Action Item pipeline: Propose actions and handle user choice."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import SLM_ROUTER_MODEL
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
    deadline_date: Optional[str] = Field(
        default=None,
        description="ISO-8601 date (YYYY-MM-DD) when parseable from text. E.g. 'apply by March 20' -> '2026-03-20'.",
    )


ACTION_ITEM_EXTRACTION_PROMPT = """Extract structured info from this action-item text (internship, apply, pitch, task with deadline).

Return JSON: title, summary, suggested_actions (list, e.g. ["remind_after_days", "add_to_evening_schedule"]), deadline_mentioned (bool), deadline_date (str or null).

If a deadline or "apply by" is mentioned, set deadline_mentioned=true and deadline_date as YYYY-MM-DD when parseable.
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
        model_override=SLM_ROUTER_MODEL,  # 4B suffices for extraction
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
        deadline_date=data.deadline_date,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
