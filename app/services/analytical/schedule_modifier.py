"""Schedule Modifier: parse and apply chat-based schedule modifications.

Enables users to say "make task 2 longer" or "add a reading task" without
re-running the full decomposition pipeline. Uses the 4B SLM for fast
intent parsing, then applies changes to the execution graph and re-schedules.
"""

import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.config import SLM_ROUTER_MODEL
from app.models.brain.litellm_conf import hybrid_route_query


class ScheduleModification(BaseModel):
    """Parsed modification request from user chat."""

    action: str = Field(
        description="One of: change_duration, change_time, add_task, remove_task, rename_task, reorder",
    )
    target_task_title: Optional[str] = Field(
        default=None,
        description="Fuzzy reference to existing task (title, index, or description)",
    )
    new_duration_minutes: Optional[int] = Field(
        default=None, ge=5, le=120,
        description="New duration for the target task",
    )
    new_title: Optional[str] = Field(
        default=None,
        description="New title for rename_task action",
    )
    new_start_time: Optional[str] = Field(
        default=None,
        description="Desired start time (e.g. '2 PM', '14:00')",
    )
    add_task_title: Optional[str] = Field(
        default=None,
        description="Title for a new task to add",
    )
    add_task_duration: Optional[int] = Field(
        default=None, ge=5, le=120,
        description="Duration for the new task",
    )
    add_task_difficulty: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Difficulty weight for the new task",
    )


MODIFICATION_PARSE_PROMPT = (
    "You parse schedule modification requests. The user has a draft schedule and wants to change it.\n\n"
    "Current tasks:\n{task_list}\n\n"
    "Parse the user's request into a structured modification. Actions:\n"
    "- change_duration: user wants a task longer/shorter (set new_duration_minutes)\n"
    "- change_time: user wants to move a task to a specific time (set new_start_time)\n"
    "- add_task: user wants to add a new task (set add_task_title, add_task_duration, add_task_difficulty)\n"
    "- remove_task: user wants to remove a task (set target_task_title)\n"
    "- rename_task: user wants to rename a task (set target_task_title, new_title)\n"
    "- reorder: user wants tasks in a different order (set target_task_title)\n\n"
    "Match target_task_title to the closest task from the list above. Use the exact title from the list.\n"
    "Return strictly valid JSON."
)


async def parse_modification_request(
    user_prompt: str,
    task_list: list[dict[str, Any]],
) -> Optional[ScheduleModification]:
    """Use 4B SLM to parse user intent into a structured modification.

    Returns None if parsing fails (caller should fall back to full pipeline).
    """
    task_summary = "\n".join(
        f"  {i+1}. \"{t.get('title', t.get('task_id', 'unknown'))}\" — {t.get('duration_minutes', '?')} min, difficulty {t.get('difficulty_weight', '?')}"
        for i, t in enumerate(task_list)
    )
    system_prompt = MODIFICATION_PARSE_PROMPT.format(task_list=task_summary)

    try:
        result = await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            response_schema=ScheduleModification,
            model_override=SLM_ROUTER_MODEL,
        )
        if isinstance(result, dict):
            return ScheduleModification.model_validate(result)
        return ScheduleModification.model_validate_json(result)
    except Exception as e:
        print(f"[ScheduleModifier] Parse failed: {e}")
        return None


def fuzzy_match_task(
    query: str,
    tasks: list[dict[str, Any]],
) -> Optional[str]:
    """Match a fuzzy task reference to an actual task_id.

    Handles: exact title, substring, "task N" / "the Nth task", ordinal references.
    """
    if not query or not tasks:
        return None

    query_lower = query.lower().strip()

    # Try exact title match first
    for t in tasks:
        title = t.get("title", "").lower()
        if title == query_lower:
            return t.get("task_id")

    # Try substring match
    for t in tasks:
        title = t.get("title", "").lower()
        if query_lower in title or title in query_lower:
            return t.get("task_id")

    # Try index-based references: "task 2", "the second task", "2nd task"
    ordinals = {
        "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
        "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "sixth": 5, "6th": 5,
        "seventh": 6, "7th": 6, "eighth": 7, "8th": 7, "ninth": 8, "9th": 8,
        "tenth": 9, "10th": 9,
    }
    for word, idx in ordinals.items():
        if word in query_lower and idx < len(tasks):
            return tasks[idx].get("task_id")

    # Try "task N" pattern
    match = re.search(r"task\s*(\d+)", query_lower)
    if match:
        idx = int(match.group(1)) - 1  # 1-indexed
        if 0 <= idx < len(tasks):
            return tasks[idx].get("task_id")

    # Try simple number
    match = re.match(r"^(\d+)$", query_lower)
    if match:
        idx = int(match.group(1)) - 1
        if 0 <= idx < len(tasks):
            return tasks[idx].get("task_id")

    return None


def apply_modification(
    modification: ScheduleModification,
    execution_graph: dict[str, Any],
    schedule_data: dict[str, Any],
    time_slots: list,
    horizon_start: Any,
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Apply parsed modification to execution graph, then re-schedule.

    Returns (modified_execution_graph, modified_schedule_or_None).
    For rename_task, no re-scheduling is needed.
    For everything else, calls run_schedule() to recompute.
    """
    from app.api.v1.endpoints.reasoning import ExecutionGraph, TaskChunk
    from app.api.v1.endpoints.schedule import run_schedule

    decomposition = execution_graph.get("decomposition", [])
    action = modification.action

    if action == "rename_task" and modification.target_task_title and modification.new_title:
        task_id = fuzzy_match_task(modification.target_task_title, decomposition)
        if task_id:
            for t in decomposition:
                if t.get("task_id") == task_id:
                    t["title"] = modification.new_title
                    break
            # Update title in schedule too
            sched = schedule_data.get("schedule", {})
            if task_id in sched:
                sched[task_id]["title"] = modification.new_title
            execution_graph["decomposition"] = decomposition
            return execution_graph, schedule_data

    if action == "change_duration" and modification.target_task_title and modification.new_duration_minutes:
        task_id = fuzzy_match_task(modification.target_task_title, decomposition)
        if task_id:
            for t in decomposition:
                if t.get("task_id") == task_id:
                    t["duration_minutes"] = min(60, max(1, modification.new_duration_minutes))
                    break

    elif action == "remove_task" and modification.target_task_title:
        task_id = fuzzy_match_task(modification.target_task_title, decomposition)
        if task_id:
            decomposition = [t for t in decomposition if t.get("task_id") != task_id]
            # Remove from other tasks' dependencies
            for t in decomposition:
                deps = t.get("dependencies", [])
                t["dependencies"] = [d for d in deps if d != task_id]

    elif action == "add_task":
        new_id = f"added_{len(decomposition) + 1}"
        new_task = {
            "task_id": new_id,
            "title": modification.add_task_title or "New task",
            "duration_minutes": min(60, max(1, modification.add_task_duration or 15)),
            "difficulty_weight": modification.add_task_difficulty or 0.5,
            "dependencies": [],
            "completion_criteria": None,
            "implementation_intention": None,
            "deadline_hint": None,
        }
        decomposition.append(new_task)

    execution_graph["decomposition"] = decomposition

    # Re-schedule with updated tasks
    try:
        chunks = []
        for t in decomposition:
            chunks.append(TaskChunk(
                task_id=t.get("task_id", "unknown"),
                title=t.get("title", "Task"),
                duration_minutes=t.get("duration_minutes", 15),
                difficulty_weight=t.get("difficulty_weight", 0.5),
                dependencies=t.get("dependencies", []),
                completion_criteria=t.get("completion_criteria"),
                implementation_intention=t.get("implementation_intention"),
                deadline_hint=t.get("deadline_hint"),
            ))

        avg_load = sum(c.difficulty_weight for c in chunks) / max(len(chunks), 1)
        graph = ExecutionGraph(
            goal_metadata=execution_graph.get("goal_metadata"),
            decomposition=chunks,
            cognitive_load_estimate=execution_graph.get("cognitive_load_estimate", {"intrinsic_load": round(avg_load, 3)}),
        )

        from app.core.config import DEFAULT_HORIZON_MINUTES, MAX_HORIZON_MINUTES
        horizon_steps = [DEFAULT_HORIZON_MINUTES, 4320, 7200, 10080]
        new_schedule = None
        for horizon_min in horizon_steps:
            if horizon_min > MAX_HORIZON_MINUTES:
                break
            try:
                new_schedule = run_schedule(
                    graph,
                    time_slots,
                    horizon_minutes=horizon_min,
                    horizon_start=horizon_start,
                )
                break
            except Exception:
                continue

        if new_schedule:
            return (
                graph.model_dump(mode="json"),
                new_schedule.model_dump(mode="json"),
            )
    except Exception as e:
        print(f"[ScheduleModifier] Re-schedule failed: {e}")

    # Return updated graph with original schedule if re-scheduling failed
    return execution_graph, schedule_data
