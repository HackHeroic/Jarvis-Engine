# V2 Production Wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bridge the gap between "v2 architecture scaffolding passes 44 tests" and "frontend uses v2 for real users." Make `/api/v1/chat/v2/stream` feature-complete, then switch the frontend to use it.

**Architecture:** The LangGraph orchestrator (graph.py) is built with 10 nodes, 5 modules, hooks, and observation loop. This plan wires it to production: session management, full ChatResponse payload, sub-graph progress streaming, and frontend URL switch.

**Tech Stack:** FastAPI, LangGraph, Next.js 14 (frontend), Supabase, SSE

---

## What needs to happen (4 tasks)

1. **Task 1:** Make `/v2/stream` complete event match the existing ChatResponse shape
2. **Task 2:** Wire session management (save messages, conversation history)
3. **Task 3:** Bridge sub-graph progress_callback into SSE stream
4. **Task 4:** Point frontend to `/v2/stream`

---

### Task 1: Complete ChatResponse Payload in /v2/stream

**Files:**
- Modify: `app/api/v1/endpoints/chat.py`

The current `/v2/stream` complete event sends only `{intent, message, thinking_process}`. The frontend expects the full `ChatResponse` model. Fix: build a complete response from accumulated state.

- [ ] **Step 1: Read the ChatResponse schema**

Read `app/schemas/context.py` to see all fields in `ChatResponse`:
```python
class ChatResponse(BaseModel):
    intent: str
    message: str
    schedule: Optional[SchedulePayload] = None
    execution_graph: Optional[dict] = None
    ingestion_result: Optional[dict] = None
    action_proposals: Optional[List[dict]] = None
    search_result: Optional[dict] = None
    suggested_action: Optional[str] = None
    thinking_process: Optional[str] = None
    awaiting_task_confirmation: bool = False
    schedule_status: Optional[str] = None
    draft_id: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    clarification_options: Optional[List[str]] = None
    memories: Optional[List[dict]] = None
    pearl_insights: Optional[List[dict]] = None
```

- [ ] **Step 2: Update the complete event builder in chat_stream_v2**

In `app/api/v1/endpoints/chat.py`, find the `/v2/stream` endpoint's `event_gen()`. Replace the incomplete complete event with a full ChatResponse:

```python
# Build complete ChatResponse from accumulated state
complete_payload = {
    "intent": str(final_state.get("intent", "CHAT")),
    "message": final_state.get("response_message", ""),
    "schedule": final_state.get("schedule"),
    "execution_graph": final_state.get("execution_graph"),
    "ingestion_result": final_state.get("ingestion_result"),
    "action_proposals": None,
    "search_result": None,
    "suggested_action": None,
    "thinking_process": final_state.get("thinking_process"),
    "awaiting_task_confirmation": False,
    "schedule_status": "draft" if final_state.get("schedule") else None,
    "draft_id": final_state.get("draft_id"),
    "conversation_id": final_state.get("conversation_id"),
    "message_id": final_state.get("message_id"),
    "clarification_options": None,
    "memories": final_state.get("memories"),
    "pearl_insights": None,
}
yield f"event: complete\ndata: {json_mod.dumps(complete_payload)}\n\n"
```

- [ ] **Step 3: Run existing tests**

Run: `python -m pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/api/v1/endpoints/chat.py
git commit -m "fix: v2/stream complete event sends full ChatResponse payload"
```

---

### Task 2: Wire Session Management into /v2/stream

**Files:**
- Modify: `app/api/v1/endpoints/chat.py`

The existing `/stream` endpoint calls `save_user_message()` and `save_assistant_message()` from `app/services/chat_history.py`. The `/v2/stream` does neither — every turn is stateless.

- [ ] **Step 1: Read chat_history.py to understand the session API**

Read `app/services/chat_history.py` to find:
- `save_user_message(session_id, user_id, content, supabase)` signature
- `save_assistant_message(session_id, user_id, content, intent, metadata, supabase)` signature
- `get_or_create_session(user_id, supabase)` signature

- [ ] **Step 2: Read the existing /stream endpoint's session flow**

Read `app/api/v1/endpoints/chat.py` around lines 190-330 to see how `/stream` does session management. Key pattern:
```python
session_id = await get_or_create_session(request.user_id, supabase)
await save_user_message(session_id, request.user_id, request.user_prompt, supabase)
# ... run pipeline ...
msg_id = await save_assistant_message(session_id, request.user_id, response_text, intent, metadata, supabase)
```

- [ ] **Step 3: Add session management to /v2/stream**

At the top of `chat_stream_v2`, before the event_gen:

```python
from app.services.chat_history import get_or_create_session, save_user_message, save_assistant_message

supabase = db_client.supabase if hasattr(db_client, 'supabase') else None
session_id = None
if supabase:
    session_id = await get_or_create_session(request.user_id, supabase)
    await save_user_message(session_id, request.user_id, request.user_prompt, supabase)
```

After the astream loop completes (before the complete event):

```python
# Save assistant message and get message_id
msg_id = None
if supabase and session_id:
    msg_id = await save_assistant_message(
        session_id, request.user_id,
        final_state.get("response_message", ""),
        str(final_state.get("intent", "CHAT")),
        {},
        supabase,
    )
final_state["conversation_id"] = session_id
final_state["message_id"] = msg_id
```

- [ ] **Step 4: Load conversation history into initial state**

The existing `/stream` loads prior messages for context. Add to `/v2/stream` before the event_gen:

```python
conversation_history = []
if supabase and session_id:
    from app.services.chat_history import load_conversation
    conversation_history = await load_conversation(session_id, request.user_id, supabase)
```

Pass this into initial_state or UserModel for module access.

- [ ] **Step 5: Also load existing memories for the complete payload**

```python
existing_memories = []
if hasattr(http_request.app.state, "memory_store"):
    memory_store = http_request.app.state.memory_store
    existing_memories = memory_store.get_active_memories(request.user_id)
```

Add to complete payload: `"memories": [{"memory_type": m.get("memory_type"), "content": m.get("content"), "confidence": m.get("confidence", 0.5)} for m in existing_memories[:20]]`

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/chat.py
git commit -m "feat: add session management to v2/stream (save messages, load history, load memories)"
```

---

### Task 3: Bridge Sub-Graph Progress into SSE Stream

**Files:**
- Modify: `app/orchestrator/graph.py`
- Modify: `app/modules/planning_graph.py`
- Modify: `app/api/v1/endpoints/chat.py`

The planning sub-graph has 9 nodes that emit progress (habits_fetched, translating, decomposing, scheduling). Currently `progress_callback: None` so these are lost. The old endpoint uses an asyncio.Queue bridge.

- [ ] **Step 1: Add progress_callback to JarvisState**

In `app/orchestrator/state.py`, add to JarvisState:
```python
progress_callback: Any  # Optional[Callable[[str], None]]
```

- [ ] **Step 2: Create asyncio.Queue bridge in /v2/stream**

In the `/v2/stream` endpoint, create a queue that planning nodes write to:

```python
import asyncio

progress_queue: asyncio.Queue[str] = asyncio.Queue()

def progress_cb(phase: str, **detail):
    progress_queue.put_nowait(json_mod.dumps({"phase": phase, **detail}))
```

Add to initial_state:
```python
"progress_callback": progress_cb,
```

- [ ] **Step 3: Pass progress_callback through to planning sub-graph**

In `app/orchestrator/graph.py`, update `_planning_module_node` to pass the callback:

```python
planning_state = {
    ...
    "progress_callback": state.get("progress_callback"),
}
```

The planning_graph.py nodes already call `cb = state.get("progress_callback"); if cb: cb("habits_fetched")` — this will now flow through.

- [ ] **Step 4: Consume progress queue in event_gen**

Update the event_gen to drain the progress queue after each astream event:

```python
async for event in jarvis_graph.astream(initial_state, config):
    # Drain any sub-graph progress events
    while not progress_queue.empty():
        try:
            progress_data = progress_queue.get_nowait()
            yield f"event: phase\ndata: {progress_data}\n\n"
        except asyncio.QueueEmpty:
            break
    
    # ... existing node event handling ...
```

- [ ] **Step 5: Test manually**

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/v2/stream \
  -H "Content-Type: application/json" \
  -d '{"user_prompt": "plan my DSA study for tomorrow", "user_id": "demo"}'
```

Should see: `phase: loading_context` → `phase: brain_dump_extraction` → `phase: intent_classified` → `phase: habits_fetched` → `phase: translating` → `phase: decomposing` → `phase: scheduling` → `phase: synthesizing` → `phase: learning` → `complete: {...}`

- [ ] **Step 6: Commit**

```bash
git add app/orchestrator/state.py app/orchestrator/graph.py app/modules/planning_graph.py app/api/v1/endpoints/chat.py
git commit -m "feat: bridge sub-graph progress_callback into SSE stream via asyncio.Queue"
```

---

### Task 4: Point Frontend to /v2/stream

**Files:**
- Modify: `jarvis-frontend/lib/api.ts`

The simplest change: update the URL from `/chat/stream` to `/chat/v2/stream`.

- [ ] **Step 1: Read the current chatStream function**

Read `jarvis-frontend/lib/api.ts` line 174-205 to see the `chatStream` function.

- [ ] **Step 2: Update the URL**

Change line 186:
```typescript
// Old:
res = await fetch(`${API_BASE}/api/v1/chat/stream`, {
// New:
res = await fetch(`${API_BASE}/api/v1/chat/v2/stream`, {
```

- [ ] **Step 3: Add env var toggle for easy rollback**

Better approach — make it configurable:

```typescript
const CHAT_STREAM_PATH = process.env.NEXT_PUBLIC_USE_V2 === 'true'
  ? '/api/v1/chat/v2/stream'
  : '/api/v1/chat/stream';

// In chatStream():
res = await fetch(`${API_BASE}${CHAT_STREAM_PATH}`, {
```

Add `NEXT_PUBLIC_USE_V2=true` to `.env.local` to enable. Remove to rollback.

- [ ] **Step 4: Test in browser**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
npm run dev
```

Open http://localhost:3000, send a message. Verify:
- Spinner phases render (phase events)
- Thinking process shows (thinking events)
- Schedule renders if PLAN_DAY (complete event has schedule)
- Conversation ID persists across messages (session management)

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/api.ts .env.local
git commit -m "feat: add NEXT_PUBLIC_USE_V2 toggle to switch frontend to LangGraph v2 endpoint"
```

---

## Summary

| Task | What | Files | Est. |
|---|---|---|---|
| 1 | Complete ChatResponse payload | chat.py | 15 min |
| 2 | Session management (save/load) | chat.py | 30 min |
| 3 | Sub-graph progress → SSE bridge | state.py, graph.py, planning_graph.py, chat.py | 30 min |
| 4 | Frontend URL switch | api.ts, .env.local | 10 min |

**Total: ~1.5 hours.** After this, the frontend uses the v2 LangGraph orchestrator for all chat interactions.
