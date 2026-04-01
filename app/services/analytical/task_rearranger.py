"""Task rearranger — parses a reorder request via LLM and swaps scheduled positions.

Uses hybrid_route_query (prefer_local=True) to interpret the user's desired
new task order, then reassigns scheduled_start values accordingly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.models.brain.litellm_conf import hybrid_route_query

logger = logging.getLogger(__name__)

_REARRANGE_SYSTEM_PROMPT = """\
You are a JSON extractor. The user has a numbered list of tasks and wants to rearrange them.

Here are their current tasks (in scheduled order):
{task_list}

The user said: "{user_request}"

Return ONLY a JSON array of 1-indexed integers representing the new order.
For example, if there are 4 tasks and the user wants to swap tasks 2 and 3:
[1, 3, 2, 4]

If the user says "do math first" and math is task 3 out of 4:
[3, 1, 2, 4]

Return ONLY the JSON array. No explanation."""


async def handle_rearrange(
    user_id: str,
    user_prompt: str,
    draft_store: Any,
    db_client: Any,
    memory_store: Any,
) -> dict:
    """Parse a rearrange request and swap scheduled_start values.

    Returns a dict with ``intent`` and ``message`` keys.
    """
    # ------------------------------------------------------------------
    # 1. Fetch pending tasks ordered by scheduled_start
    # ------------------------------------------------------------------
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        return {
            "intent": "REARRANGE",
            "message": "I can't rearrange tasks right now — database unavailable.",
        }
    try:
        result = await asyncio.to_thread(
            lambda sb=supabase, uid=user_id: sb.table("user_tasks")
            .select("*")
            .eq("user_id", uid)
            .eq("status", "pending")
            .order("scheduled_start", desc=False)
            .execute()
        )
        tasks = result.data or []
    except Exception as exc:
        logger.error("REARRANGE: failed to fetch tasks: %s", exc)
        return {
            "intent": "REARRANGE",
            "message": "Something went wrong fetching your tasks. Please try again.",
        }

    # ------------------------------------------------------------------
    # 2. Validate we have enough tasks to rearrange
    # ------------------------------------------------------------------
    if len(tasks) < 2:
        return {
            "intent": "REARRANGE",
            "message": "You need at least 2 pending tasks to rearrange. Try planning your day first.",
        }

    # Filter out tasks without a scheduled_start — they can't be rearranged
    scheduled_tasks = [t for t in tasks if t.get("scheduled_start") is not None]
    unscheduled_count = len(tasks) - len(scheduled_tasks)

    if len(scheduled_tasks) < 2:
        return {
            "intent": "REARRANGE",
            "message": (
                "Not enough tasks have scheduled times to rearrange. "
                "Try planning your day first so tasks get assigned time slots."
            ),
        }

    # ------------------------------------------------------------------
    # 3. Build numbered list and ask LLM for new order
    # ------------------------------------------------------------------
    task_list_str = "\n".join(
        f"{i + 1}. {t.get('title', 'Untitled')} (scheduled: {t['scheduled_start']})"
        for i, t in enumerate(scheduled_tasks)
    )

    system_prompt = _REARRANGE_SYSTEM_PROMPT.format(
        task_list=task_list_str,
        user_request=user_prompt,
    )

    try:
        raw_response = await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            prefer_local=True,
        )
        raw_text = raw_response if isinstance(raw_response, str) else str(raw_response)
        clean = re.sub(r"```json|```", "", raw_text).strip()
        new_order: list[int] = json.loads(clean)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("REARRANGE: failed to parse LLM output: %s", exc)
        return {
            "intent": "REARRANGE",
            "message": "Sorry, I couldn't understand your rearrangement request. Could you rephrase?",
        }

    # ------------------------------------------------------------------
    # 4. Validate the returned order
    # ------------------------------------------------------------------
    n = len(scheduled_tasks)
    if (
        not isinstance(new_order, list)
        or len(new_order) != n
        or sorted(new_order) != list(range(1, n + 1))
    ):
        logger.warning(
            "REARRANGE: invalid order array %s for %d tasks", new_order, n
        )
        return {
            "intent": "REARRANGE",
            "message": (
                f"I got a reorder that doesn't match your {n} tasks. "
                "Could you try again with something like 'swap tasks 1 and 3'?"
            ),
        }

    # ------------------------------------------------------------------
    # 5. Collect existing scheduled_start values and reassign
    # ------------------------------------------------------------------
    existing_starts = sorted(
        t["scheduled_start"] for t in scheduled_tasks
    )

    # new_order is 1-indexed: new_order[i] means "put task at original
    # position new_order[i]-1 into slot i"
    updates: list[tuple[str, str, str, str]] = []  # (task_id, user_id, new_start, new_end)
    for slot_idx, orig_pos_1based in enumerate(new_order):
        task = scheduled_tasks[orig_pos_1based - 1]
        new_start = existing_starts[slot_idx]
        # Recompute scheduled_end from duration (more correct than swapping end times)
        duration = task.get("duration_minutes", 25)
        try:
            from datetime import datetime as _dt, timedelta
            start_dt = _dt.fromisoformat(new_start.replace("Z", "+00:00"))
            new_end = (start_dt + timedelta(minutes=duration)).isoformat()
        except Exception:
            new_end = new_start  # Fallback: at least don't crash
        if task["scheduled_start"] != new_start:
            updates.append((task["task_id"], user_id, new_start, new_end))

    # ------------------------------------------------------------------
    # 6. Apply updates in DB
    # ------------------------------------------------------------------
    try:
        for task_id, uid, new_start, new_end in updates:
            await asyncio.to_thread(
                lambda tid=task_id, u=uid, ns=new_start, ne=new_end: supabase.table("user_tasks")
                .update({"scheduled_start": ns, "scheduled_end": ne})
                .eq("task_id", tid)
                .eq("user_id", u)
                .execute()
            )
    except Exception as exc:
        logger.error("REARRANGE: failed to update tasks: %s", exc)
        return {
            "intent": "REARRANGE",
            "message": "Something went wrong saving the new order. Please try again.",
        }

    # ------------------------------------------------------------------
    # 7. Build response with new order
    # ------------------------------------------------------------------
    reordered_names = [
        scheduled_tasks[orig_pos - 1].get("title", "Untitled")
        for orig_pos in new_order
    ]
    order_summary = "\n".join(
        f"{i + 1}. {name}" for i, name in enumerate(reordered_names)
    )

    unscheduled_note = ""
    if unscheduled_count > 0:
        unscheduled_note = (
            f"\n\nNote: {unscheduled_count} task(s) without scheduled times were left unchanged."
        )

    return {
        "intent": "REARRANGE",
        "message": f"Done — here's your new task order:\n{order_summary}{unscheduled_note}",
    }
