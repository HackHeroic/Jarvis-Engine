# Jarvis Core Loop Realignment — Design Spec

**Date:** 2026-03-31
**Author:** Madhav + Claude
**Status:** Draft — awaiting review
**Context:** VC pitch demo on 2026-04-01. The architecture spec (2026-03-28) is correct — the implementation drifted in 4 specific places. This spec realigns code to spec.

---

## Executive Summary

The core demo loop is broken in 4 places, and 6 additional gaps were found during spec review against the architecture diagrams:

**Critical (Demo-Blocking):**
1. **Decomposition generates wrong tasks** — Brain dump extraction strips domain context ("deep learning contest") into generic "Plan my week", so Socratic Chunker produces irrelevant tasks
2. **Accept All doesn't show on calendar** — Backend stores `scheduled_start`/`scheduled_end`, frontend transform looks for `start_time`/`start_min` — field name mismatch
3. **Schedule rendering is nonsensical** — Multi-day schedules render as flat timeline with "1370m break" gaps, TMT scores round to 0.0
4. **Memory doesn't influence decomposition** — Memory context injected into brain dump extraction but NOT into the Socratic Chunker, so memories never shape task generation

**Critical (Competitive Moat):**
5. **Draft negotiation loop incomplete** — Only Accept All works. Edit Task → re-solve, Reject → memory + new approach, and Chat to Modify → re-decompose are not wired. The architecture calls this "no competitor does this"
6. **Memory-to-constraint bridge unverified** — `memories_to_constraints()` exists in code but may not be called correctly. The architecture's breakthrough is memories becoming mathematical constraints in OR-Tools, not just text in LLM prompts

**Important (Architecture Compliance):**
7. **Memory CRUD API missing** — `memory/store.py` has full CRUD but zero API endpoints expose it. MemoryPanel delete/confirm/dismiss buttons are dead
8. **Memory Panel data source wrong** — Only shows memories from last chat response (localStorage). Should fetch independently
9. **Workspace navigation not discoverable** — Calendar task click navigates to workspace but no visible affordance; workspace rendering itself not verified
10. **Goal progress tracking absent** — No per-goal aggregation of task completion. Dashboard shows totals only
11. **PEARL fire-and-forget unreliable** — `asyncio.create_task(detect_patterns(...))` with no error handling. Failures are silent
12. **Task completion signals not queryable** — `task_completion_signals` table fills up but no endpoint to retrieve for analytics/DKT

**Known Post-Demo Gaps (acknowledged, not in scope):**
- **Intent Registry refactor** — Architecture defines extensible `BaseRegistry` pattern. Current code uses hardcoded if/elif routing. Works for demo but limits extensibility
- **Document Intelligence Pipeline** — Architecture defines per-type document handlers (practice_problems, lecture_notes, syllabus). Current implementation does shallow cosine similarity. Rich doc handlers deferred
- **Session Summarization (Recall Memory)** — Architecture defines LLM-generated session summaries, mood signals, cross-session search. Not implemented. Conversation continuity works via message history but no summarization
- **LLM Routing Inversion** — Architecture spec says Gemini 2.5 Flash primary for extraction/decomposition, Qwen fallback. Current implementation may still use Qwen-27B primary. Spec assumes current routing works; inversion is a quality optimization, not a bug fix

---

## Fix 1: Extraction Preserves Full Context

### Root Cause

`control_policy.py:65` — Brain dump extraction prompt says **"Clean goal string only"**, causing the SLM to strip "deep learning contest on Friday and calculus exam on Monday" → "Plan my week".

Additionally, `_build_planning_context()` only enriches with stored deadlines from `user_plan_updates`, ignoring the freshly-extracted `deadline_update` field.

### Changes

#### 1a. Update `BrainDumpExtraction` schema (`app/schemas/context.py`)

Add field:
```python
subject_context: Optional[list[str]] = Field(
    default=None,
    description="Specific subjects, topics, exams, contests, projects mentioned. "
    "Preserve verbatim with any associated deadlines. "
    "E.g. ['deep learning contest - Friday', 'calculus exam - Monday']"
)
```

#### 1b. Update extraction prompt (`control_policy.py:62-88`)

Change from:
```
planning_goal: Schedule tasks, break down goal, plan day. Clean goal string only
```

To:
```
planning_goal: The user's scheduling intent WITH all specific subjects, topics, 
exams, contests, and deadlines preserved verbatim. Include domain details — never 
reduce to a generic summary. E.g. 'Plan my week for deep learning contest Friday 
and calculus exam Monday', NOT just 'Plan my week'.
```

Add extraction instruction for `subject_context`:
```
subject_context: List of specific subjects/topics/exams mentioned with any 
associated time references. E.g. ["deep learning contest - Friday", 
"calculus exam - Monday"]. Use null if no specific subjects mentioned.
```

#### 1c. Enrich decomposition input (`control_policy.py:_build_planning_context`)

After existing deadline enrichment, append:
```python
# Append deadline_update from current extraction
if extraction and extraction.deadline_update:
    enriched += f" [Deadline: {extraction.deadline_update}]"

# Append subject context
if extraction and extraction.subject_context:
    enriched += f" [Subjects: {', '.join(extraction.subject_context)}]"
```

This ensures the Socratic Chunker receives: `"Plan my week for deep learning contest Friday and calculus exam Monday [Deadline: 2026-04-04] [Subjects: deep learning contest - Friday, calculus exam - Monday]"`

#### 1d. Cache busting

The decomposition cache is keyed on SHA-256 of `enriched_planning_goal`. Since the goal string is now richer, old generic cached results ("Plan my week") won't match new requests ("Plan my week for deep learning contest..."). No explicit cache invalidation needed.

### Files Changed
- `app/schemas/context.py` — Add `subject_context` field
- `app/services/analytical/control_policy.py` — Update extraction prompt, update `_build_planning_context()`

---

## Fix 2: Accept All → Calendar Persistence

### Root Cause

**Field name mismatch between backend and frontend.**

Backend `_persist_fused_tasks()` stores tasks with:
- `scheduled_start` (ISO-8601 datetime string)
- `scheduled_end` (ISO-8601 datetime string)

Frontend `apiTasksToScheduleTasks()` in `transforms.ts` looks for:
- `start_time` (not found)
- OR `horizon_start` + `start_min` (not found)
- Fallback: `new Date()` (current time)

Result: every persisted task gets `start_time = now()`, fails `isSameDay` filter, calendar shows empty.

### Changes

#### 2a. Update frontend transform (`lib/transforms.ts`)

Add `scheduled_start` / `scheduled_end` as the **primary** fallback:

```typescript
const startTime = t.start_time
  ? new Date(t.start_time as string)
  : t.scheduled_start
    ? new Date(t.scheduled_start as string)
    : t.horizon_start && typeof t.start_min === "number"
      ? new Date(
          new Date(t.horizon_start as string).getTime() +
            (t.start_min as number) * 60_000,
        )
      : new Date();

const endTime = t.end_time
  ? new Date(t.end_time as string)
  : t.scheduled_end
    ? new Date(t.scheduled_end as string)
    : new Date(startTime.getTime() + dur * 60_000);
```

#### 2b. Retype `ChatResponse.schedule` (`app/schemas/context.py`)

Create a typed model instead of `Optional[dict]`:

```python
class SchedulePayload(BaseModel):
    """Typed schedule payload returned in ChatResponse."""
    schedule: dict = Field(description="Map of task_id -> {start_min, end_min, tmt_score, title}")
    horizon_start: str = Field(description="ISO-8601 datetime for minute-0 of the schedule")
    horizon_minutes: int = Field(description="Total horizon length in minutes")
    daily_cap_minutes: Optional[int] = Field(default=None, description="Adaptive daily cap used")
    draft_id: Optional[str] = Field(default=None, description="Draft ID if schedule is a draft")
    status: Optional[str] = Field(default=None, description="'draft' or 'final'")
```

Update `ChatResponse`:
```python
schedule: Optional[SchedulePayload] = Field(default=None, ...)
```

Update all `ChatResponse(schedule=...)` call sites in `control_policy.py` to construct `SchedulePayload` instead of raw dict.

#### 2c. Update frontend types (`lib/types.ts`)

Add `SchedulePayload` type matching the backend model. Update `ChatResponse` type to use it.

### Files Changed
- `jarvis-frontend/lib/transforms.ts` — Add `scheduled_start`/`scheduled_end` support
- `app/schemas/context.py` — Add `SchedulePayload` model, update `ChatResponse.schedule` type
- `app/services/analytical/control_policy.py` — Construct `SchedulePayload` instead of raw dict
- `jarvis-frontend/lib/types.ts` — Add `SchedulePayload` type

---

## Fix 3: Multi-Day Schedule Rendering

### Root Cause

`SchedulePreview.tsx` renders all tasks on a flat timeline without day boundaries. For a 48h+ horizon:
- Day 1 task ending at minute 70 → Day 2 task starting at minute 1440 = **1370m gap displayed raw**
- Times appear to go backwards when Day 2 tasks have smaller wall-clock hours than Day 1
- TMT scores round to 0.0 because `(Expectancy * Value) / (Impulsiveness * 24h)` produces ~0.01

### Changes

#### 3a. Group tasks by day in `SchedulePreview.tsx`

Replace flat task list with day-grouped rendering:

```typescript
type DayGroup = {
  date: Date;
  label: string;       // "Today", "Tomorrow", "Wed, Apr 2"
  entries: TimelineEntry[];
};

function groupByDay(entries: TimelineEntry[], horizonStart: Date): DayGroup[] {
  const groups: Map<string, DayGroup> = new Map();
  for (const entry of entries) {
    const taskDate = new Date(horizonStart.getTime() + entry.startMin * 60_000);
    const dateKey = taskDate.toDateString();
    if (!groups.has(dateKey)) {
      groups.set(dateKey, {
        date: taskDate,
        label: formatDayLabel(taskDate),
        entries: [],
      });
    }
    groups.get(dateKey)!.entries.push(entry);
  }
  return Array.from(groups.values()).sort((a, b) => a.date.getTime() - b.date.getTime());
}
```

Render with day headers:
```tsx
{dayGroups.map((group) => (
  <div key={group.label}>
    <DayHeader label={group.label} taskCount={group.entries.length} />
    {group.entries.map((entry, i) => {
      const prevEnd = i > 0 ? group.entries[i - 1].endMin : entry.startMin;
      const gap = entry.startMin - prevEnd;
      return (
        <>
          {gap > 5 && <BreakGap minutes={gap} />}
          <TimelineBlock entry={entry} />
        </>
      );
    })}
  </div>
))}
```

#### 3b. Show overnight breaks as labeled separators

Replace raw "1370m break" with:
```tsx
function BreakGap({ minutes }: { minutes: number }) {
  if (minutes > 360) {
    // Overnight or long break — don't show raw minutes
    return null; // Day header handles the visual separation
  }
  const hrs = Math.floor(minutes / 60);
  const mins = minutes % 60;
  const label = hrs > 0 ? `${hrs}h ${mins}m break` : `${mins}m break`;
  return <div className="...">{label}</div>;
}
```

Cross-day gaps are absorbed by day headers. Intra-day gaps >5min shown as human-readable breaks.

#### 3c. Scale TMT scores to 0-100 range

In `schedule.py`, after computing TMT:
```python
# Scale to 0-100 for display
tmt_display = min(100, round(tmt_raw * 1000))
```

This maps 0.01 → 10, 0.05 → 50, 0.1 → 100. More intuitive for users.

Update frontend to display as "TMT 45" instead of "TMT 0.0".

### Files Changed
- `jarvis-frontend/components/app/SchedulePreview.tsx` — Day grouping, break rendering
- `app/api/v1/endpoints/schedule.py` — TMT score scaling

---

## Fix 4: Memory Context Into Decomposition & Voice

### Root Cause

Memory retrieval works (SM-2 scoring, top-K injection) but has 2 breaks in the planning flow:

1. **Memory not in decomposition** — `build_memory_context()` output is injected into `effective_prompt` for brain dump extraction, but `_call_decompose()` uses `enriched_planning_goal` which only has deadline context, not memories
2. **ChatResponse.memories never populated** — The field exists but all `return ChatResponse(...)` calls omit it. MemoryPanel only works via streaming SSE

NOTE: Memory CRUD API endpoints (confirm/dismiss/delete) are covered in Fix 7 to avoid overlap.

### Changes

#### 4a. Inject memory context into decomposition

In `control_policy.py`, before `_call_decompose()`:

```python
# Reuse memory_context already built for effective_prompt earlier in the flow
# Pass it as parameter to _run_plan_day_flow() instead of calling build_memory_context twice
decompose_prompt = enriched_planning_goal
if memory_context:
    decompose_prompt = (
        f"[User Context from Memory]\n{memory_context}\n\n"
        f"[Planning Goal]\n{enriched_planning_goal}"
    )
```

This ensures the Socratic Chunker knows "user hates mornings" when deciding task order, "user prefers 20-min tasks" when setting durations, etc.

#### 4b. Populate `memories` field in ChatResponse

At the end of `execute_agentic_flow()`, before returning ChatResponse:

```python
# Fetch active memories for frontend display
response_memories = None
if memory_store:
    try:
        active_mems = memory_store.get_active_memories(user_id)
        response_memories = [
            {
                "memory_type": m.get("memory_type"),
                "content": m.get("content"),
                "confidence": m.get("confidence", 0.5),
                "source": m.get("source", "inferred"),
                "id": m.get("id"),
            }
            for m in (active_mems or [])[:20]
        ]
    except Exception:
        pass  # Non-critical — don't break response

return ChatResponse(
    ...,
    memories=response_memories,
)
```

Apply to ALL `return ChatResponse(...)` call sites in `control_policy.py`.

#### 4c. Add memory context to voice synthesis

Pass memory context to `synthesize_jarvis_response()` so Jarvis can reference known facts:
```python
await synthesize_jarvis_response(
    execution_summary,
    memory_context=memory_context,  # Add parameter
)
```

This allows responses like "Since you mentioned you study best after lunch, I've scheduled your deep learning tasks for 1-3 PM."

### Files Changed
- `app/services/analytical/control_policy.py` — Pass memory context to decomposition + voice synthesis, populate `memories` in all ChatResponse returns

---

## Fix 5: Draft Negotiation Loop (Reject + Edit Task)

### Root Cause

The architecture spec defines the draft negotiation loop as a core competitive moat: "no competitor does this." Currently only Accept All is wired end-to-end. The other negotiation paths exist as intent handlers in the backend but the frontend doesn't trigger them properly:

- **Edit Task**: `_handle_edit_task` exists (intent_registry.py) — finds task, edits in-place, but does NOT trigger re-scheduling
- **Reject**: `_handle_reject_draft` exists — marks draft rejected but does NOT store rejection reason as memory or generate new approach
- **Chat to Modify**: Sends a new message but doesn't explicitly trigger re-decompose + re-solve on the existing draft

### Changes

#### 5a. Fix Reject → Memory + New Approach

In `control_policy.py`, `_handle_reject_draft`:
```python
async def _handle_reject_draft(self, user_id, draft_id, reason, memory_store, supabase):
    # 1. Mark draft as rejected
    draft_store.reject(draft_id, user_id, components=["tasks", "schedule"])
    
    # 2. Store rejection reason as memory (feedback type)
    if memory_store and reason:
        memory_store.store_memory(user_id, {
            "memory_type": "feedback",
            "content": f"User rejected schedule: {reason}",
            "source": "user",
            "confidence": 0.8,
            "importance": 0.7,
        })
    
    # 3. Extract what to change from rejection reason
    # Re-decompose with the reason as additional context
    new_prompt = f"The user rejected the previous schedule because: '{reason}'. Generate a new approach."
    
    # 4. Re-run plan_day_flow with rejection context
    return await self._run_plan_day_flow(
        user_id, original_goal + f" [Rejected: {reason}]",
        supabase, memory_store, ...
    )
```

Frontend changes in `SchedulePreview.tsx`:
```tsx
// Replace simple reject with rejection reason prompt
const handleReject = () => {
  const reason = window.prompt("What would you change?");
  if (reason) {
    onReject(reason);  // Pass reason to useJarvisChat hook
  }
};
```

In `useJarvisChat.ts`, update `rejectDraftFn`:
```typescript
const rejectDraftFn = useCallback(async (reason?: string) => {
  if (!draftScheduleResponse?.draft_id) return;
  await rejectDraft(draftScheduleResponse.draft_id, ['tasks', 'schedule']);
  // Send rejection reason as a new message to trigger re-plan
  if (reason) {
    await sendMessage(`I rejected the schedule because: ${reason}. Please make a new plan.`);
  }
  setDraftScheduleResponse(null);
}, [draftScheduleResponse]);
```

#### 5b. Fix Edit Task → Re-solve

In `control_policy.py`, `_handle_edit_task`:
```python
async def _handle_edit_task(self, user_id, task_id, edits, supabase, memory_store):
    # 1. Apply edits to the task in user_tasks
    supabase.table("user_tasks") \
        .update(edits) \
        .eq("task_id", task_id).eq("user_id", user_id).execute()
    
    # 2. Trigger re-schedule with modified task
    # Fetch all pending tasks (including the modified one)
    pending = get_all_pending_tasks(user_id, supabase)
    
    # 3. Re-run scheduler
    schedule_result = await run_schedule(pending, time_slots, ...)
    
    # 4. Return new draft
    return ChatResponse(
        intent="EDIT_TASK",
        message="Updated and rescheduled.",
        schedule=schedule_result,
    )
```

Frontend: When user edits a task in the draft and clicks "Save", send the edit to the backend via existing `PATCH /api/v1/drafts/{id}/tasks/{taskId}` then trigger replan.

#### 5c. Chat to Modify (natural language re-decompose)

This already partially works — user sends a new message like "move DSA to afternoon". The fix is to detect this pattern and route to re-decompose instead of full plan-day:

In brain dump extraction, when there's an active draft, the extracted intent should be `EDIT_TASK` or `REARRANGE`, not `PLAN_DAY`. Add to extraction prompt:
```
If the user is modifying an existing schedule (e.g., "move X to afternoon", "swap A and B", 
"make the DSA tasks shorter"), classify as EDIT_TASK or REARRANGE, not PLAN_DAY.
```

### Files Changed
- `app/services/analytical/control_policy.py` — Fix reject handler (store memory + re-plan), fix edit handler (re-solve)
- `app/services/intent_registry.py` — Ensure EDIT_TASK and REJECT_DRAFT handlers call re-solve
- `jarvis-frontend/components/app/SchedulePreview.tsx` — Reject with reason prompt
- `jarvis-frontend/lib/hooks/useJarvisChat.ts` — Reject sends reason as message
- `app/services/analytical/control_policy.py` — Extraction prompt update for active draft context

---

## Fix 6: Memory-to-Constraint Bridge Verification & Fix

### Root Cause

The architecture spec identifies the Memory → Constraint Bridge as "THE breakthrough — no competitor has this." The code for `memories_to_constraints()` exists in `constraint_bridge.py` and IS called from `_run_plan_day_flow()` (line 708-710). However, we need to verify:

1. Is it actually called with a valid `memory_store`?
2. Does it parse memory content into valid `TimeSlot` objects?
3. Are those TimeSlots actually injected into the OR-Tools solver?
4. Does the solver respect them?

### Changes

#### 6a. Audit and log the constraint bridge

Add explicit logging to trace memory → constraint flow:

```python
# In _run_plan_day_flow, after memories_to_constraints call:
memory_constraints = await asyncio.to_thread(
    memories_to_constraints, user_id, memory_store
)
logger.info(
    f"Memory-to-Constraint Bridge: {len(memory_constraints)} constraints "
    f"generated from memories for user {user_id}: "
    f"{[f'{c.name}: {c.start_min}-{c.end_min} ({c.availability})' for c in memory_constraints]}"
)

# Merge with existing time_slots
all_slots = semantic_slots + memory_constraints
```

#### 6b. Guard against None memory_store

In `_run_plan_day_flow`, ensure constraint bridge doesn't silently skip:

```python
if memory_store:
    memory_constraints = await asyncio.to_thread(
        memories_to_constraints, user_id, memory_store
    )
else:
    memory_constraints = []
    logger.warning(f"Memory store unavailable — skipping constraint bridge for user {user_id}")
```

#### 6c. Verify constraint bridge parses correctly

Test with real memory examples:
- Memory: "User avoids work before 11 AM" → should produce `TimeSlot(start_min=0, end_min=180, availability="blocked")`
- PEARL pattern: "User skips tasks between 2-3 PM" (confidence 0.7) → should produce `TimeSlot(start_min=360, end_min=420, availability="minimal_work")`

If parsing fails for common patterns, fix the regex/NLP in `constraint_bridge.py:18-78`.

#### 6d. Surface constraint bridge output in ChatResponse

Add to ChatResponse or schedule payload:
```python
class SchedulePayload(BaseModel):
    ...
    applied_constraints: Optional[list[dict]] = Field(
        default=None,
        description="Memory-derived constraints that shaped this schedule"
    )
```

Frontend can display: "Jarvis avoided scheduling before 11 AM based on your preference."

#### 6e. Frontend: show applied constraints

In `SchedulePreview.tsx`, if `schedule.applied_constraints` is non-empty:
```tsx
{appliedConstraints.length > 0 && (
  <div className="constraints-notice">
    <span>Schedule shaped by your preferences:</span>
    <ul>
      {appliedConstraints.map(c => (
        <li key={c.name}>{c.name}: {c.availability}</li>
      ))}
    </ul>
  </div>
)}
```

This makes the memory → math bridge VISIBLE to users and VCs, not just an invisible backend feature.

### Files Changed
- `app/services/analytical/control_policy.py` — Add logging, guard against None, surface constraints
- `app/services/memory/constraint_bridge.py` — Verify/fix parsing for common patterns
- `app/schemas/context.py` — Add `applied_constraints` to `SchedulePayload`
- `jarvis-frontend/components/app/SchedulePreview.tsx` — Display applied constraints

---

## Fix 7: Memory CRUD API Endpoints

### Root Cause

`memory/store.py` has full CRUD operations (`store_memory`, `get_active_memories`, `update_memory`, `reinforce_memory`, `supersede_memory`, `archive_memory`) but **zero API endpoints** expose them. The MemoryPanel renders Delete/Confirm/Dismiss buttons but they call handlers that are never passed from `chat/page.tsx`.

### Changes

#### 5a. Create memory router (`app/api/v1/endpoints/memories.py`)

Full CRUD endpoints:

```python
router = APIRouter(prefix="/memories", tags=["memories"])

@router.get("/")
async def list_memories(
    user_id: str = Query(...),
    memory_type: Optional[str] = Query(default=None),
    min_confidence: float = Query(default=0.0),
    request: Request = None,
) -> dict:
    """List active memories for a user, optionally filtered by type."""
    memory_store = request.app.state.memory_store
    if memory_type:
        memories = memory_store.get_memories_by_type(user_id, memory_type, min_confidence)
    else:
        memories = memory_store.get_active_memories(user_id)
    return {"memories": memories or [], "count": len(memories or [])}

@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    user_id: str = Query(...),
    request: Request = None,
) -> dict:
    """Archive a memory (set strength to 0, excluded from active queries)."""
    memory_store = request.app.state.memory_store
    memory_store.archive_memory(memory_id, user_id)
    return {"status": "archived"}

@router.post("/{memory_id}/confirm")
async def confirm_memory(
    memory_id: str,
    user_id: str = Query(...),
    request: Request = None,
) -> dict:
    """User confirms a PEARL pattern — reinforce it."""
    memory_store = request.app.state.memory_store
    memory_store.reinforce_memory(memory_id, user_id)
    return {"status": "reinforced"}

@router.post("/{memory_id}/dismiss")
async def dismiss_memory(
    memory_id: str,
    user_id: str = Query(...),
    request: Request = None,
) -> dict:
    """User dismisses a pattern — reduce confidence."""
    memory_store = request.app.state.memory_store
    memory_store.weaken_memory(memory_id, user_id)
    return {"status": "weakened"}
```

#### 5b. Add `weaken_memory()` and `archive_memory()` to store (`app/services/memory/store.py`)

```python
def weaken_memory(self, memory_id: str, user_id: str):
    """Reduce confidence by 0.3, cap stability at 0.5. Used when user dismisses a pattern."""
    existing = self.supabase.table("user_memories") \
        .select("confidence") \
        .eq("id", memory_id).eq("user_id", user_id).single().execute()
    if existing.data:
        new_conf = max(0.0, existing.data["confidence"] - 0.3)
        self.supabase.table("user_memories") \
            .update({"confidence": new_conf, "stability": min(0.5, existing.data.get("stability", 1.0))}) \
            .eq("id", memory_id).eq("user_id", user_id).execute()

def archive_memory(self, memory_id: str, user_id: str):
    """Set strength to 0 — excluded from active queries but preserved for history."""
    self.supabase.table("user_memories") \
        .update({"strength": 0.0}) \
        .eq("id", memory_id).eq("user_id", user_id).execute()
```

#### 5c. Mount router (`app/api/v1/router.py`)

```python
from app.api.v1.endpoints.memories import router as memories_router
api_router.include_router(memories_router)
```

#### 5d. Wire frontend handlers

In `jarvis-frontend/lib/api.ts`:
```typescript
export async function listMemories(): Promise<MemoryRecord[]> {
  const res = await fetch(`${API_BASE}/api/v1/memories/?user_id=${USER_ID}`);
  if (!res.ok) throw new Error(`Failed to list memories`);
  const data = await res.json();
  return data.memories;
}

export async function deleteMemory(memoryId: string): Promise<void> {
  await fetch(`${API_BASE}/api/v1/memories/${memoryId}?user_id=${USER_ID}`, { method: 'DELETE' });
}

export async function confirmMemory(memoryId: string): Promise<void> {
  await fetch(`${API_BASE}/api/v1/memories/${memoryId}/confirm?user_id=${USER_ID}`, { method: 'POST' });
}

export async function dismissMemory(memoryId: string): Promise<void> {
  await fetch(`${API_BASE}/api/v1/memories/${memoryId}/dismiss?user_id=${USER_ID}`, { method: 'POST' });
}
```

In `chat/page.tsx`, pass handlers to MemoryPanel:
```tsx
<MemoryPanel
  memories={memories}
  onDeleteMemory={async (id) => { await deleteMemory(id); refreshMemories(); }}
  onConfirmPattern={async (id) => { await confirmMemory(id); refreshMemories(); }}
  onDismissPattern={async (id) => { await dismissMemory(id); refreshMemories(); }}
/>
```

### Files Changed
- `app/api/v1/endpoints/memories.py` — New file: full CRUD
- `app/services/memory/store.py` — Add `weaken_memory()`, verify `archive_memory()` exists
- `app/api/v1/router.py` — Mount memory router
- `jarvis-frontend/lib/api.ts` — Add `listMemories`, `deleteMemory`, `confirmMemory`, `dismissMemory`
- `jarvis-frontend/app/(app)/chat/page.tsx` — Wire handlers to MemoryPanel props

---

## Fix 8: Memory Panel Independent Data Fetching

### Root Cause

MemoryPanel only receives memories from the last chat response's `memories` field (passed as prop from streaming SSE). This means:
- Opening the panel before any chat shows nothing
- Memories from previous sessions aren't visible
- No way to browse all learned memories

### Changes

#### 6a. Fetch memories on panel open

In `chat/page.tsx`, when memory panel toggles open:
```typescript
const [panelMemories, setPanelMemories] = useState<MemoryRecord[]>([]);

const toggleMemoryPanel = useCallback(async () => {
  setShowMemoryPanel(prev => {
    const next = !prev;
    if (next) {
      // Fetch fresh memories when opening
      listMemories().then(setPanelMemories).catch(() => {});
    }
    return next;
  });
}, []);
```

#### 6b. Merge response memories with fetched memories

When a new chat response arrives with memories, merge with panel state:
```typescript
// In stream complete handler:
if (response.memories) {
  setPanelMemories(prev => {
    const existing = new Set(prev.map(m => m.id));
    const newMems = response.memories.filter(m => !existing.has(m.id));
    return [...newMems, ...prev];
  });
}
```

#### 6c. Refresh after actions

After confirm/dismiss/delete, refetch:
```typescript
const refreshMemories = useCallback(async () => {
  const fresh = await listMemories();
  setPanelMemories(fresh);
}, []);
```

### Files Changed
- `jarvis-frontend/app/(app)/chat/page.tsx` — Independent memory fetch, merge logic, refresh

---

## Fix 9: Workspace Navigation & Rendering Verification

### Root Cause

Calendar task click navigates to `/workspace/{taskId}` (handler exists at `schedule/page.tsx:325`) but there's no visible button or indicator — users don't know they can click. The architecture spec envisions workspace as a first-class feature accessible from the schedule.

### Changes

#### 7a. Add "Open Workspace" button to task cards on schedule

In `schedule/page.tsx`, inside the day view task block:
```tsx
<div className="task-card" onClick={() => router.push(`/workspace/${t.task_id}`)}>
  <div className="task-title">{t.title}</div>
  <div className="task-meta">
    <span>{t.duration_minutes}m</span>
    <button
      className="workspace-btn"
      onClick={(e) => {
        e.stopPropagation();
        router.push(`/workspace/${t.task_id}`);
      }}
    >
      Open Workspace
    </button>
  </div>
  {/* existing complete/skip buttons */}
</div>
```

#### 7b. Add workspace link in draft SchedulePreview

In `SchedulePreview.tsx`, after accept — show link to view tasks in workspace:
```tsx
{acceptState === 'accepted' && (
  <div className="text-sm text-sage-600">
    Tasks saved! Click any task on the calendar to open its workspace.
  </div>
)}
```

### Files Changed
- `jarvis-frontend/app/(app)/schedule/page.tsx` — Add visible workspace button on task cards
- `jarvis-frontend/components/app/SchedulePreview.tsx` — Post-accept guidance

---

## Fix 10: Goal Progress Tracking

### Root Cause

Architecture spec shows per-goal progress (e.g., "Deep Learning Contest: 3/5 tasks done, 60%"). Currently dashboard only shows total task counts with no goal-level breakdown.

### Changes

#### 8a. Add goal progress aggregation endpoint

In `app/api/v1/endpoints/tasks.py`:
```python
@router.get("/goals/progress")
async def goal_progress(
    user_id: str = Query(...),
    request: Request = None,
) -> dict:
    """Aggregate task completion by goal_id."""
    supabase = _get_supabase(request)
    result = supabase.table("user_tasks") \
        .select("goal_id, status, title") \
        .eq("user_id", user_id) \
        .execute()
    
    goals: dict = {}
    for task in (result.data or []):
        gid = task.get("goal_id") or "ungrouped"
        if gid not in goals:
            goals[gid] = {"goal_id": gid, "total": 0, "completed": 0, "tasks": []}
        goals[gid]["total"] += 1
        if task.get("status") == "completed":
            goals[gid]["completed"] += 1
        goals[gid]["tasks"].append({"title": task["title"], "status": task["status"]})
    
    for g in goals.values():
        g["progress_pct"] = round(g["completed"] / g["total"] * 100) if g["total"] > 0 else 0
    
    return {"goals": list(goals.values())}
```

#### 8b. Display on dashboard

In `dashboard/page.tsx`, add GoalProgress section:
```tsx
function GoalProgress({ goals }: { goals: GoalSummary[] }) {
  return (
    <Card>
      <h3>Goal Progress</h3>
      {goals.map(g => (
        <div key={g.goal_id}>
          <div className="flex justify-between">
            <span>{g.goal_id}</span>
            <span>{g.completed}/{g.total} ({g.progress_pct}%)</span>
          </div>
          <ProgressBar value={g.progress_pct} />
        </div>
      ))}
    </Card>
  );
}
```

#### 8c. Frontend API call

In `lib/api.ts`:
```typescript
export async function getGoalProgress(): Promise<GoalSummary[]> {
  const res = await fetch(`${API_BASE}/api/v1/tasks/goals/progress?user_id=${USER_ID}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.goals;
}
```

### Files Changed
- `app/api/v1/endpoints/tasks.py` — Add `/goals/progress` endpoint
- `jarvis-frontend/app/(app)/dashboard/page.tsx` — GoalProgress component
- `jarvis-frontend/lib/api.ts` — Add `getGoalProgress()`
- `jarvis-frontend/lib/types.ts` — Add `GoalSummary` type

---

## Fix 11: PEARL Reliability

### Root Cause

`detect_patterns()` is called via `asyncio.create_task()` (fire-and-forget) after task completion and memory extraction. If it throws, the error is silently swallowed. Dashboard reads PEARL insights from stale localStorage.

### Changes

#### 9a. Wrap PEARL in safe executor with logging

In `app/services/memory/extractor.py` and `app/api/v1/endpoints/tasks.py`, replace bare `asyncio.create_task`:

```python
async def safe_detect_patterns(user_id: str, supabase, memory_store, logger):
    """Run PEARL pattern detection with error handling and logging."""
    try:
        patterns = await asyncio.to_thread(
            detect_patterns, user_id, supabase, memory_store
        )
        if patterns:
            logger.info(f"PEARL detected {len(patterns)} patterns for user {user_id}")
        return patterns
    except Exception as e:
        logger.error(f"PEARL pattern detection failed for user {user_id}: {e}")
        return []

# Usage:
asyncio.create_task(safe_detect_patterns(user_id, supabase, memory_store, logger))
```

#### 9b. Include PEARL insights in ChatResponse

Add field to ChatResponse:
```python
pearl_insights: Optional[list[dict]] = Field(
    default=None,
    description="Recently detected behavioral patterns from PEARL."
)
```

Populate from memory store (behavioral_pattern type, recent):
```python
if memory_store:
    patterns = memory_store.get_memories_by_type(user_id, "behavioral_pattern", min_confidence=0.5)
    response.pearl_insights = [
        {"content": p["content"], "confidence": p["confidence"]}
        for p in (patterns or [])[:5]
    ]
```

#### 9c. Dashboard fetches fresh insights

Replace localStorage-based PEARL insights with API call:
```typescript
// In dashboard/page.tsx
const [insights, setInsights] = useState<PearlInsight[]>([]);
useEffect(() => {
  listMemories()
    .then(mems => mems.filter(m => m.memory_type === 'behavioral_pattern'))
    .then(patterns => setInsights(patterns.slice(0, 5)));
}, []);
```

### Files Changed
- `app/services/memory/extractor.py` — Safe PEARL wrapper with logging
- `app/api/v1/endpoints/tasks.py` — Use safe wrapper for task complete/skip
- `app/schemas/context.py` — Add `pearl_insights` to ChatResponse
- `app/services/analytical/control_policy.py` — Populate `pearl_insights` in responses
- `jarvis-frontend/app/(app)/dashboard/page.tsx` — Fetch fresh insights from API

---

## Fix 12: Task Completion Signals Queryable

### Root Cause

`task_completion_signals` table is populated on every task complete/skip (quality 0-5, actual duration, timestamp) but no endpoint exposes this data. Future DKT/RL needs it, and for now, it powers better analytics.

### Changes

#### 10a. Add completion signals endpoint

In `app/api/v1/endpoints/tasks.py`:
```python
@router.get("/completion-signals")
async def list_completion_signals(
    user_id: str = Query(...),
    limit: int = Query(default=50, le=200),
    request: Request = None,
) -> dict:
    """Return recent task completion signals for analytics and future DKT/RL."""
    supabase = _get_supabase(request)
    result = supabase.table("task_completion_signals") \
        .select("*") \
        .eq("user_id", user_id) \
        .order("created_at", desc=True) \
        .limit(limit) \
        .execute()
    
    # Compute summary stats
    signals = result.data or []
    avg_quality = sum(s.get("quality", 0) for s in signals) / len(signals) if signals else 0
    
    return {
        "signals": signals,
        "count": len(signals),
        "avg_quality": round(avg_quality, 1),
    }
```

#### 10b. Surface in dashboard stats

Add average quality score to StatsStrip on dashboard:
```typescript
// Fetch signals for quality metric
const signals = await fetch(`${API_BASE}/api/v1/tasks/completion-signals?user_id=${USER_ID}&limit=20`);
const { avg_quality } = await signals.json();
// Display: "Avg Quality: 3.8/5"
```

### Files Changed
- `app/api/v1/endpoints/tasks.py` — Add `/completion-signals` endpoint
- `jarvis-frontend/app/(app)/dashboard/page.tsx` — Show avg quality in stats
- `jarvis-frontend/lib/api.ts` — Add `getCompletionSignals()`

---

## Verification Criteria

### Fix 1: Decomposition Quality
- [ ] "Plan my week - deep learning contest Friday, calculus exam Monday" produces tasks like "Study CNNs", "Practice calculus integration" — NOT "Reflect on Last Week's Performance"
- [ ] `planning_goal` field preserves domain-specific details
- [ ] `subject_context` field is populated correctly

### Fix 2: Accept All → Calendar
- [ ] Click Accept All → navigate to /schedule → tasks appear on calendar at correct times
- [ ] `scheduled_start`/`scheduled_end` are read correctly in transform
- [ ] ChatResponse.schedule uses typed `SchedulePayload`

### Fix 3: Schedule Rendering
- [ ] Multi-day schedule shows day headers ("Today", "Tomorrow", etc.)
- [ ] No "1370m break" — overnight gaps handled by day separators
- [ ] TMT scores display as 0-100 (e.g., "TMT 45") not 0.0
- [ ] Tasks within each day are in chronological order

### Fix 4: Memory Context
- [ ] Decomposition LLM receives memory context (verified by logging decompose prompt)
- [ ] ChatResponse includes `memories` array on both streaming and REST endpoints
- [ ] Voice of Jarvis references user's known preferences in response text

### Fix 5: Draft Negotiation Loop
- [ ] Reject with reason → stores rejection as feedback memory → generates new schedule approach
- [ ] Edit Task → saves modification → triggers re-solve via OR-Tools → returns updated draft
- [ ] Chat to Modify (e.g., "move DSA to afternoon") routes to EDIT_TASK intent, not PLAN_DAY
- [ ] Frontend Reject button prompts for reason before rejecting

### Fix 6: Memory-to-Constraint Bridge
- [ ] `memories_to_constraints()` is called during plan-day flow (verified by logs)
- [ ] Memory "avoids work before 11 AM" produces blocked TimeSlot(0-180min)
- [ ] PEARL pattern constraints produce minimal_work TimeSlots
- [ ] `applied_constraints` returned in SchedulePayload and displayed in frontend
- [ ] Guard logs warning if memory_store is None (not silent skip)

### Fix 7: Memory CRUD API
- [ ] `GET /api/v1/memories/` returns user's active memories
- [ ] `DELETE /api/v1/memories/{id}` archives a memory
- [ ] `POST /api/v1/memories/{id}/confirm` reinforces pattern (stability/confidence increase)
- [ ] `POST /api/v1/memories/{id}/dismiss` weakens pattern (confidence decrease)
- [ ] MemoryPanel delete/confirm/dismiss buttons call these endpoints and refresh

### Fix 8: Memory Panel Independent Fetch
- [ ] Opening MemoryPanel fetches fresh memories from API (not just from last chat response)
- [ ] Panel shows all active memories even before any chat in current session
- [ ] New chat response memories merge with panel state
- [ ] Error state shown if API unreachable ("Unable to load memories")

### Fix 9: Workspace Navigation & Verification
- [ ] Schedule day view task cards show visible "Open Workspace" button
- [ ] Post-accept message guides user to workspace
- [ ] Workspace page renders completion criteria with checkboxes
- [ ] Progress percentage works correctly
- [ ] Study materials appear from RAG + web search

### Fix 10: Goal Progress
- [ ] `GET /api/v1/tasks/goals/progress` returns per-goal completion stats with human-readable goal name (not UUID)
- [ ] Dashboard shows progress bars per goal (e.g., "Deep Learning Contest: 3/5, 60%")

### Fix 11: PEARL Reliability
- [ ] `detect_patterns()` wrapped in safe executor with error logging
- [ ] ChatResponse includes `pearl_insights` field populated from memory store
- [ ] Dashboard fetches fresh PEARL insights from API, not localStorage

### Fix 12: Completion Signals
- [ ] `GET /api/v1/tasks/completion-signals` returns recent quality signals
- [ ] Dashboard shows average quality score in stats strip

---

## Risk Assessment

| Fix | Risk | Mitigation |
|-----|------|-----------|
| 1. Extraction prompt | LLM may over-preserve (include irrelevant details) | Test with 5 diverse inputs before demo |
| 2. Schedule typing | Breaking API contract change | Update frontend types simultaneously |
| 3. Day grouping | Edge cases (midnight tasks, timezone) | Use `horizon_start` timezone throughout |
| 4. Memory in decomposition | Memory context may confuse Socratic Chunker if too verbose | Limit to top-5 most relevant memories, format concisely |
| 5. Draft negotiation | Re-solve may produce INFEASIBLE if edit conflicts | Reuse existing INFEASIBLE handling (graceful fallback) |
| 6. Constraint bridge | Regex parsing may miss some memory formats | Log all parse failures, add patterns iteratively |
| 7. Memory CRUD | New endpoints + router wiring | Follow existing endpoint patterns in tasks.py |
| 10. Goal progress | goal_id may be null for some tasks | Group nulls under "ungrouped", skip in display |

---

## Implementation Order

**Batch 1 — Demo Critical (parallel):**
- Fix 2 (Accept All → Calendar) — smallest change, biggest visible impact
- Fix 1 (Extraction quality) — second biggest demo impact

**Batch 2 — Competitive Moats (parallel):**
- Fix 5 (Draft Negotiation — Reject + Edit Task) — core differentiator
- Fix 6 (Memory-to-Constraint Bridge — verify + surface) — core differentiator

**Batch 3 — Visual + UX (parallel):**
- Fix 3 (Schedule rendering) — day grouping, TMT scaling
- Fix 9 (Workspace navigation + verification) — button affordance + rendering check

**Batch 4 — Memory System (sequential):**
- Fix 7 (Memory CRUD API) — backend endpoints first
- Fix 8 (Memory Panel fetch) — frontend wiring depends on Fix 7
- Fix 4 (Memory into decomposition + voice) — uses memory store from Fix 7

**Batch 5 — Analytics + Reliability (parallel, post-demo OK):**
- Fix 10 (Goal progress) — new endpoint + dashboard component
- Fix 11 (PEARL reliability) — safe wrapper + fresh insights
- Fix 12 (Completion signals) — new endpoint + dashboard stat

Batches 1-3 are **must-have for demo**. Batch 4 is high priority. Batch 5 is post-demo.
