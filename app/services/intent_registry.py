"""Intent registry — extensible intent routing for /chat endpoint.

Uses BaseRegistry from app/core/registry.py. Adding a new intent
requires only a handler function + a register() call.

Each handler receives an IntentContext and returns a ChatResponse (as dict).
Lazy imports inside handler bodies to avoid circular dependencies.
"""

from __future__ import annotations

from typing import Any

from app.core.registry import BaseRegistry, RegistryEntry

intent_registry = BaseRegistry[dict](name="intent", fallback_key="CHAT")


# ---------------------------------------------------------------------------
# Handlers — each delegates to existing business logic
# ---------------------------------------------------------------------------


async def _handle_plan_day(ctx: Any) -> dict:
    """Delegate to _run_plan_day_flow from control_policy."""
    from app.services.analytical.control_policy import _run_plan_day_flow

    planning_goal = ctx.user_prompt
    if ctx.brain_dump and getattr(ctx.brain_dump, "planning_goal", None):
        planning_goal = ctx.brain_dump.planning_goal

    response = await _run_plan_day_flow(
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        db_client=ctx.db_client,
        planning_goal=planning_goal,
        state_updates=getattr(ctx.brain_dump, "state_updates", None) if ctx.brain_dump else None,
        use_voice_synthesis=True,
        progress_callback=ctx.progress_callback,
        draft_store=ctx.draft_store,
        memory_store=ctx.memory_store,
        **{k: v for k, v in ctx.extra.items() if k in (
            "day_start_hour_override",
            "max_daily_deep_work_minutes",
            "min_daily_deep_work_minutes",
            "max_task_duration_minutes",
            "min_task_duration_minutes",
            "deadline_override",
            "skip_scheduling",
        )},
    )
    return response.model_dump() if hasattr(response, "model_dump") else response


async def _handle_greeting(ctx: Any) -> dict:
    """Short greetings — synthesize a warm Jarvis response."""
    from app.schemas.context import IntentType
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

    message, thinking_process = await synthesize_jarvis_response({"greeting": True})
    return {
        "intent": IntentType.GREETING.value,
        "message": message,
        "thinking_process": thinking_process,
    }


async def _handle_general_qa(ctx: Any) -> dict:
    """Knowledge questions — direct QA via 27B."""
    from app.services.analytical.control_policy import _direct_qa_response

    response = await _direct_qa_response(
        ctx.user_prompt,
        progress_callback=ctx.progress_callback,
        conversation_history=ctx.conversation_history or None,
    )
    return response.model_dump() if hasattr(response, "model_dump") else response


async def _handle_chat(ctx: Any) -> dict:
    """General conversation fallback — same path as GENERAL_QA."""
    return await _handle_general_qa(ctx)


async def _handle_ingest_document(ctx: Any) -> dict:
    """Document / knowledge ingestion via orchestrator."""
    return await _ingestion_handler(ctx, "KNOWLEDGE_INGESTION")


async def _handle_calendar_sync(ctx: Any) -> dict:
    """Calendar / timetable extraction via orchestrator."""
    return await _ingestion_handler(ctx, "CALENDAR_SYNC")


async def _handle_behavioral_constraint(ctx: Any) -> dict:
    """Store a behavioral constraint / habit."""
    return await _ingestion_handler(ctx, "BEHAVIORAL_CONSTRAINT")


async def _handle_action_item(ctx: Any) -> dict:
    """Record an action item."""
    return await _ingestion_handler(ctx, "ACTION_ITEM")


async def _ingestion_handler(ctx: Any, intent_name: str) -> dict:
    """Shared ingestion logic for CALENDAR_SYNC, KNOWLEDGE_INGESTION, etc."""
    from app.schemas.context import IntentType
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    from app.services.extraction.orchestrator import process_ingestion

    intent_type = IntentType(intent_name)

    result = await process_ingestion(
        payload=ctx.user_prompt,
        user_id=ctx.user_id,
        db_client=ctx.db_client,
        intent_override=intent_type,
    )

    ingestion_messages = {
        IntentType.CALENDAR_SYNC: "Extracted your timetable. Review pending calendar updates to approve.",
        IntentType.KNOWLEDGE_INGESTION: "Saved your materials to knowledge base.",
        IntentType.BEHAVIORAL_CONSTRAINT: "Got it, I've noted your preference. Your schedule constraints have been updated.",
        IntentType.ACTION_ITEM: "Recorded your action item. You can schedule it when ready.",
    }

    execution_summary: dict[str, Any] = {}
    if result.calendar_result:
        execution_summary["calendar_extracted"] = True
        execution_summary["needs_end_date"] = getattr(result.calendar_result, "needs_end_date", False)
    if result.action_proposal:
        execution_summary["action_proposal"] = result.action_proposal.model_dump()
    if intent_name == "KNOWLEDGE_INGESTION" and result.knowledge_result:
        execution_summary["knowledge_stored"] = True

    if execution_summary:
        message, thinking_process = await synthesize_jarvis_response(execution_summary)
    else:
        message = ingestion_messages.get(intent_type, "Saved.")
        thinking_process = "I processed your input and saved it to your profile."

    suggested = "replan" if intent_type == IntentType.BEHAVIORAL_CONSTRAINT else None
    return {
        "intent": intent_type.value,
        "message": message,
        "ingestion_result": result.model_dump(),
        "suggested_action": suggested,
        "thinking_process": thinking_process,
    }


async def _handle_edit_task(ctx: Any) -> dict:
    """Edit an existing task — parse request, find task, apply change."""
    from app.services.analytical.task_editor import handle_edit_task

    return await handle_edit_task(
        user_id=ctx.user_id,
        user_prompt=ctx.user_prompt,
        draft_store=ctx.draft_store,
        db_client=ctx.db_client,
        memory_store=ctx.memory_store,
    )


async def _handle_rearrange(ctx: Any) -> dict:
    """Rearrange task order — parse request, fetch tasks, swap positions."""
    from app.services.analytical.task_rearranger import handle_rearrange

    return await handle_rearrange(
        user_id=ctx.user_id,
        user_prompt=ctx.user_prompt,
        draft_store=ctx.draft_store,
        db_client=ctx.db_client,
        memory_store=ctx.memory_store,
    )


async def _handle_check_progress(ctx: Any) -> dict:
    """Check progress — minimal VoJ response for now."""
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

    message, thinking_process = await synthesize_jarvis_response(
        {"check_progress": True, "user_request": ctx.user_prompt}
    )
    return {
        "intent": "CHECK_PROGRESS",
        "message": message,
        "thinking_process": thinking_process,
    }


async def _handle_accept_draft(ctx: Any) -> dict:
    """Accept the current draft schedule."""
    draft_store = ctx.draft_store
    if draft_store and hasattr(draft_store, "accept"):
        accepted = await draft_store.accept(ctx.user_id)
        if accepted:
            return {
                "intent": "ACCEPT_DRAFT",
                "message": "Schedule accepted and saved. Let's get to work!",
                "schedule_status": "accepted",
            }
    return {
        "intent": "ACCEPT_DRAFT",
        "message": "No pending draft to accept. Try planning your day first.",
    }


async def _handle_reject_draft(ctx: Any) -> dict:
    """Reject the current draft schedule."""
    draft_store = ctx.draft_store
    if draft_store and hasattr(draft_store, "reject"):
        await draft_store.reject(ctx.user_id)

    # Store rejection reason as feedback memory for PEARL
    if ctx.memory_store:
        try:
            await ctx.memory_store.store_memory(
                user_id=ctx.user_id,
                memory_type="feedback",
                content=f"User rejected schedule draft: {ctx.user_prompt}",
                confidence=0.5,
            )
        except Exception:
            pass  # Non-blocking

    return {
        "intent": "REJECT_DRAFT",
        "message": "Draft rejected. Tell me what you'd like to change and I'll re-plan.",
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


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
        name="GREETING",
        description="Short greetings, chitchat, non-actionable messages",
        handler=_handle_greeting,
        examples=["hello", "hey Jarvis", "good morning"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="GENERAL_QA",
        description="Questions, explanations, knowledge queries (not planning)",
        handler=_handle_general_qa,
        examples=["what is dynamic programming", "explain WOOP", "how does SM-2 work"],
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
        handler=_handle_behavioral_constraint,
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
        name="CALENDAR_SYNC",
        description="User is sharing a timetable or calendar for extraction",
        handler=_handle_calendar_sync,
        examples=["here's my class schedule", "I have meetings at these times"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="KNOWLEDGE_INGESTION",
        description="User is sharing knowledge content (syllabus, notes, text) for storage",
        handler=_handle_ingest_document,
        examples=["here's my syllabus", "save these notes", "add this to my knowledge base"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
    intent_registry.register(RegistryEntry(
        name="BEHAVIORAL_CONSTRAINT",
        description="User is stating a habit, preference, or scheduling constraint",
        handler=_handle_behavioral_constraint,
        examples=["I never study before 11 AM", "I sleep by midnight", "no heavy work on weekends"],
        metadata={"requires_draft": False, "triggers_replan": True},
    ))
    intent_registry.register(RegistryEntry(
        name="ACTION_ITEM",
        description="User has a specific task or reminder to record",
        handler=_handle_action_item,
        examples=["remind me to call mom", "I need to submit my assignment", "add a task"],
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
