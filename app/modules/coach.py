"""Coach module — anti-guilt, progress tracking, mastery orientation."""

from app.core.jarvis_logger import JARVIS_LOGGER as logger


async def run_coaching_response(state: dict) -> dict:
    """
    Generate a coaching response based on user's current task progress.

    Anti-guilt, progress-focused coaching that emphasizes mastery and incremental improvement.
    Returns a motivational response with task completion tracking.
    """
    user_model = state.get("user_model")
    error = state.get("error")

    if error and "INFEASIBLE" in str(error):
        return {
            "response_message": (
                "This is a scope problem, not a you problem. "
                "You've got more on your plate than fits in the time available. "
                "Want to reduce scope, extend the deadline, or adjust your daily capacity?"
            ),
            "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
        }

    try:
        if user_model:
            tasks = await user_model.get_pending_tasks()
            completed = sum(1 for t in tasks if t.get("status") == "completed")
            pending = sum(1 for t in tasks if t.get("status") == "pending")
            total = len(tasks)
            return {
                "response_message": (
                    f"Here's where you stand: {completed} tasks completed, "
                    f"{pending} still pending out of {total} total. "
                    "Every task you finish is progress — keep it going!"
                ),
                "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
            }
    except Exception as e:
        logger.error(f"Coach module error: {e}")

    return {
        "response_message": "You're doing great — keep going!",
        "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
    }
