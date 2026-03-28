"""Intent registry — extensible intent routing for /chat endpoint.

Uses BaseRegistry from app/core/registry.py. Adding a new intent
requires only a handler function + a register() call.
"""

from app.core.registry import BaseRegistry, RegistryEntry

intent_registry = BaseRegistry[dict](name="intent", fallback_key="CHAT")


async def _handle_plan_day(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "PLAN_DAY", "status": "not_wired"}

async def _handle_edit_task(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "EDIT_TASK", "status": "not_wired"}

async def _handle_rearrange(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "REARRANGE", "status": "not_wired"}

async def _handle_add_constraint(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "ADD_CONSTRAINT", "status": "not_wired"}

async def _handle_accept_draft(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "ACCEPT_DRAFT", "status": "not_wired"}

async def _handle_reject_draft(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "REJECT_DRAFT", "status": "not_wired"}

async def _handle_ingest_document(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "INGEST_DOCUMENT", "status": "not_wired"}

async def _handle_check_progress(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "CHECK_PROGRESS", "status": "not_wired"}

async def _handle_chat(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "CHAT", "status": "not_wired"}


def register_default_intents() -> None:
    """Register all built-in intents. Called during app lifespan startup."""
    intent_registry.register(RegistryEntry(
        name="PLAN_DAY",
        description="User wants to plan their day, schedule tasks, or organize their workload",
        handler=_handle_plan_day,
        examples=["plan my day", "schedule my week", "I need to study for 3 exams"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="EDIT_TASK",
        description="User wants to modify an existing task (duration, title, deadline, priority)",
        handler=_handle_edit_task,
        examples=["change that to 15 minutes", "rename the first task", "make it easier"],
        metadata={"requires_draft": False, "triggers_replan": True},
    ))
    intent_registry.register(RegistryEntry(
        name="REARRANGE",
        description="User wants to swap task order or move a task to a different time",
        handler=_handle_rearrange,
        examples=["move DSA to afternoon", "swap tasks 2 and 3", "do math first"],
        metadata={"requires_draft": True, "triggers_replan": True},
    ))
    intent_registry.register(RegistryEntry(
        name="ADD_CONSTRAINT",
        description="User is adding a scheduling constraint, habit, or time block",
        handler=_handle_add_constraint,
        examples=["no work after 6 PM", "I have a meeting at 2", "I sleep at midnight"],
        metadata={"requires_draft": False, "triggers_replan": True},
    ))
    intent_registry.register(RegistryEntry(
        name="ACCEPT_DRAFT",
        description="User accepts the proposed schedule or draft",
        handler=_handle_accept_draft,
        examples=["looks good", "accept", "yes go with this", "perfect"],
        metadata={"requires_draft": True, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="REJECT_DRAFT",
        description="User rejects the proposed schedule and wants something different",
        handler=_handle_reject_draft,
        examples=["no that doesn't work", "reject", "start over", "this is wrong"],
        metadata={"requires_draft": True, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="INGEST_DOCUMENT",
        description="User is uploading a document, PDF, or file for processing",
        handler=_handle_ingest_document,
        examples=["process this PDF", "here's my syllabus", "upload this document"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="CHECK_PROGRESS",
        description="User wants to check their progress, completed tasks, or status",
        handler=_handle_check_progress,
        examples=["how am I doing", "what did I finish", "show my progress"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="CHAT",
        description="General conversation, questions, or anything that doesn't fit other intents",
        handler=_handle_chat,
        examples=["hello", "what can you do", "tell me about yourself"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
