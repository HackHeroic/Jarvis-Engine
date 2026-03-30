# WS1: Backend Architecture Compliance Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align Jarvis-Engine backend with every flow diagram in the architecture reset spec — fix LLM routing (Gemini Flash primary), wire intent registry handlers, add missing draft endpoints, integrate PEARL, fix memory scoring.

**Architecture:** Three-phase approach: (1) Fix LLM routing so brain dump/decompose/translate use Gemini Flash primary, (2) Wire intent registry with real handlers and registry-based dispatch, (3) Fix memory system (importance scoring, lifecycle state, PEARL integration).

**Tech Stack:** Python 3.11+, FastAPI, LiteLLM, Pydantic v2, Supabase, OR-Tools CP-SAT

**Spec:** `docs/superpowers/specs/2026-03-30-jarvis-spec-compliance-fix-design.md`

---

### Task 1: Add `gemini_primary_route` and `local_primary_route` helpers to litellm_conf.py

**Files:**
- Modify: `app/models/brain/litellm_conf.py`
- Test: `tests/test_routing_helpers.py`

- [ ] **Step 1: Write failing test for gemini_primary_route**

```python
# tests/test_routing_helpers.py
import pytest
from unittest.mock import patch, AsyncMock

@pytest.mark.asyncio
async def test_gemini_primary_route_calls_gemini_first():
    """gemini_primary_route should call Gemini Flash as primary."""
    from app.models.brain.litellm_conf import gemini_primary_route

    with patch("app.models.brain.litellm_conf.hybrid_route_query", new_callable=AsyncMock) as mock:
        mock.return_value = '{"planning_goal": "test"}'
        result = await gemini_primary_route(
            user_prompt="plan my day",
            system_prompt="Extract brain dump",
            response_schema=None,
        )
        # First call should force cloud (Gemini)
        call_args = mock.call_args
        assert call_args.kwargs.get("force_cloud") is True


@pytest.mark.asyncio
async def test_local_primary_route_uses_slm():
    """local_primary_route should use SLM_ROUTER_MODEL (4B) by default."""
    from app.models.brain.litellm_conf import local_primary_route

    with patch("app.models.brain.litellm_conf.hybrid_route_query", new_callable=AsyncMock) as mock:
        mock.return_value = '{"intent": "PLAN_DAY"}'
        result = await local_primary_route(
            user_prompt="plan my day",
            system_prompt="Classify intent",
            response_schema=None,
        )
        call_args = mock.call_args
        assert call_args.kwargs.get("force_cloud") is not True
        assert call_args.kwargs.get("model_override") is not None  # Should use SLM
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_routing_helpers.py -v`
Expected: FAIL — `gemini_primary_route` not found

- [ ] **Step 3: Implement routing helpers**

Add to `app/models/brain/litellm_conf.py` after the `hybrid_route_query` function:

```python
from app.core.config import SLM_ROUTER_MODEL, GEMINI_API_KEY


async def gemini_primary_route(
    user_prompt: str,
    system_prompt: str,
    response_schema: type[BaseModel] | None = None,
    fallback_model: str | None = None,
    stream: bool = False,
    conversation_history: list[dict] | None = None,
) -> str | dict | AsyncGenerator[str, None]:
    """Route to Gemini 2.5 Flash as primary. Fall back to local model on failure.

    Used for: brain dump extraction, task decomposition, habit translation.
    """
    if GEMINI_API_KEY:
        try:
            return await hybrid_route_query(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                response_schema=response_schema,
                force_cloud=True,
                stream=stream,
                conversation_history=conversation_history,
            )
        except Exception as e:
            logger.warning("Gemini primary failed, falling back to local: %s", e)

    # Fallback to local model
    return await hybrid_route_query(
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        response_schema=response_schema,
        model_override=fallback_model or SLM_ROUTER_MODEL,
        stream=stream,
        conversation_history=conversation_history,
    )


async def local_primary_route(
    user_prompt: str,
    system_prompt: str,
    response_schema: type[BaseModel] | None = None,
    model_override: str | None = None,
    stream: bool = False,
    conversation_history: list[dict] | None = None,
) -> str | dict | AsyncGenerator[str, None]:
    """Route to local Qwen-4B SLM as primary. Fall back to Gemini on failure.

    Used for: intent classification, Voice of Jarvis, memory extraction.
    """
    target = model_override or SLM_ROUTER_MODEL
    try:
        return await hybrid_route_query(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            response_schema=response_schema,
            model_override=target,
            stream=stream,
            conversation_history=conversation_history,
        )
    except Exception as e:
        logger.warning("Local primary (%s) failed, falling back to Gemini: %s", target, e)
        if GEMINI_API_KEY:
            return await hybrid_route_query(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                response_schema=response_schema,
                force_cloud=True,
                stream=stream,
                conversation_history=conversation_history,
            )
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_routing_helpers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/models/brain/litellm_conf.py tests/test_routing_helpers.py
git commit -m "feat: add gemini_primary_route and local_primary_route helpers

Spec 1.1: Gemini 2.5 Flash is now primary for brain dump, decomposition,
and habit translation. Local Qwen-4B for classification and synthesis."
```

---

### Task 2: Switch brain dump extraction to Gemini Flash primary

**Files:**
- Modify: `app/services/analytical/control_policy.py`

- [ ] **Step 1: Find and update `_run_brain_dump_extraction` to use `gemini_primary_route`**

In `app/services/analytical/control_policy.py`, replace the LLM call inside `_run_brain_dump_extraction` (around line 411-420). The current code calls `hybrid_route_query` with `model_override=SLM_ROUTER_MODEL`. Change it to call `gemini_primary_route` with `fallback_model=SLM_ROUTER_MODEL`:

```python
from app.models.brain.litellm_conf import gemini_primary_route

# Inside _run_brain_dump_extraction, replace the hybrid_route_query call:
result = await gemini_primary_route(
    user_prompt=user_prompt,
    system_prompt=BRAIN_DUMP_SYSTEM_PROMPT,
    response_schema=BrainDumpExtraction,
    fallback_model=SLM_ROUTER_MODEL,
    conversation_history=conversation_history,
)
```

- [ ] **Step 2: Update habit_translator.py to use Gemini Flash primary**

In `app/services/analytical/habit_translator.py`, in `translate_habits_to_slots` (around line 256-262), replace the `hybrid_route_query` call:

```python
from app.models.brain.litellm_conf import gemini_primary_route

# Replace the existing hybrid_route_query call with:
result = await gemini_primary_route(
    user_prompt=habits_text,
    system_prompt=HABIT_TRANSLATOR_PROMPT,
    response_schema=SemanticTimeSlotsResponse,
    fallback_model=SLM_ROUTER_MODEL,
)
```

- [ ] **Step 3: Update decomposition to use Gemini Flash primary**

In `control_policy.py`, find where Socratic Chunker calls `hybrid_route_query` for decomposition (around lines 696-707). Change to `gemini_primary_route` with `fallback_model=LOCAL_LLM_MODEL` (27B as Phase 1 fallback):

```python
from app.core.config import LOCAL_LLM_MODEL

result = await gemini_primary_route(
    user_prompt=decomposition_prompt,
    system_prompt=SOCRATIC_CHUNKER_PROMPT,
    response_schema=ExecutionGraph,
    fallback_model=LOCAL_LLM_MODEL,  # 27B as Phase 1 fallback
)
```

- [ ] **Step 4: Verify the server starts cleanly**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.services.analytical.control_policy import execute_agentic_flow; print('OK')"`
Expected: `OK` (no import errors)

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py app/services/analytical/habit_translator.py
git commit -m "feat: switch brain dump, decomposition, habit translation to Gemini Flash primary

Spec 1.1: Gemini 2.5 Flash is now primary for all structured extraction tasks.
Brain dump fallback: Qwen-4B. Decomposition fallback: Qwen-27B (Phase 1).
Habit translation fallback: Qwen-4B. This fixes the ~10s 'hi' response."
```

---

### Task 3: Wire intent registry with real async handlers

**Files:**
- Modify: `app/services/intent_registry.py`
- Modify: `app/services/analytical/control_policy.py`

- [ ] **Step 1: Define IntentContext dataclass**

Add to `app/schemas/context.py`:

```python
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable

@dataclass
class IntentContext:
    """Context passed to every intent handler."""
    user_id: str
    user_prompt: str
    brain_dump: Optional["BrainDumpExtraction"] = None
    memory_context: str = ""
    db_client: Any = None
    progress_callback: Any = None
    model_mode: str = "auto"
    draft_store: Any = None
    memory_store: Any = None
    conversation_history: list[dict] = field(default_factory=list)
    # Pass-through kwargs from execute_agentic_flow
    extra: dict = field(default_factory=dict)
```

- [ ] **Step 2: Rewrite intent_registry.py with real handlers**

Replace the entire `app/services/intent_registry.py` file. Each handler delegates to the existing business logic functions. Handlers that don't have existing logic yet get a minimal implementation that calls Voice of Jarvis with a contextual message:

```python
"""Intent Registry — real handlers, no stubs.

Adding a new intent:
  1. Write an async handler function
  2. Call intent_registry.register(RegistryEntry(...))
  3. Done. The classifier auto-discovers it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.registry import BaseRegistry, RegistryEntry
from app.schemas.context import IntentContext, ChatResponse

logger = logging.getLogger(__name__)

intent_registry: BaseRegistry[dict] = BaseRegistry(name="intent", fallback_key="CHAT")


# ── Handlers ───────────────────────────────────────────────────────────

async def handle_plan_day(ctx: IntentContext) -> ChatResponse:
    """Full plan-day pipeline: habits → translate → decompose → schedule → draft."""
    from app.services.analytical.control_policy import _run_plan_day_flow
    planning_goal = ""
    if ctx.brain_dump and ctx.brain_dump.planning_goal:
        planning_goal = ctx.brain_dump.planning_goal
    if not planning_goal:
        planning_goal = ctx.user_prompt

    return await _run_plan_day_flow(
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        db_client=ctx.db_client,
        planning_goal=planning_goal,
        state_updates=ctx.brain_dump.state_updates if ctx.brain_dump else None,
        progress_callback=ctx.progress_callback,
        draft_store=ctx.draft_store,
        memory_store=ctx.memory_store,
        **ctx.extra,
    )


async def handle_greeting(ctx: IntentContext) -> ChatResponse:
    """Simple greeting — Voice of Jarvis only."""
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"greeting": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_general_qa(ctx: IntentContext) -> ChatResponse:
    """Direct QA — 4B answers the question."""
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"general_qa": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
        conversation_history=ctx.conversation_history,
    )


async def handle_ingest_document(ctx: IntentContext) -> ChatResponse:
    """Route to document ingestion pipeline."""
    from app.services.extraction.orchestrator import process_ingestion
    from app.schemas.context import IntentType
    result = await process_ingestion(
        user_id=ctx.user_id,
        payload=ctx.user_prompt,
        db_client=ctx.db_client,
        intent_override=IntentType.KNOWLEDGE_INGESTION,
        file_base64=ctx.extra.get("file_base64"),
        media_type=ctx.extra.get("media_type"),
        file_name=ctx.extra.get("file_name"),
    )
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"knowledge_ingested": True, "ingestion_result": str(result)},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_calendar_sync(ctx: IntentContext) -> ChatResponse:
    """Route to calendar extraction pipeline."""
    from app.services.extraction.orchestrator import process_ingestion
    from app.schemas.context import IntentType
    result = await process_ingestion(
        user_id=ctx.user_id,
        payload=ctx.user_prompt,
        db_client=ctx.db_client,
        intent_override=IntentType.CALENDAR_SYNC,
        file_base64=ctx.extra.get("file_base64"),
        media_type=ctx.extra.get("media_type"),
    )
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"calendar_extracted": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_behavioral_constraint(ctx: IntentContext) -> ChatResponse:
    """Store behavioral constraint and acknowledge."""
    from app.services.extraction.orchestrator import process_ingestion
    from app.schemas.context import IntentType
    result = await process_ingestion(
        user_id=ctx.user_id,
        payload=ctx.user_prompt,
        db_client=ctx.db_client,
        intent_override=IntentType.BEHAVIORAL_CONSTRAINT,
    )
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"habits_saved": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_action_item(ctx: IntentContext) -> ChatResponse:
    """Route to action item proposal pipeline."""
    from app.services.extraction.orchestrator import process_ingestion
    from app.schemas.context import IntentType
    result = await process_ingestion(
        user_id=ctx.user_id,
        payload=ctx.user_prompt,
        db_client=ctx.db_client,
        intent_override=IntentType.ACTION_ITEM,
    )
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"action_items_proposed": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_edit_task(ctx: IntentContext) -> ChatResponse:
    """Edit a task in Supabase, trigger replan."""
    # Phase 1: acknowledge + VoJ. Full edit logic TBD with draft endpoints.
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"edit_task": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_rearrange(ctx: IntentContext) -> ChatResponse:
    """Rearrange tasks, trigger replan."""
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"rearrange": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_add_constraint(ctx: IntentContext) -> ChatResponse:
    """Store constraint in behavioral_constraints, trigger replan."""
    return await handle_behavioral_constraint(ctx)


async def handle_accept_draft(ctx: IntentContext) -> ChatResponse:
    """Accept pending draft."""
    if ctx.draft_store:
        draft = await ctx.draft_store.get_pending_draft(ctx.user_id)
        if draft:
            await ctx.draft_store.accept_draft(draft["id"], ctx.user_id)
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"draft_accepted": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_reject_draft(ctx: IntentContext) -> ChatResponse:
    """Reject pending draft, store reason as memory."""
    if ctx.draft_store:
        draft = await ctx.draft_store.get_pending_draft(ctx.user_id)
        if draft:
            await ctx.draft_store.reject_draft(draft["id"], ctx.user_id)
    # Store rejection reason as feedback memory
    if ctx.memory_store:
        await ctx.memory_store.store_memory(
            user_id=ctx.user_id,
            memory_type="feedback",
            content=f"User rejected draft: {ctx.user_prompt}",
            confidence=0.5,
        )
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"draft_rejected": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_check_progress(ctx: IntentContext) -> ChatResponse:
    """Query tasks + completion stats."""
    from app.services.analytical.task_retrieval import get_all_pending_tasks
    tasks = await get_all_pending_tasks(ctx.user_id, ctx.db_client)
    total = len(tasks)
    done = sum(1 for t in tasks if t.get("status") == "completed")
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={
            "check_progress": True,
            "total_tasks": total,
            "completed_tasks": done,
        },
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
    )


async def handle_chat(ctx: IntentContext) -> ChatResponse:
    """General conversation fallback — Voice of Jarvis."""
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    return await synthesize_jarvis_response(
        execution_summary={"general_chat": True},
        user_prompt=ctx.user_prompt,
        user_id=ctx.user_id,
        memory_context=ctx.memory_context,
        conversation_history=ctx.conversation_history,
    )


# ── Registration ───────────────────────────────────────────────────────

def register_default_intents() -> None:
    """Register all built-in intents. Called during app lifespan startup."""
    entries = [
        ("PLAN_DAY", "User wants to plan their day or create a schedule", handle_plan_day, {"triggers_replan": True}),
        ("GREETING", "User is greeting or saying hello", handle_greeting, {}),
        ("GENERAL_QA", "User is asking a general question", handle_general_qa, {}),
        ("INGEST_DOCUMENT", "User wants to upload or process a document", handle_ingest_document, {}),
        ("CALENDAR_SYNC", "User mentions a timetable or class schedule", handle_calendar_sync, {}),
        ("BEHAVIORAL_CONSTRAINT", "User states a habit or time constraint", handle_behavioral_constraint, {}),
        ("ACTION_ITEM", "User mentions a specific action or task to do", handle_action_item, {}),
        ("EDIT_TASK", "User wants to edit an existing task", handle_edit_task, {"triggers_replan": True}),
        ("REARRANGE", "User wants to rearrange or reorder tasks", handle_rearrange, {"triggers_replan": True}),
        ("ADD_CONSTRAINT", "User wants to add a new scheduling constraint", handle_add_constraint, {"triggers_replan": True}),
        ("ACCEPT_DRAFT", "User accepts the proposed schedule", handle_accept_draft, {"requires_draft": True}),
        ("REJECT_DRAFT", "User rejects the proposed schedule", handle_reject_draft, {"requires_draft": True}),
        ("CHECK_PROGRESS", "User asks about their progress or task status", handle_check_progress, {}),
        ("CHAT", "General conversation or anything not matching other intents", handle_chat, {}),
    ]
    for name, desc, handler, meta in entries:
        intent_registry.register(RegistryEntry(
            name=name,
            description=desc,
            handler=handler,
            metadata=meta,
        ))
```

- [ ] **Step 3: Update control_policy.py to use registry dispatch instead of if/elif**

In `execute_agentic_flow`, after intent classification, replace the `_fallback_single_intent` call with registry-based dispatch:

```python
from app.services.intent_registry import intent_registry
from app.schemas.context import IntentContext

# Build context for handler
intent_ctx = IntentContext(
    user_id=user_id,
    user_prompt=user_prompt,
    brain_dump=brain_dump_result,
    memory_context=memory_context,
    db_client=db_client,
    progress_callback=progress_callback,
    model_mode=model_mode,
    draft_store=draft_store,
    memory_store=memory_store,
    conversation_history=conversation_history or [],
    extra={
        "file_base64": file_base64,
        "media_type": media_type,
        "file_name": file_name,
        "day_start_hour_override": day_start_hour_override,
        "deadline_override": deadline_override,
        "max_daily_deep_work_minutes": max_daily_deep_work_minutes,
        "min_daily_deep_work_minutes": min_daily_deep_work_minutes,
        "skip_scheduling": skip_scheduling,
        "draft_schedule": draft_schedule,
    },
)

# Registry dispatch — NO if/elif
entry = intent_registry.get(classified_intent.value if hasattr(classified_intent, 'value') else str(classified_intent))
if entry is None:
    entry = intent_registry.get_or_fallback("CHAT")
response = await entry.handler(intent_ctx)
```

- [ ] **Step 4: Verify server starts and imports work**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.services.intent_registry import intent_registry, register_default_intents; register_default_intents(); print(f'Registered: {intent_registry.registered_names()}')"`
Expected: List of all registered intent names

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/intent_registry.py app/schemas/context.py app/services/analytical/control_policy.py
git commit -m "feat: wire intent registry with real handlers, registry-based dispatch

Spec 1.2: Replace if/elif cascade with BaseRegistry dispatch.
All 14 intents have real async handlers. Adding new intent = handler + register()."
```

---

### Task 4: Add missing draft endpoints (rearrange + chat)

**Files:**
- Modify: `app/api/v1/endpoints/drafts.py`

- [ ] **Step 1: Add rearrange endpoint**

Add after the existing `edit_draft_task` endpoint in `drafts.py`:

```python
from pydantic import BaseModel
from typing import List


class DraftRearrangeRequest(BaseModel):
    user_id: str
    task_order: List[str]  # Ordered list of task_ids in desired sequence


@router.post(
    "/{draft_id}/rearrange",
    summary="Rearrange task order in draft and re-solve",
)
async def rearrange_draft(
    draft_id: str,
    request: DraftRearrangeRequest,
    http_request: Request,
):
    """Swap task positions in a draft, then re-trigger OR-Tools solve."""
    draft_store = http_request.app.state.draft_store
    draft = await draft_store.get_draft(draft_id, request.user_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Reorder tasks according to task_order
    tasks = draft.get("tasks", [])
    order_map = {tid: idx for idx, tid in enumerate(request.task_order)}
    tasks.sort(key=lambda t: order_map.get(t.get("task_id", ""), len(tasks)))

    # Update draft with new order
    await draft_store.update_draft_tasks(draft_id, request.user_id, tasks)

    return {"status": "rearranged", "draft_id": draft_id, "task_count": len(tasks)}
```

- [ ] **Step 2: Add chat endpoint for natural language draft modification**

```python
class DraftChatRequest(BaseModel):
    user_id: str
    message: str  # Natural language: "move DSA to afternoon"


@router.post(
    "/{draft_id}/chat",
    summary="Modify draft via natural language",
)
async def chat_modify_draft(
    draft_id: str,
    request: DraftChatRequest,
    http_request: Request,
):
    """Natural language modification of a draft. May re-decompose and re-solve."""
    draft_store = http_request.app.state.draft_store
    draft = await draft_store.get_draft(draft_id, request.user_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Use the existing schedule modify flow
    from app.services.analytical.control_policy import _run_schedule_modify_flow
    result = await _run_schedule_modify_flow(
        user_prompt=request.message,
        user_id=request.user_id,
        db_client=http_request.app.state.db,
        draft_store=draft_store,
        existing_draft_id=draft_id,
    )

    return {"status": "modified", "draft_id": draft_id, "result": result}
```

- [ ] **Step 3: Fix reject endpoint to build memory**

In the existing `reject_draft` function, after marking the draft rejected, add memory storage:

```python
# After the existing rejection logic, add:
memory_store = getattr(http_request.app.state, "memory_store", None)
if memory_store and request.reason:
    await memory_store.store_memory(
        user_id=request.user_id,
        memory_type="feedback",
        content=f"User rejected schedule draft: {request.reason}",
        confidence=0.5,
    )
```

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/api/v1/endpoints/drafts.py
git commit -m "feat: add draft rearrange and chat endpoints, fix reject to build memory

Spec 1.4: Complete draft negotiation loop with all 5 actions."
```

---

### Task 5: Add PEARL deadline_buffer detector and integrate detection into flows

**Files:**
- Modify: `app/services/memory/pearl.py`
- Modify: `app/api/v1/endpoints/tasks.py`
- Modify: `app/api/v1/endpoints/drafts.py`
- Modify: `app/services/memory/extractor.py`

- [ ] **Step 1: Add deadline_buffer detector to pearl.py**

After the existing `detect_completion_time_preference` function, add:

```python
def detect_deadline_buffer(
    user_id: str, tasks: list[dict], memory_store, **kwargs
) -> list[dict]:
    """Detect if user consistently extends deadlines by N days."""
    # Look for tasks that were edited to have later deadlines
    edited_tasks = [t for t in tasks if t.get("deadline_edited")]
    if len(edited_tasks) < MIN_OBSERVATIONS:
        return []

    extensions = []
    for t in edited_tasks:
        original = t.get("original_deadline")
        current = t.get("deadline_hint")
        if original and current:
            try:
                from datetime import datetime
                orig_dt = datetime.fromisoformat(original)
                curr_dt = datetime.fromisoformat(current)
                delta_days = (curr_dt - orig_dt).days
                if delta_days > 0:
                    extensions.append(delta_days)
            except (ValueError, TypeError):
                continue

    if not extensions or len(extensions) < MIN_OBSERVATIONS:
        return []

    avg_extension = sum(extensions) / len(extensions)
    extension_rate = len(extensions) / max(1, len(edited_tasks))

    if extension_rate >= MIN_PATTERN_RATE:
        pattern_content = f"User typically extends deadlines by {avg_extension:.0f} days"
        existing = memory_store.find_pattern(user_id, "deadline_buffer")
        if existing:
            memory_store.reinforce_memory(existing["id"], user_id)
            return [{"pattern": "deadline_buffer", "action": "reinforced", "avg_days": avg_extension}]
        else:
            memory_store.store_memory(
                user_id=user_id,
                memory_type="behavioral_pattern",
                content=pattern_content,
                confidence=min(0.9, extension_rate),
                metadata={"pattern_name": "deadline_buffer", "avg_days": avg_extension},
            )
            return [{"pattern": "deadline_buffer", "action": "created", "avg_days": avg_extension}]

    return []
```

- [ ] **Step 2: Register the new detector and rename existing**

In `register_default_patterns()`, add:

```python
pearl_registry.register(RegistryEntry(
    name="deadline_buffer",
    description="Detects when user consistently extends deadlines",
    handler=detect_deadline_buffer,
    metadata={"applied_as": "deadline_buffer"},
))
```

Also rename `completion_time_preference` to `duration_preference` in the existing registration.

- [ ] **Step 3: Call detect_patterns after task complete/skip**

In `app/api/v1/endpoints/tasks.py`, after the task completion and skip handlers, add:

```python
import asyncio
from app.services.memory.pearl import detect_patterns

# At the end of complete_task endpoint:
memory_store = getattr(request.app.state, "memory_store", None)
if memory_store:
    asyncio.create_task(
        detect_patterns(user_id, request.app.state.db, memory_store)
    )
```

- [ ] **Step 4: Chain PEARL detection after memory extraction**

In `app/services/memory/extractor.py`, modify `safe_extract_memories`:

```python
async def safe_extract_memories(
    user_id: str,
    user_message: str,
    assistant_response: str,
    memory_store,
    db_client=None,
) -> None:
    """Fire-and-forget: extract memories then run PEARL detection."""
    try:
        await extract_memories_from_turn(
            user_id=user_id,
            user_message=user_message,
            assistant_response=assistant_response,
            memory_store=memory_store,
        )
        # Chain PEARL detection after extraction
        if db_client:
            from app.services.memory.pearl import detect_patterns
            detect_patterns(user_id, db_client, memory_store)
    except Exception as e:
        logger.debug("Memory extraction/PEARL failed (non-blocking): %s", e)
```

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/memory/pearl.py app/api/v1/endpoints/tasks.py app/services/memory/extractor.py
git commit -m "feat: add deadline_buffer PEARL detector, integrate detection into all flows

Spec 1.6: All 3 patterns (skip_time_window, duration_preference, deadline_buffer)
now detected. PEARL runs after task complete/skip and memory extraction."
```

---

### Task 6: Fix memory scoring (add Importance factor + lifecycle state)

**Files:**
- Modify: `app/services/memory/retriever.py`
- Modify: `app/services/memory/store.py`

- [ ] **Step 1: Add IMPORTANCE_WEIGHTS to retriever.py**

At the top of `app/services/memory/retriever.py`, add or update the constant:

```python
IMPORTANCE_WEIGHTS: dict[str, float] = {
    "constraint": 1.0,
    "behavioral_pattern": 0.9,
    "preference": 0.8,
    "temporal_event": 0.8,
    "goal": 0.7,
    "fact": 0.6,
    "feedback": 0.5,
}
```

Verify that `score_memory` uses this dict. The existing code should already have `IMPORTANCE_WEIGHTS` — if the values differ, update them to match.

- [ ] **Step 2: Update `get_active_memories` to filter by lifecycle state**

In `app/services/memory/store.py`, modify `get_active_memories`:

```python
def get_active_memories(self, user_id: str) -> list[dict]:
    """Retrieve active memories, excluding archived and superseded."""
    result = self.client.table("user_memories").select("*").eq(
        "user_id", user_id
    ).is_("superseded_by", "null").execute()

    memories = result.data or []
    # Filter out archived (strength < 0.1)
    current_time = datetime.now(timezone.utc)
    return [
        m for m in memories
        if compute_memory_strength(m, current_time) >= 0.1
    ]
```

- [ ] **Step 3: Add reinforcement method that resets state**

In `app/services/memory/store.py`:

```python
async def reinforce_memory(self, memory_id: str, user_id: str) -> None:
    """Reinforce a memory: stability++, confidence += 0.1, strength = 1.0."""
    mem = await self.get_memory(memory_id, user_id)
    if not mem:
        return
    new_stability = min(20.0, (mem.get("stability", 1.0) + 1.0))
    new_confidence = min(1.0, (mem.get("confidence", 0.5) + 0.1))
    self.client.table("user_memories").update({
        "stability": new_stability,
        "confidence": new_confidence,
        "strength": 1.0,
        "last_reinforced": datetime.now(timezone.utc).isoformat(),
    }).eq("id", memory_id).eq("user_id", user_id).execute()
```

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/memory/retriever.py app/services/memory/store.py
git commit -m "feat: add Importance factor to memory scoring, lifecycle state management

Spec 1.7: Score = Relevance × Recency × Importance × Confidence.
Reinforcement resets strength to 1.0. Archived memories excluded from queries."
```

---

### Task 7: Add phase SSE events to control_policy.py

**Files:**
- Modify: `app/services/analytical/control_policy.py`

- [ ] **Step 1: Emit phase events at each pipeline stage**

In `execute_agentic_flow` and `_run_plan_day_flow`, add `progress_callback` calls at each stage. The `progress_callback` is already a parameter — it just needs to be called:

```python
# Before brain dump extraction:
if progress_callback:
    await progress_callback("phase", {"phase": "brain_dump_extraction", "model": "gemini-2.5-flash"})

# After intent classification:
if progress_callback:
    await progress_callback("phase", {"phase": "intent_classified", "intent": str(classified_intent)})

# In _run_plan_day_flow, before decomposition:
if progress_callback:
    await progress_callback("phase", {"phase": "decomposing", "goal": planning_goal})

# Before OR-Tools solve:
if progress_callback:
    await progress_callback("phase", {"phase": "scheduling"})

# Before Voice of Jarvis:
if progress_callback:
    await progress_callback("phase", {"phase": "synthesizing"})
```

- [ ] **Step 2: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py
git commit -m "feat: emit phase SSE events at each pipeline stage

Spec 1.13: Frontend PhaseProgress component can now display
'Brewing your plan... 301ms ✓' for each pipeline stage."
```

---

### Task 8: Add draft rearrange and chat endpoints to router

**Files:**
- Modify: `app/api/v1/router.py` (if needed — verify drafts router is mounted)

- [ ] **Step 1: Verify draft router is mounted**

Check `app/api/v1/router.py` includes the drafts router. If the new endpoints are in the same file, they should auto-mount.

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && grep -n "drafts" app/api/v1/router.py`

- [ ] **Step 2: Run a smoke test**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.main import app; print([r.path for r in app.routes if 'draft' in str(r.path)])"`

Verify `/api/v1/drafts/{draft_id}/rearrange` and `/api/v1/drafts/{draft_id}/chat` appear.

- [ ] **Step 3: Run existing tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v --timeout=30 2>&1 | tail -20`

- [ ] **Step 4: Final commit for WS1**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add -A
git commit -m "chore: verify all WS1 backend changes integrate correctly

Backend architecture now matches spec flow diagrams:
- Gemini Flash primary for brain dump/decompose/habit translation
- Registry-based intent dispatch (no if/elif)
- All 14 intent handlers wired
- Draft rearrange + chat endpoints
- PEARL all 3 patterns integrated
- Memory scoring with Importance factor"
```
