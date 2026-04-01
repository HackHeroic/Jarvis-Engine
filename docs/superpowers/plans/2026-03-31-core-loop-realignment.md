# Core Loop Realignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 12 issues where the Jarvis implementation drifted from the architecture spec — making the core demo loop (chat → decompose → schedule → accept → calendar) bulletproof for the VC pitch.

**Architecture:** The fixes are organized into 5 batches by priority. Batches 1-4 are must-have. Batch 5 is post-demo. Each task touches specific files across the Python backend (`Jarvis-Engine/`) and Next.js frontend (`jarvis-frontend/`). No new infrastructure — all changes are code-level realignment.

**Tech Stack:** Python 3.11 / FastAPI / Pydantic v2 / Supabase / OR-Tools (backend), Next.js / TypeScript / React / Tailwind (frontend)

**Spec:** `docs/superpowers/specs/2026-03-31-core-loop-realignment-design.md`

**Key Constraint:** Never run two 27B LLM calls concurrently (`asyncio.gather`) — OOM risk on M4 Pro 24GB.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `Jarvis-Engine/app/schemas/context.py` | Modify | Add `subject_context` to BrainDumpExtraction, add `SchedulePayload` model, add `pearl_insights` + `applied_constraints` fields |
| `Jarvis-Engine/app/services/analytical/control_policy.py` | Modify | Fix extraction prompt, enrich planning context, inject memory into decomposition, populate memories in ChatResponse, construct SchedulePayload |
| `jarvis-frontend/lib/transforms.ts` | Modify | Add `scheduled_start`/`scheduled_end` fallback in task transform |
| `jarvis-frontend/lib/types.ts` | Modify | Add `SchedulePayload` type, `GoalSummary` type |
| `jarvis-frontend/components/app/SchedulePreview.tsx` | Modify | Day grouping, smart break gaps, applied constraints display |
| `Jarvis-Engine/app/api/v1/endpoints/schedule.py` | Modify | Scale TMT scores to 0-100 |
| `Jarvis-Engine/app/api/v1/endpoints/memories.py` | Create | Full CRUD: list, delete, confirm, dismiss |
| `Jarvis-Engine/app/services/memory/store.py` | Modify | Add `weaken_memory()` method |
| `Jarvis-Engine/app/api/v1/router.py` | Modify | Mount memories router |
| `Jarvis-Engine/app/services/memory/constraint_bridge.py` | Modify | Add logging, guard None |
| `Jarvis-Engine/app/services/analytical/voice_of_jarvis.py` | Modify | Accept `memory_context` param |
| `jarvis-frontend/lib/api.ts` | Modify | Add memory CRUD, goal progress, completion signals API functions |
| `jarvis-frontend/app/(app)/chat/page.tsx` | Modify | Wire memory panel handlers, independent fetch |
| `jarvis-frontend/lib/hooks/useJarvisChat.ts` | Modify | Update rejectDraftFn to prompt for reason |
| `jarvis-frontend/app/(app)/schedule/page.tsx` | Modify | Add workspace button on task cards |
| `jarvis-frontend/app/(app)/dashboard/page.tsx` | Modify | Goal progress, fresh PEARL insights, avg quality |
| `Jarvis-Engine/app/api/v1/endpoints/tasks.py` | Modify | Add `/goals/progress` and `/completion-signals` endpoints |
| `Jarvis-Engine/app/services/memory/extractor.py` | Modify | Safe PEARL wrapper |

---

## Batch 1 — Demo Critical

### Task 1: Fix Accept All → Calendar (Fix 2a — Frontend Transform)

**Files:**
- Modify: `jarvis-frontend/lib/transforms.ts:26-64`

- [ ] **Step 1: Update the transform to read `scheduled_start`/`scheduled_end`**

In `jarvis-frontend/lib/transforms.ts`, replace lines 31-42:

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
      const dur = (t.duration_minutes as number) || 25;
      const endTime = t.end_time
        ? new Date(t.end_time as string)
        : t.scheduled_end
          ? new Date(t.scheduled_end as string)
          : new Date(startTime.getTime() + dur * 60_000);
```

- [ ] **Step 2: Verify the frontend builds**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -5`
Expected: Build succeeds (or only pre-existing warnings)

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/transforms.ts
git commit -m "fix: read scheduled_start/scheduled_end in task transform for calendar display"
```

---

### Task 2: Add SchedulePayload Type to Backend (Fix 2b)

**Files:**
- Modify: `Jarvis-Engine/app/schemas/context.py:240`

- [ ] **Step 1: Add `SchedulePayload` model after the existing imports/models, before ChatResponse**

Add this class in `app/schemas/context.py` before the `ChatResponse` class:

```python
class SchedulePayload(BaseModel):
    """Typed schedule payload returned in ChatResponse."""
    schedule: dict = Field(description="Map of task_id -> {start_min, end_min, tmt_score, title}")
    horizon_start: str = Field(description="ISO-8601 datetime for minute-0 of the schedule")
    horizon_minutes: int = Field(default=0, description="Total horizon length in minutes")
    daily_cap_minutes: Optional[int] = Field(default=None, description="Adaptive daily cap used")
    draft_id: Optional[str] = Field(default=None, description="Draft ID if schedule is a draft")
    status: Optional[str] = Field(default=None, description="'draft' or 'final'")
    applied_constraints: Optional[List[dict]] = Field(
        default=None,
        description="Memory-derived constraints that shaped this schedule"
    )
```

- [ ] **Step 2: Update `ChatResponse.schedule` field type**

Change line 240 from:
```python
    schedule: Optional[dict] = Field(
        default=None,
        description="OR-Tools output: status, schedule, goal_metadata",
    )
```
To:
```python
    schedule: Optional[SchedulePayload] = Field(
        default=None,
        description="Typed schedule payload from OR-Tools solver",
    )
```

- [ ] **Step 3: Add `pearl_insights` field to ChatResponse**

Add after the `memories` field (around line 295):
```python
    pearl_insights: Optional[List[dict]] = Field(
        default=None,
        description="Recently detected behavioral patterns from PEARL.",
    )
```

- [ ] **Step 4: Add `subject_context` field to BrainDumpExtraction**

Add after `deadline_update` field (after line 89):
```python
    subject_context: Optional[List[str]] = Field(
        default=None,
        description="Specific subjects, topics, exams, contests mentioned. "
        "Preserve verbatim with any associated deadlines. "
        "E.g. ['deep learning contest - Friday', 'calculus exam - Monday']",
    )
```

- [ ] **Step 5: Verify the app starts**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.schemas.context import ChatResponse, SchedulePayload, BrainDumpExtraction; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/schemas/context.py
git commit -m "feat: add SchedulePayload model, subject_context field, pearl_insights field"
```

---

### Task 3: Wire SchedulePayload in Control Policy (Fix 2b continued)

**Files:**
- Modify: `Jarvis-Engine/app/services/analytical/control_policy.py:999-1010`

- [ ] **Step 1: Import SchedulePayload at top of control_policy.py**

Add to the imports section:
```python
from app.schemas.context import SchedulePayload
```

- [ ] **Step 2: Replace the raw dict schedule in the PLAN_DAY success return**

Find the return statement around line 999-1010 where `schedule=schedule_response.model_dump(mode='json')` and replace with:

```python
        schedule_payload = SchedulePayload(
            schedule=schedule_response.model_dump(mode='json').get("schedule", {}),
            horizon_start=schedule_response.model_dump(mode='json').get("horizon_start", ""),
            horizon_minutes=schedule_response.model_dump(mode='json').get("horizon_minutes", 0),
            daily_cap_minutes=schedule_response.model_dump(mode='json').get("daily_cap_minutes"),
            draft_id=draft.draft_id if draft else None,
            status="draft",
        )
```

Then use `schedule=schedule_payload` in the ChatResponse.

NOTE: Read the actual code carefully before editing — the exact shape of `schedule_response` may need adaptation. The key change is wrapping in `SchedulePayload` instead of passing raw dict.

- [ ] **Step 3: Verify the app starts**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.services.analytical.control_policy import execute_agentic_flow; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py
git commit -m "feat: use typed SchedulePayload in ChatResponse instead of raw dict"
```

---

### Task 4: Update Frontend Types for SchedulePayload (Fix 2c)

**Files:**
- Modify: `jarvis-frontend/lib/types.ts`

- [ ] **Step 1: Add `SchedulePayload` interface**

Add near the existing `ScheduleResponse` type:

```typescript
export interface SchedulePayload {
  schedule: Record<string, TaskSchedule>;
  horizon_start: string;
  horizon_minutes: number;
  daily_cap_minutes?: number;
  draft_id?: string;
  status?: 'draft' | 'final';
  applied_constraints?: AppliedConstraint[];
}

export interface AppliedConstraint {
  name: string;
  start_min: number;
  end_min: number;
  availability: string;
  source: string;
}

export interface GoalSummary {
  goal_id: string;
  total: number;
  completed: number;
  progress_pct: number;
  tasks: { title: string; status: string }[];
}
```

- [ ] **Step 2: Verify build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit 2>&1 | head -20`
Expected: No new errors

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/types.ts
git commit -m "feat: add SchedulePayload, AppliedConstraint, GoalSummary types"
```

---

### Task 5: Fix Brain Dump Extraction Prompt (Fix 1)

**Files:**
- Modify: `Jarvis-Engine/app/services/analytical/control_policy.py:62-88`

- [ ] **Step 1: Update the BRAIN_DUMP_EXTRACTION_PROMPT**

Replace line 65 (the `planning_goal` instruction) from:
```python
    "planning_goal: Schedule tasks, break down goal, plan day. Clean goal string only (e.g. 'Plan my day to write 3 posts'). "
```
To:
```python
    "planning_goal: The user's scheduling intent WITH all specific subjects, topics, exams, contests, and deadlines preserved verbatim. "
    "Include domain details — never reduce to a generic summary. "
    "E.g. 'Plan my week for deep learning contest Friday and calculus exam Monday', NOT just 'Plan my week'. "
```

- [ ] **Step 2: Add `subject_context` extraction instruction**

Add before the `"Return strictly valid JSON."` line (before line 87):
```python
    "subject_context: List of specific subjects/topics/exams mentioned with any associated time references. "
    "E.g. [\"deep learning contest - Friday\", \"calculus exam - Monday\"]. Use null if no specific subjects mentioned.\n"
```

- [ ] **Step 3: Update `_build_planning_context` to include deadline_update and subject_context**

After line 189 (end of the existing function), the function returns `planning_goal` as fallback. We need to modify the function signature and add enrichment. Replace the function (lines 154-189):

```python
def _build_planning_context(
    user_id: str,
    planning_goal: str,
    supabase_client: Any,
    extraction: Any = None,
) -> str:
    """Enrich planning_goal with deadline context from user_plan_updates and extraction."""
    enriched = planning_goal
    if supabase_client and user_id:
        try:
            result = (
                supabase_client.table("user_plan_updates")
                .select("deadline_date, deadline_raw, context_snippet")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(10)
                .execute()
            )
            rows = result.data or []
            goal_words = set(planning_goal.lower().split())
            for r in rows:
                snippet = (r.get("context_snippet") or "").lower()
                if not snippet or not goal_words:
                    continue
                snippet_words = set(snippet.split())
                if goal_words & snippet_words:
                    deadline_date = r.get("deadline_date")
                    if deadline_date:
                        enriched = f"[Context: Known deadline for this goal: {deadline_date}.] {enriched}"
                    break
            else:
                for r in rows:
                    deadline_date = r.get("deadline_date")
                    if deadline_date:
                        enriched = f"[Context: Known deadline: {deadline_date}.] {enriched}"
                        break
        except Exception as e:
            print(f"[Control Policy] _build_planning_context failed: {e}")

    # Append deadline from current extraction
    if extraction and hasattr(extraction, "deadline_update") and extraction.deadline_update:
        enriched += f" [Deadline: {extraction.deadline_update}]"

    # Append subject context from current extraction
    if extraction and hasattr(extraction, "subject_context") and extraction.subject_context:
        enriched += f" [Subjects: {', '.join(extraction.subject_context)}]"

    return enriched
```

- [ ] **Step 4: Update the call site to pass `extraction`**

Find where `_build_planning_context` is called (around line 731) and add the `extraction` parameter. The call currently looks like:
```python
enriched_planning_goal = _build_planning_context(user_id, planning_goal, supabase)
```
Change to:
```python
enriched_planning_goal = _build_planning_context(user_id, planning_goal, supabase, extraction=brain_dump_result)
```

NOTE: `brain_dump_result` is the `BrainDumpExtraction` object from earlier in the flow. Read the surrounding code to find the correct variable name — it may be `extraction_result`, `bde`, or stored differently.

- [ ] **Step 5: Verify import and syntax**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.services.analytical.control_policy import _build_planning_context; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py
git commit -m "fix: preserve domain context in brain dump extraction, enrich decomposition input"
```

---

## Batch 2 — Competitive Moats

### Task 6: Draft Reject with Reason (Fix 5a)

**Files:**
- Modify: `jarvis-frontend/components/app/SchedulePreview.tsx`
- Modify: `jarvis-frontend/lib/hooks/useJarvisChat.ts:573-586`

- [ ] **Step 1: Update `rejectDraftFn` in useJarvisChat.ts to accept a reason**

Replace lines 573-586:
```typescript
  const rejectDraftFn = useCallback(async (reason?: string) => {
    const draftId =
      draftScheduleResponse?.draft_id ||
      draftScheduleResponse?.schedule?.draft_id;
    if (draftId) {
      try {
        await rejectDraft(draftId, ["tasks", "schedule"]);
      } catch {
        // Best-effort: still clear local state even if backend call fails
      }
    }
    clearDraftSchedule();
    setDraftScheduleResponse(null);
    // Send rejection reason as a new message to trigger re-plan
    if (reason) {
      // Small delay to let state clear before sending new message
      setTimeout(() => {
        sendMessage(`I rejected the schedule because: ${reason}. Please make a new plan.`);
      }, 200);
    }
  }, [draftScheduleResponse, sendMessage]);
```

- [ ] **Step 2: Update the Reject button in SchedulePreview.tsx to prompt for reason**

Find the Reject button's `onClick` handler and change it to prompt the user:
```typescript
onClick={() => {
  const reason = window.prompt("What would you like changed?");
  if (reason !== null) {
    onReject(reason || undefined);
  }
}}
```

NOTE: The exact location depends on how `onReject` is passed as a prop. Read the component to find the Reject button and its handler. The `onReject` prop type may need updating from `() => void` to `(reason?: string) => void`.

- [ ] **Step 3: Verify build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -5`

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/hooks/useJarvisChat.ts components/app/SchedulePreview.tsx
git commit -m "feat: reject draft with reason, sends reason as new message to trigger re-plan"
```

---

### Task 7: Memory-to-Constraint Bridge Logging & Surfacing (Fix 6)

**Files:**
- Modify: `Jarvis-Engine/app/services/analytical/control_policy.py:708-711`
- Modify: `Jarvis-Engine/app/services/memory/constraint_bridge.py`

- [ ] **Step 1: Add logging and guard in control_policy.py around the constraint bridge call**

Find lines 708-711 and replace with:

```python
        from app.services.memory.constraint_bridge import memories_to_constraints
        if memory_store:
            memory_constraints = await asyncio.to_thread(
                memories_to_constraints, user_id, memory_store
            )
            if memory_constraints:
                logger.info(
                    f"Memory-to-Constraint Bridge: {len(memory_constraints)} constraints "
                    f"for user {user_id}: "
                    f"{[f'{c.name}: {c.start_min}-{c.end_min} ({c.availability})' for c in memory_constraints]}"
                )
        else:
            memory_constraints = []
            logger.warning(f"Memory store unavailable — skipping constraint bridge for user {user_id}")
```

- [ ] **Step 2: Store applied constraints for ChatResponse**

After the scheduler runs, collect the memory constraints into a list of dicts that can be passed to `SchedulePayload.applied_constraints`:

```python
        applied_constraints_data = [
            {"name": c.name, "start_min": c.start_min, "end_min": c.end_min,
             "availability": c.availability, "source": getattr(c, "source", "memory")}
            for c in memory_constraints
        ] if memory_constraints else None
```

Then pass `applied_constraints=applied_constraints_data` when constructing the `SchedulePayload`.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py
git commit -m "feat: log and surface memory-to-constraint bridge output in schedule payload"
```

---

## Batch 3 — Visual + UX

### Task 8: Multi-Day Schedule Rendering (Fix 3)

**Files:**
- Modify: `jarvis-frontend/components/app/SchedulePreview.tsx`

- [ ] **Step 1: Add `groupByDay` helper and `DayHeader` component**

Add above the main component:

```typescript
type DayGroup = {
  date: Date;
  label: string;
  entries: TimelineEntry[];
};

function formatDayLabel(date: Date): string {
  const today = new Date();
  const tomorrow = new Date(today);
  tomorrow.setDate(today.getDate() + 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === tomorrow.toDateString()) return "Tomorrow";
  return date.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

function groupByDay(entries: TimelineEntry[], horizonStart: string): DayGroup[] {
  const base = new Date(horizonStart);
  const groups: Map<string, DayGroup> = new Map();
  for (const entry of entries) {
    const taskDate = new Date(base.getTime() + entry.startMin * 60_000);
    const dateKey = taskDate.toDateString();
    if (!groups.has(dateKey)) {
      groups.set(dateKey, { date: taskDate, label: formatDayLabel(taskDate), entries: [] });
    }
    groups.get(dateKey)!.entries.push(entry);
  }
  return Array.from(groups.values()).sort((a, b) => a.date.getTime() - b.date.getTime());
}

function DayHeader({ label, taskCount }: { label: string; taskCount: number }) {
  return (
    <div className="flex items-center gap-2 py-2 mt-3 first:mt-0">
      <span className="text-xs font-semibold text-foreground/80 uppercase tracking-wide">{label}</span>
      <span className="text-[10px] text-muted/60">{taskCount} tasks</span>
      <div className="flex-1 h-px bg-border" />
    </div>
  );
}
```

- [ ] **Step 2: Update the `BreakGap` component to handle overnight gaps**

Replace lines 100-116:

```typescript
function BreakGap({ minutes }: { minutes: number }) {
  if (minutes <= 0 || minutes > 360) return null; // Overnight gaps handled by DayHeader
  const hrs = Math.floor(minutes / 60);
  const mins = minutes % 60;
  const label = hrs > 0 ? `${hrs}h ${mins}m break` : `${mins}m break`;
  return (
    <div className="flex gap-3 items-stretch">
      <div className="w-[72px]" />
      <div className="flex flex-col items-center w-3">
        <div className="flex-1 w-px border-l border-dashed border-border" />
      </div>
      <div className="flex-1 flex items-center py-1.5">
        <span className="text-[10px] text-muted/60 italic">{label}</span>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Replace flat rendering with day-grouped rendering**

Find the section that maps over `entries` (around line 172+) and replace with:

```tsx
{(() => {
  const horizonStart = schedule.horizon_start || new Date().toISOString();
  const dayGroups = groupByDay(entries, horizonStart);
  return dayGroups.map((group) => (
    <div key={group.label}>
      <DayHeader label={group.label} taskCount={group.entries.length} />
      {group.entries.map((entry, i) => {
        const prevEnd = i > 0 ? group.entries[i - 1].endMin : entry.startMin;
        const gap = entry.startMin - prevEnd;
        return (
          <div key={entry.taskId}>
            {gap > 5 && <BreakGap minutes={gap} />}
            <TimelineBlock entry={entry} horizonStart={horizonStart} />
          </div>
        );
      })}
    </div>
  ));
})()}
```

NOTE: Read the actual rendering section carefully. The component structure may differ slightly. The key change is: group entries by day, render DayHeader per group, only show BreakGap for intra-day gaps ≤360min.

- [ ] **Step 4: Add applied constraints display**

After the schedule timeline, before the Accept/Reject buttons:

```tsx
{schedule.applied_constraints && schedule.applied_constraints.length > 0 && (
  <div className="px-4 py-2 rounded-lg bg-sage-50 dark:bg-sage-900/20 text-xs text-sage-700 dark:text-sage-300 mt-2">
    <span className="font-medium">Schedule shaped by your preferences:</span>
    <ul className="mt-1 space-y-0.5">
      {schedule.applied_constraints.map((c: AppliedConstraint, i: number) => (
        <li key={i}>{c.name.replace(/_/g, ' ')}</li>
      ))}
    </ul>
  </div>
)}
```

- [ ] **Step 5: Verify build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -5`

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/SchedulePreview.tsx
git commit -m "feat: multi-day schedule rendering with day headers, smart breaks, applied constraints"
```

---

### Task 9: Scale TMT Scores (Fix 3c)

**Files:**
- Modify: `Jarvis-Engine/app/api/v1/endpoints/schedule.py:56-75`

- [ ] **Step 1: Update `_compute_tmt_priority` to return scaled display score**

Find lines 72-75 and replace:

```python
    value = difficulty_weight
    motivation = (EXPECTANCY * value) / (IMPULSIVENESS * delay_hours)
    priority_score = max(1, int(motivation * 100))
    tmt_display = min(100, round(motivation * 1000))
    return (tmt_display, priority_score)
```

NOTE: The first element of the tuple is now the display-friendly 0-100 score. Check all call sites that use this return value — they may expect `(raw_float, int)`. If so, keep `motivation` for the priority calculation and return `tmt_display` separately:

```python
    value = difficulty_weight
    motivation = (EXPECTANCY * value) / (IMPULSIVENESS * delay_hours)
    priority_score = max(1, int(motivation * 100))
    tmt_display = min(100, round(motivation * 1000))
    return (tmt_display, priority_score)
```

Find where `tmt_scores[task_id]` is set and ensure it uses `tmt_display` (the first element), not `motivation`.

- [ ] **Step 2: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/api/v1/endpoints/schedule.py
git commit -m "fix: scale TMT scores to 0-100 range for readable display"
```

---

### Task 10: Workspace Navigation Button (Fix 9)

**Files:**
- Modify: `jarvis-frontend/app/(app)/schedule/page.tsx`

- [ ] **Step 1: Add a visible "Open Workspace" link on task cards in the day view**

Find the day-view task card rendering (where task title and duration are shown) and add a small button:

```tsx
<button
  className="text-[10px] text-terra-600 hover:text-terra-700 underline"
  onClick={(e) => {
    e.stopPropagation();
    router.push(`/workspace/${t.task_id}`);
  }}
>
  Workspace
</button>
```

Place it next to the existing Complete/Skip buttons.

- [ ] **Step 2: Verify build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add app/\(app\)/schedule/page.tsx
git commit -m "feat: add visible Workspace button on schedule task cards"
```

---

## Batch 4 — Memory System

### Task 11: Memory CRUD Backend Endpoints (Fix 7)

**Files:**
- Create: `Jarvis-Engine/app/api/v1/endpoints/memories.py`
- Modify: `Jarvis-Engine/app/services/memory/store.py`
- Modify: `Jarvis-Engine/app/api/v1/router.py`

- [ ] **Step 1: Add `weaken_memory` to store.py**

Add after the `archive_memory` method (after line 177):

```python
    def weaken_memory(self, memory_id: str, user_id: str = None) -> bool:
        """Reduce confidence by 0.3, cap stability at 0.5. Used when user dismisses a pattern."""
        if not user_id:
            return False
        mem = self.get_memory(memory_id, user_id=user_id)
        if not mem:
            return False
        new_conf = max(0.0, mem.get("confidence", 0.5) - 0.3)
        new_stab = min(0.5, mem.get("stability", 1.0))
        return self.update_memory(memory_id, {
            "confidence": new_conf,
            "stability": new_stab,
        }, user_id=user_id)
```

- [ ] **Step 2: Create `memories.py` endpoint file**

Create `Jarvis-Engine/app/api/v1/endpoints/memories.py`:

```python
"""Memory CRUD endpoints for Jarvis."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter()


def _get_memory_store(request: Request):
    store = getattr(request.app.state, "memory_store", None)
    if not store:
        raise HTTPException(status_code=503, detail="Memory store not available")
    return store


@router.get("/")
async def list_memories(
    user_id: str = Query(..., description="User ID"),
    memory_type: Optional[str] = Query(default=None, description="Filter by memory type"),
    min_confidence: float = Query(default=0.0, description="Minimum confidence threshold"),
    request: Request = None,
) -> dict:
    """List active memories for a user, optionally filtered by type."""
    store = _get_memory_store(request)
    if memory_type:
        memories = store.get_memories_by_type(user_id, memory_type, min_confidence)
    else:
        memories = store.get_active_memories(user_id)
    return {"memories": memories or [], "count": len(memories or [])}


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """Archive a memory (set strength to 0, excluded from active queries)."""
    store = _get_memory_store(request)
    success = store.archive_memory(memory_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "archived"}


@router.post("/{memory_id}/confirm")
async def confirm_memory(
    memory_id: str,
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """User confirms a PEARL pattern — reinforce it."""
    store = _get_memory_store(request)
    success = store.reinforce_memory(memory_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "reinforced"}


@router.post("/{memory_id}/dismiss")
async def dismiss_memory(
    memory_id: str,
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """User dismisses a pattern — reduce confidence."""
    store = _get_memory_store(request)
    success = store.weaken_memory(memory_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "weakened"}
```

- [ ] **Step 3: Mount in router.py**

Add to `Jarvis-Engine/app/api/v1/router.py` after the existing imports:

```python
from app.api.v1.endpoints.memories import router as memories_router
```

And add before the final line:
```python
api_router.include_router(memories_router, prefix="/memories", tags=["Memories"])
```

- [ ] **Step 4: Verify the app starts**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.api.v1.router import api_router; print(f'{len(api_router.routes)} routes OK')"`

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/api/v1/endpoints/memories.py app/services/memory/store.py app/api/v1/router.py
git commit -m "feat: add memory CRUD API endpoints (list, delete, confirm, dismiss)"
```

---

### Task 12: Frontend Memory API Functions + Panel Wiring (Fix 7d + Fix 8)

**Files:**
- Modify: `jarvis-frontend/lib/api.ts`
- Modify: `jarvis-frontend/app/(app)/chat/page.tsx`

- [ ] **Step 1: Add memory API functions to api.ts**

Add at the end of the file:

```typescript
export async function listMemories(): Promise<MemoryRecord[]> {
  const res = await fetch(`${API_BASE}/api/v1/memories/?user_id=${USER_ID}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.memories || [];
}

export async function deleteMemory(memoryId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/v1/memories/${encodeURIComponent(memoryId)}?user_id=${USER_ID}`,
    { method: 'DELETE' },
  );
  if (!res.ok) throw new Error(`Failed to delete memory: ${res.status}`);
}

export async function confirmMemory(memoryId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/v1/memories/${encodeURIComponent(memoryId)}/confirm?user_id=${USER_ID}`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to confirm memory: ${res.status}`);
}

export async function dismissMemory(memoryId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/v1/memories/${encodeURIComponent(memoryId)}/dismiss?user_id=${USER_ID}`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to dismiss memory: ${res.status}`);
}
```

- [ ] **Step 2: Wire MemoryPanel handlers and independent fetch in chat/page.tsx**

Find where MemoryPanel is rendered and where memory state is managed. Add:

```typescript
const [panelMemories, setPanelMemories] = useState<MemoryRecord[]>([]);
const [memoryError, setMemoryError] = useState(false);

const refreshMemories = useCallback(async () => {
  try {
    const fresh = await listMemories();
    setPanelMemories(fresh);
    setMemoryError(false);
  } catch {
    setMemoryError(true);
  }
}, []);

// Fetch when panel opens
const toggleMemoryPanel = useCallback(() => {
  setShowMemoryPanel((prev) => {
    const next = !prev;
    if (next) refreshMemories();
    return next;
  });
}, [refreshMemories]);
```

Then pass handlers to MemoryPanel:
```tsx
<MemoryPanel
  memories={panelMemories}
  onDeleteMemory={async (id) => { await deleteMemory(id); refreshMemories(); }}
  onConfirmPattern={async (id) => { await confirmMemory(id); refreshMemories(); }}
  onDismissPattern={async (id) => { await dismissMemory(id); refreshMemories(); }}
/>
```

Also merge chat response memories:
```typescript
// In the stream complete handler or where chat response memories arrive:
if (response.memories && response.memories.length > 0) {
  setPanelMemories((prev) => {
    const existingIds = new Set(prev.map((m) => m.id));
    const newMems = response.memories!.filter((m) => m.id && !existingIds.has(m.id));
    return [...newMems, ...prev];
  });
}
```

NOTE: Read the actual MemoryPanel props interface to match the expected handler signatures.

- [ ] **Step 3: Verify build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -5`

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/api.ts app/\(app\)/chat/page.tsx
git commit -m "feat: wire memory CRUD API + independent panel fetch + handlers"
```

---

### Task 13: Memory Context Into Decomposition (Fix 4)

**Files:**
- Modify: `Jarvis-Engine/app/services/analytical/control_policy.py`

- [ ] **Step 1: Pass `memory_context` into `_run_plan_day_flow`**

Find where `_run_plan_day_flow` is called from `execute_agentic_flow`. The `memory_context` string should already be built earlier in the flow (for the effective_prompt). Add it as a parameter:

```python
# In execute_agentic_flow, before calling _run_plan_day_flow:
# memory_context is already built above for effective_prompt injection
result = await _run_plan_day_flow(
    user_id, planning_goal, supabase, memory_store,
    ...,
    memory_context=memory_context,  # Add this parameter
)
```

- [ ] **Step 2: Use memory_context in decomposition**

Inside `_run_plan_day_flow`, before `_call_decompose` is called, prepend memory context:

```python
    # Inject memory context into decomposition prompt
    decompose_input = enriched_planning_goal
    if memory_context:
        decompose_input = (
            f"[User Context from Memory]\n{memory_context}\n\n"
            f"[Planning Goal]\n{enriched_planning_goal}"
        )
```

Then update the `_call_decompose` inner function to use `decompose_input` instead of `enriched_planning_goal`:

```python
    async def _call_decompose(force_cloud: bool = False) -> dict:
        if force_cloud:
            result = await hybrid_route_query(
                user_prompt=decompose_input,  # was enriched_planning_goal
                ...
            )
        else:
            result = await gemini_primary_route(
                user_prompt=decompose_input,  # was enriched_planning_goal
                ...
            )
```

- [ ] **Step 3: Populate `memories` in all ChatResponse returns**

Find all `return ChatResponse(...)` in `execute_agentic_flow` and `_run_plan_day_flow`. Add a helper at the top of the function:

```python
    def _get_response_memories():
        if not memory_store:
            return None
        try:
            active = memory_store.get_active_memories(user_id)
            return [
                {"memory_type": m.get("memory_type"), "content": m.get("content"),
                 "confidence": m.get("confidence", 0.5), "source": m.get("source", "inferred"),
                 "id": m.get("id")}
                for m in (active or [])[:20]
            ]
        except Exception:
            return None
```

Then add `memories=_get_response_memories()` to each ChatResponse return.

NOTE: There are ~10+ return statements. Read through the function and add to each one. Focus on the main returns (PLAN_DAY success, PLAN_DAY infeasible, GREETING, GENERAL_QA, CHAT, etc.).

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py
git commit -m "feat: inject memory context into decomposition, populate memories in all ChatResponse returns"
```

---

## Batch 5 — Analytics + Reliability (Post-Demo OK)

### Task 14: Goal Progress Endpoint + Dashboard (Fix 10)

**Files:**
- Modify: `Jarvis-Engine/app/api/v1/endpoints/tasks.py`
- Modify: `jarvis-frontend/lib/api.ts`
- Modify: `jarvis-frontend/app/(app)/dashboard/page.tsx`

- [ ] **Step 1: Add `/goals/progress` endpoint in tasks.py**

Add before the existing list endpoint:

```python
@router.get("/goals/progress", summary="Goal progress aggregation")
async def goal_progress(
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """Aggregate task completion by goal_id."""
    supabase = _get_supabase(request)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")
    result = (
        supabase.table("user_tasks")
        .select("goal_id, status, title")
        .eq("user_id", user_id)
        .execute()
    )
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

- [ ] **Step 2: Add frontend API function**

In `jarvis-frontend/lib/api.ts`:
```typescript
export async function getGoalProgress(): Promise<GoalSummary[]> {
  const res = await fetch(`${API_BASE}/api/v1/tasks/goals/progress?user_id=${USER_ID}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.goals || [];
}
```

- [ ] **Step 3: Add GoalProgress section to dashboard**

In `dashboard/page.tsx`, add a section that fetches and displays goal progress:

```tsx
// In the component:
const [goals, setGoals] = useState<GoalSummary[]>([]);
useEffect(() => {
  getGoalProgress().then(setGoals).catch(() => {});
}, []);

// In the JSX, after StatsStrip:
{goals.length > 0 && (
  <Card className="p-4">
    <h3 className="text-sm font-semibold mb-3">Goal Progress</h3>
    {goals.filter(g => g.goal_id !== "ungrouped").map((g) => (
      <div key={g.goal_id} className="mb-2">
        <div className="flex justify-between text-xs mb-1">
          <span className="truncate max-w-[200px]">{g.goal_id}</span>
          <span>{g.completed}/{g.total} ({g.progress_pct}%)</span>
        </div>
        <div className="w-full h-1.5 bg-neutral-200 dark:bg-neutral-700 rounded-full">
          <div
            className="h-full bg-sage-500 rounded-full transition-all"
            style={{ width: `${g.progress_pct}%` }}
          />
        </div>
      </div>
    ))}
  </Card>
)}
```

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/api/v1/endpoints/tasks.py
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/api.ts app/\(app\)/dashboard/page.tsx
```

Commit in both repos.

---

### Task 15: PEARL Safe Wrapper + Fresh Insights (Fix 11)

**Files:**
- Modify: `Jarvis-Engine/app/services/memory/extractor.py`
- Modify: `Jarvis-Engine/app/api/v1/endpoints/tasks.py`

- [ ] **Step 1: Add `safe_detect_patterns` wrapper in extractor.py**

Add at the module level:

```python
async def safe_detect_patterns(user_id: str, supabase, memory_store, logger=None):
    """Run PEARL pattern detection with error handling and logging."""
    try:
        from app.services.memory.pearl import detect_patterns
        patterns = await asyncio.to_thread(
            detect_patterns, user_id, supabase, memory_store
        )
        if patterns and logger:
            logger.info(f"PEARL detected {len(patterns)} patterns for user {user_id}")
        return patterns
    except Exception as e:
        if logger:
            logger.error(f"PEARL pattern detection failed for user {user_id}: {e}")
        return []
```

- [ ] **Step 2: Replace bare `asyncio.create_task(detect_patterns(...))` calls**

Search for `detect_patterns` calls in `tasks.py` and `extractor.py`. Replace with:
```python
asyncio.create_task(safe_detect_patterns(user_id, supabase, memory_store, logger))
```

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/memory/extractor.py app/api/v1/endpoints/tasks.py
git commit -m "fix: wrap PEARL pattern detection in safe executor with logging"
```

---

### Task 16: Completion Signals Endpoint (Fix 12)

**Files:**
- Modify: `Jarvis-Engine/app/api/v1/endpoints/tasks.py`

- [ ] **Step 1: Add `/completion-signals` endpoint**

```python
@router.get("/completion-signals", summary="Task completion quality signals")
async def list_completion_signals(
    user_id: str = Query(..., description="User ID"),
    limit: int = Query(default=50, le=200),
    request: Request = None,
) -> dict:
    """Return recent task completion signals for analytics and future DKT/RL."""
    supabase = _get_supabase(request)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not available")
    result = (
        supabase.table("task_completion_signals")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    signals = result.data or []
    avg_quality = sum(s.get("quality", 0) for s in signals) / len(signals) if signals else 0
    return {
        "signals": signals,
        "count": len(signals),
        "avg_quality": round(avg_quality, 1),
    }
```

- [ ] **Step 2: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/api/v1/endpoints/tasks.py
git commit -m "feat: add completion signals query endpoint for analytics"
```

---

## Summary

| Task | Fix | Batch | What |
|------|-----|-------|------|
| 1 | 2a | 1 | Frontend transform reads `scheduled_start`/`scheduled_end` |
| 2 | 2b | 1 | Add `SchedulePayload` + `subject_context` + `pearl_insights` to schemas |
| 3 | 2b | 1 | Wire `SchedulePayload` in control_policy returns |
| 4 | 2c | 1 | Frontend `SchedulePayload` + `GoalSummary` types |
| 5 | 1 | 1 | Fix extraction prompt + enrich decomposition input |
| 6 | 5a | 2 | Reject with reason → re-plan |
| 7 | 6 | 2 | Constraint bridge logging + surfacing |
| 8 | 3 | 3 | Multi-day schedule rendering with day headers |
| 9 | 3c | 3 | Scale TMT scores 0-100 |
| 10 | 9 | 3 | Workspace navigation button |
| 11 | 7 | 4 | Memory CRUD backend endpoints |
| 12 | 7+8 | 4 | Frontend memory API + panel wiring |
| 13 | 4 | 4 | Memory context into decomposition + ChatResponse memories |
| 14 | 10 | 5 | Goal progress endpoint + dashboard |
| 15 | 11 | 5 | PEARL safe wrapper |
| 16 | 12 | 5 | Completion signals endpoint |
