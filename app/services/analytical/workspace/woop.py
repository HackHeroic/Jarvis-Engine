"""The workspace's WOOP card: the task's if-then plan, with its goal's wish and outcome.

All four stages (Wish, Outcome, Obstacle, Plan) or none: a task without both halves of
an if-then plan has no card. The wish and outcome come from the goal's newest planning
context (``GoalRepository.latest_context``); without one the wish is the task's title.
"""

import json
import logging

from app.domain.entities import Task
from app.domain.ports import GoalRepository
from app.schemas.workspace import WoopCard

logger = logging.getLogger(__name__)


def _as_dict(value: object) -> dict:
    """A JSON object stored as an object or as text; anything else is empty."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


async def woop_card(
    user_id: str, task: Task, title: str, goals: GoalRepository | None
) -> WoopCard | None:
    """The task's WOOP card, or None when it has no complete if-then plan.

    Best effort on the goal: a goal store that fails leaves the wish at ``title`` and the
    outcome empty (logged by the exception's type).
    """
    intention = task.implementation_intention or {}
    obstacle = str(intention.get("obstacle_trigger") or "").strip()
    plan = str(intention.get("behavioral_response") or "").strip()
    if not obstacle or not plan:
        return None
    wish, outcome = title, ""
    if task.goal_id and goals is not None:
        try:
            snippet = _as_dict(await goals.latest_context(user_id, task.goal_id))
        except Exception as exc:
            logger.warning("The goal of task %s could not be read (%s)", task.task_id, type(exc).__name__)
            snippet = {}
        wish = str(snippet.get("planning_goal") or title)
        outcome = str(_as_dict(snippet.get("goal_metadata")).get("outcome_visualization") or "")
    return WoopCard(wish=wish, outcome=outcome, obstacle=obstacle, plan=plan)
