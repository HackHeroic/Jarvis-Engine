"""Task editor — parses an edit request via LLM and applies it to a pending task.

Uses hybrid_route_query (prefer_local=True) to extract the edit intent,
then applies the change directly in Supabase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.models.brain.litellm_conf import hybrid_route_query

logger = logging.getLogger(__name__)

_EDIT_PARSE_SYSTEM_PROMPT = """\
You are a JSON extractor. The user wants to edit one of their tasks.
Return ONLY a JSON object with these fields:
- "task_keyword": a word or short phrase that identifies the task (from the task title)
- "field": one of "duration_minutes", "title", "difficulty_weight", "remove"
- "new_value": the new value (string for title, number for duration_minutes/difficulty_weight, null for remove)

Examples:
User: "change math to 15 minutes" -> {"task_keyword": "math", "field": "duration_minutes", "new_value": 15}
User: "rename DSA task to Algorithms" -> {"task_keyword": "DSA", "field": "title", "new_value": "Algorithms"}
User: "remove the reading task" -> {"task_keyword": "reading", "field": "remove", "new_value": null}
User: "make physics easier" -> {"task_keyword": "physics", "field": "difficulty_weight", "new_value": 0.3}

Return ONLY the JSON object. No explanation."""

_EDITABLE_FIELDS = {"duration_minutes", "title", "difficulty_weight", "remove"}


async def handle_edit_task(
    user_id: str,
    user_prompt: str,
    draft_store: Any,
    db_client: Any,
    memory_store: Any,
) -> dict:
    """Parse an edit request and apply it to the matching pending task.

    Returns a dict with ``intent`` and ``message`` keys.
    """
    # ------------------------------------------------------------------
    # 1. Ask the LLM to parse the edit request
    # ------------------------------------------------------------------
    try:
        raw_response = await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=_EDIT_PARSE_SYSTEM_PROMPT,
            prefer_local=True,
        )
        # raw_response may be a string or an object with a text attribute
        raw_text = raw_response if isinstance(raw_response, str) else str(raw_response)
        clean = re.sub(r"```json|```", "", raw_text).strip()
        parsed: dict = json.loads(clean)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("EDIT_TASK: failed to parse LLM output: %s", exc)
        return {
            "intent": "EDIT_TASK",
            "message": "Sorry, I couldn't understand which task to edit. Could you rephrase?",
        }

    task_keyword: str = parsed.get("task_keyword", "")
    field: str = parsed.get("field", "")
    new_value = parsed.get("new_value")

    if not task_keyword or field not in _EDITABLE_FIELDS:
        return {
            "intent": "EDIT_TASK",
            "message": (
                "I couldn't determine what to change. "
                "Try something like 'change math to 20 minutes' or 'remove the reading task'."
            ),
        }

    # ------------------------------------------------------------------
    # 2. Fetch pending tasks for this user
    # ------------------------------------------------------------------
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        return {
            "intent": "EDIT_TASK",
            "message": "I can't edit tasks right now — database unavailable.",
        }
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("user_tasks")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .execute()
        )
        tasks = result.data or []
    except Exception as exc:
        logger.error("EDIT_TASK: failed to fetch tasks: %s", exc)
        return {
            "intent": "EDIT_TASK",
            "message": "Something went wrong fetching your tasks. Please try again.",
        }

    # ------------------------------------------------------------------
    # 3. Find the matching task (case-insensitive keyword in title)
    # ------------------------------------------------------------------
    keyword_lower = task_keyword.lower()
    matched_task = None
    for task in tasks:
        if keyword_lower in (task.get("title") or "").lower():
            matched_task = task
            break

    if matched_task is None:
        return {
            "intent": "EDIT_TASK",
            "message": f"I couldn't find a pending task matching '{task_keyword}'. Check your task list and try again.",
        }

    task_id = matched_task["task_id"]
    task_title = matched_task.get("title", task_keyword)

    # ------------------------------------------------------------------
    # 4. Apply the edit
    # ------------------------------------------------------------------
    try:
        if field == "remove":
            await asyncio.to_thread(
                lambda: supabase.table("user_tasks")
                .delete()
                .eq("task_id", task_id)
                .eq("user_id", user_id)
                .execute()
            )
            return {
                "intent": "EDIT_TASK",
                "message": f"Done — I've removed '{task_title}' from your schedule.",
            }

        # Build the update payload
        if field == "duration_minutes":
            update_val = int(new_value)
        elif field == "difficulty_weight":
            update_val = float(new_value)
        else:  # title
            update_val = str(new_value)

        await asyncio.to_thread(
            lambda: supabase.table("user_tasks")
            .update({field: update_val})
            .eq("task_id", task_id)
            .eq("user_id", user_id)
            .execute()
        )

        friendly_field = field.replace("_", " ")
        return {
            "intent": "EDIT_TASK",
            "message": f"Updated '{task_title}' — {friendly_field} is now {update_val}.",
        }

    except (ValueError, TypeError) as exc:
        logger.warning("EDIT_TASK: bad value for %s: %s", field, exc)
        return {
            "intent": "EDIT_TASK",
            "message": f"The value '{new_value}' doesn't look right for {field}. Could you try again?",
        }
    except Exception as exc:
        logger.error("EDIT_TASK: failed to update task: %s", exc)
        return {
            "intent": "EDIT_TASK",
            "message": "Something went wrong saving the edit. Please try again.",
        }
