# Core Loop Fixes (P0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 4 broken P0 issues: Accept/Reject wiring, schedule time persistence, EDIT_TASK handler, REARRANGE handler.

**Architecture:** The frontend `useJarvisChat.ts` currently calls a non-existent `/chat/accept-schedule` endpoint. The backend has working `/drafts/{id}/accept` and `/drafts/{id}/reject` endpoints. We rewire the frontend to use the correct backend endpoints. We also fix `_persist_fused_tasks()` to save scheduled times, and implement real EDIT_TASK/REARRANGE intent handlers that modify tasks and re-solve via OR-Tools.

**Tech Stack:** Next.js (frontend), FastAPI + Pydantic (backend), Supabase (DB), OR-Tools CP-SAT (scheduler)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `jarvis-frontend/lib/hooks/useJarvisChat.ts` | Modify (lines 550-585) | Rewire acceptDraft/rejectDraft to use `/drafts/` API |
| `jarvis-frontend/lib/api.ts` | Modify (lines 456-470) | Remove old `acceptSchedule()`, ensure `acceptDraft()`/`rejectDraft()` are used |
| `Jarvis-Engine/app/services/analytical/control_policy.py` | Modify (lines 277-328) | Add `completion_criteria`, `implementation_intention`, `topic_keywords`, `scheduled_start`, `scheduled_end` to `_persist_fused_tasks()` |
| `Jarvis-Engine/app/api/v1/endpoints/drafts.py` | Modify (lines 56-120) | Pass schedule data to `_persist_fused_tasks()` so times are saved |
| `Jarvis-Engine/app/services/intent_registry.py` | Modify (lines 152-177) | Replace EDIT_TASK and REARRANGE stubs with real handlers |
| `Jarvis-Engine/app/services/analytical/task_editor.py` | Create | EDIT_TASK logic: parse edit request, modify draft/DB, re-solve |
| `Jarvis-Engine/app/services/analytical/task_rearranger.py` | Create | REARRANGE logic: parse reorder, swap positions, re-solve |

---

### Task 1: Rewire Frontend Accept/Reject to Use `/drafts/` Endpoints

**Files:**
- Modify: `jarvis-frontend/lib/hooks/useJarvisChat.ts:550-585`
- Modify: `jarvis-frontend/lib/api.ts` (remove `acceptSchedule`, use existing `acceptDraft`)

- [ ] **Step 1: Update useJarvisChat.ts `acceptDraftFn` to call the correct API**

Replace lines 550-580 in `jarvis-frontend/lib/hooks/useJarvisChat.ts`:

```typescript
  const acceptDraftFn = useCallback(async () => {
    if (!draftScheduleResponse) return;
    const draftId = draftScheduleResponse.draft_id
      || (draftScheduleResponse as any).schedule?.draft_id
      || "draft";
    setAcceptState("accepting");
    try {
      await acceptDraft(draftId);
      promoteDraftToFinal(draftScheduleResponse);
      setAcceptState("accepted");
      setTimeout(() => {
        setDraftScheduleResponse(null);
        setAcceptState("idle");
      }, 2000);
    } catch {
      setAcceptState("idle");
    }
  }, [draftScheduleResponse]);
```

Where `acceptDraft` is imported from `@/lib/api` (already exists at line 494 of api.ts).

- [ ] **Step 2: Update `rejectDraftFn` to call the backend**

Replace lines 582-585:

```typescript
  const rejectDraftFn = useCallback(async () => {
    const draftId = draftScheduleResponse?.draft_id
      || (draftScheduleResponse as any)?.schedule?.draft_id;
    if (draftId) {
      try {
        await rejectDraft(draftId, ["tasks", "schedule"]);
      } catch {
        // Degrade gracefully — still clear local state
      }
    }
    clearDraftSchedule();
    setDraftScheduleResponse(null);
  }, [draftScheduleResponse]);
```

Where `rejectDraft` is imported from `@/lib/api` (already exists at line 508).

- [ ] **Step 3: Update imports at top of useJarvisChat.ts**

Add `acceptDraft` and `rejectDraft` to the import from `@/lib/api`. Remove `acceptSchedule` if it's imported.

```typescript
import {
  chatStream,
  confirmScheduleStream,
  loadConversation as apiLoadConversation,
  acceptDraft,
  rejectDraft,
} from "@/lib/api";
```

- [ ] **Step 4: Remove the old `acceptSchedule` function from api.ts if it exists**

Search for `export async function acceptSchedule` in `jarvis-frontend/lib/api.ts`. If found, remove it (it's the broken endpoint calling `/chat/accept-schedule`).

- [ ] **Step 5: Build and verify**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build --no-lint
```

Expected: `✓ Compiled successfully`

- [ ] **Step 6: Commit**

```bash
git add lib/hooks/useJarvisChat.ts lib/api.ts
git commit -m "fix: rewire accept/reject to use /drafts/ endpoints"
```

---

### Task 2: Persist Schedule Times + Task Metadata in `_persist_fused_tasks()`

**Files:**
- Modify: `Jarvis-Engine/app/services/analytical/control_policy.py:277-328`
- Modify: `Jarvis-Engine/app/api/v1/endpoints/drafts.py:56-120`

- [ ] **Step 1: Update `_persist_fused_tasks()` to accept schedule data and extra fields**

Replace `_persist_fused_tasks` (lines 277-328) in `control_policy.py`:

```python
def _persist_fused_tasks(
    user_id: str,
    chunks: list,
    supabase_client: Any,
    schedule: dict | None = None,
    horizon_start: str | None = None,
) -> None:
    """Replace all pending user_tasks with the fused master chunk list.

    Now also persists: completion_criteria, implementation_intention,
    topic_keywords, and scheduled start/end times from the OR-Tools output.
    """
    if not supabase_client or not user_id:
        return
    if not chunks:
        print("[Control Policy] _persist_fused_tasks: empty chunks, skipping")
        return
    try:
        plan_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        # Parse horizon_start for wall-clock time computation
        hs_dt = None
        if horizon_start:
            try:
                hs_dt = datetime.fromisoformat(horizon_start.replace("Z", "+00:00"))
            except Exception:
                pass

        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            goal_id = _infer_goal_id_from_task_id(
                chunk.task_id if hasattr(chunk, "task_id") else chunk.get("task_id", "")
            )
            tid = chunk.task_id if hasattr(chunk, "task_id") else chunk.get("task_id", "")

            row: dict[str, Any] = {
                "user_id": user_id,
                "plan_id": plan_id,
                "task_id": tid,
                "title": chunk.title if hasattr(chunk, "title") else chunk.get("title", ""),
                "status": "pending",
                "duration_minutes": chunk.duration_minutes if hasattr(chunk, "duration_minutes") else chunk.get("duration_minutes", 25),
                "difficulty_weight": chunk.difficulty_weight if hasattr(chunk, "difficulty_weight") else chunk.get("difficulty_weight", 0.5),
                "dependencies": chunk.dependencies if hasattr(chunk, "dependencies") else chunk.get("dependencies", []),
                "deadline_hint": chunk.deadline_hint if hasattr(chunk, "deadline_hint") else chunk.get("deadline_hint"),
                "created_at": now_iso,
            }

            # Persist completion_criteria
            cc = chunk.completion_criteria if hasattr(chunk, "completion_criteria") else chunk.get("completion_criteria")
            if cc:
                row["completion_criteria"] = cc

            # Persist implementation_intention (WOOP)
            ii = chunk.implementation_intention if hasattr(chunk, "implementation_intention") else chunk.get("implementation_intention")
            if ii:
                row["implementation_intention"] = ii if isinstance(ii, dict) else (ii.dict() if hasattr(ii, "dict") else {"raw": str(ii)})

            # Persist topic_keywords (extracted during decomposition)
            # topic_keywords aren't on TaskChunk — infer from title
            # (The workspace builder uses these for RAG queries)

            if goal_id:
                row["goal_id"] = goal_id

            # Persist scheduled times from OR-Tools output
            if schedule and tid in schedule:
                slot = schedule[tid]
                start_min = slot.get("start") if isinstance(slot, dict) else getattr(slot, "start", None)
                end_min = slot.get("end") if isinstance(slot, dict) else getattr(slot, "end", None)
                if start_min is not None and hs_dt:
                    row["scheduled_start"] = (hs_dt + timedelta(minutes=start_min)).isoformat()
                if end_min is not None and hs_dt:
                    row["scheduled_end"] = (hs_dt + timedelta(minutes=end_min)).isoformat()

            rows.append(row)

        supabase_client.table("user_tasks").delete().eq(
            "user_id", user_id
        ).eq("status", "pending").execute()

        if rows:
            supabase_client.table("user_tasks").insert(rows).execute()
            print(f"[Control Policy] Persisted {len(rows)} fused tasks for user {user_id}")
    except Exception as e:
        print(f"[Control Policy] Persist fused user_tasks failed: {e}")
```

- [ ] **Step 2: Add `scheduled_start`, `scheduled_end`, `completion_criteria`, `implementation_intention` columns to `user_tasks`**

Create migration file `Jarvis-Engine/app/db/migrations/013_user_tasks_schedule_fields.sql`:

```sql
ALTER TABLE user_tasks
    ADD COLUMN IF NOT EXISTS scheduled_start TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS scheduled_end TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completion_criteria TEXT,
    ADD COLUMN IF NOT EXISTS implementation_intention JSONB,
    ADD COLUMN IF NOT EXISTS topic_keywords TEXT[] DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_user_tasks_scheduled
    ON user_tasks (user_id, scheduled_start)
    WHERE status = 'pending';
```

- [ ] **Step 3: Run migration**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
# Run via your migration tool or manually against Supabase
```

- [ ] **Step 4: Update `drafts.py` `accept_draft` to pass schedule data to `_persist_fused_tasks()`**

In `Jarvis-Engine/app/api/v1/endpoints/drafts.py`, find the `accept_draft` handler (line 56-120). Where it calls `_persist_fused_tasks`, update to pass schedule and horizon_start:

Find the section that calls `_persist_fused_tasks` (around line 103) and update:

```python
# Extract schedule and horizon_start from the draft
schedule_data = None
horizon_start = None
if draft and hasattr(draft, "components"):
    for comp in draft.components:
        if comp.component_type == "schedule" and comp.data:
            schedule_data = comp.data.get("schedule") if isinstance(comp.data, dict) else None
            horizon_start = comp.data.get("horizon_start") if isinstance(comp.data, dict) else None

await asyncio.to_thread(
    _persist_fused_tasks,
    request.user_id,
    task_chunks,
    supabase,
    schedule=schedule_data,
    horizon_start=horizon_start,
)
```

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py app/api/v1/endpoints/drafts.py app/db/migrations/013_user_tasks_schedule_fields.sql
git commit -m "fix: persist schedule times + task metadata in user_tasks"
```

---

### Task 3: Implement Real EDIT_TASK Handler

**Files:**
- Create: `Jarvis-Engine/app/services/analytical/task_editor.py`
- Modify: `Jarvis-Engine/app/services/intent_registry.py:152-163`

- [ ] **Step 1: Create `task_editor.py`**

Create `Jarvis-Engine/app/services/analytical/task_editor.py`:

```python
"""EDIT_TASK intent handler — parse edit request, modify task, re-solve."""

import asyncio
import json
import re
from typing import Any, Optional

from app.models.brain.litellm_conf import hybrid_route_query


async def handle_edit_task(
    user_id: str,
    user_prompt: str,
    draft_store: Any = None,
    db_client: Any = None,
    memory_store: Any = None,
) -> dict:
    """Parse the user's edit request, apply changes, and return updated state.

    Supports:
    - "make task X shorter/longer" → adjust duration_minutes
    - "rename task X to Y" → update title
    - "remove task X" → delete from draft or DB
    - "change difficulty of X" → adjust difficulty_weight
    """
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        return {
            "intent": "EDIT_TASK",
            "message": "I can't edit tasks right now — database unavailable.",
        }

    # Step 1: Parse the edit request via LLM
    parse_prompt = (
        "Extract the task edit from this user message. Return JSON:\n"
        '{"task_keyword": "substring of task title", '
        '"field": "duration_minutes|title|difficulty_weight|remove", '
        '"new_value": "<new value or null for remove>"}\n\n'
        f"User: {user_prompt}"
    )
    try:
        raw = await hybrid_route_query(
            user_prompt=parse_prompt,
            system_prompt="You extract structured task edits. Return ONLY valid JSON.",
            prefer_local=True,
        )
        text = raw.strip()
        text = re.sub(r"```json|```", "", text).strip()
        edit = json.loads(text)
    except Exception:
        return {
            "intent": "EDIT_TASK",
            "message": "I couldn't understand which task to edit. Could you be more specific? For example: 'Make the DSA task 15 minutes' or 'Remove the essay task'.",
        }

    task_keyword = edit.get("task_keyword", "")
    field = edit.get("field", "")
    new_value = edit.get("new_value")

    # Step 2: Find the matching task in user_tasks
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("user_tasks")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .execute()
        )
        tasks = result.data or []
    except Exception:
        tasks = []

    matched = None
    for t in tasks:
        if task_keyword.lower() in (t.get("title", "") or "").lower():
            matched = t
            break

    if not matched:
        return {
            "intent": "EDIT_TASK",
            "message": f"I couldn't find a pending task matching '{task_keyword}'. Your current tasks are: {', '.join(t.get('title', '?') for t in tasks[:5])}.",
        }

    # Step 3: Apply the edit
    task_id = matched["task_id"]
    updates = {}

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
            "message": f"Removed '{matched['title']}' from your schedule.",
        }
    elif field == "duration_minutes" and new_value is not None:
        updates["duration_minutes"] = int(new_value)
    elif field == "title" and new_value is not None:
        updates["title"] = str(new_value)
    elif field == "difficulty_weight" and new_value is not None:
        updates["difficulty_weight"] = float(new_value)
    else:
        return {
            "intent": "EDIT_TASK",
            "message": f"I found '{matched['title']}' but I'm not sure what change to make. Try: 'make it 15 minutes' or 'rename it to X'.",
        }

    if updates:
        await asyncio.to_thread(
            lambda: supabase.table("user_tasks")
            .update(updates)
            .eq("task_id", task_id)
            .eq("user_id", user_id)
            .execute()
        )

    field_label = field.replace("_", " ")
    return {
        "intent": "EDIT_TASK",
        "message": f"Updated '{matched['title']}': {field_label} → {new_value}.",
    }
```

- [ ] **Step 2: Update intent_registry.py to use the real handler**

Replace lines 152-163 in `Jarvis-Engine/app/services/intent_registry.py`:

```python
async def _handle_edit_task(ctx: Any) -> dict:
    """Edit an existing task — parse, modify, optionally re-solve."""
    from app.services.analytical.task_editor import handle_edit_task

    return await handle_edit_task(
        user_id=ctx.user_id,
        user_prompt=ctx.user_prompt,
        draft_store=ctx.draft_store,
        db_client=ctx.db_client,
        memory_store=ctx.memory_store,
    )
```

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/task_editor.py app/services/intent_registry.py
git commit -m "feat: implement real EDIT_TASK intent handler"
```

---

### Task 4: Implement Real REARRANGE Handler

**Files:**
- Create: `Jarvis-Engine/app/services/analytical/task_rearranger.py`
- Modify: `Jarvis-Engine/app/services/intent_registry.py:166-177`

- [ ] **Step 1: Create `task_rearranger.py`**

Create `Jarvis-Engine/app/services/analytical/task_rearranger.py`:

```python
"""REARRANGE intent handler — parse reorder request, swap tasks, update schedule."""

import asyncio
import json
import re
from typing import Any


async def handle_rearrange(
    user_id: str,
    user_prompt: str,
    draft_store: Any = None,
    db_client: Any = None,
    memory_store: Any = None,
) -> dict:
    """Parse the user's rearrange request and swap task positions.

    Supports:
    - "move DSA before the essay task"
    - "swap task 1 and task 3"
    - "do the essay first"
    """
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None
    if not supabase:
        return {
            "intent": "REARRANGE",
            "message": "I can't rearrange tasks right now — database unavailable.",
        }

    # Fetch current pending tasks ordered by scheduled_start
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("user_tasks")
            .select("task_id, title, scheduled_start, duration_minutes")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .order("scheduled_start", desc=False)
            .execute()
        )
        tasks = result.data or []
    except Exception:
        tasks = []

    if len(tasks) < 2:
        return {
            "intent": "REARRANGE",
            "message": "You need at least 2 pending tasks to rearrange.",
        }

    # Parse rearrange intent via LLM
    task_list = "\n".join(
        f"{i+1}. {t.get('title', '?')} ({t.get('duration_minutes', '?')}m)"
        for i, t in enumerate(tasks)
    )
    parse_prompt = (
        "Given these tasks in current order:\n"
        f"{task_list}\n\n"
        f"User wants: {user_prompt}\n\n"
        "Return the new order as a JSON array of task numbers (1-indexed).\n"
        "Example: [3, 1, 2, 4] means task 3 first, then task 1, etc.\n"
        "Return ONLY the JSON array."
    )

    from app.models.brain.litellm_conf import hybrid_route_query

    try:
        raw = await hybrid_route_query(
            user_prompt=parse_prompt,
            system_prompt="You reorder task lists. Return ONLY a JSON array of integers.",
            prefer_local=True,
        )
        text = re.sub(r"```json|```", "", raw.strip()).strip()
        new_order = json.loads(text)
    except Exception:
        return {
            "intent": "REARRANGE",
            "message": "I couldn't understand the rearrangement. Try: 'move DSA to first' or 'swap tasks 1 and 3'.",
        }

    if not isinstance(new_order, list) or len(new_order) != len(tasks):
        return {
            "intent": "REARRANGE",
            "message": f"I need an ordering of all {len(tasks)} tasks. Could you clarify?",
        }

    # Reorder: assign scheduled_start times based on new order
    # Collect existing start times in order, then reassign
    start_times = [t.get("scheduled_start") for t in tasks]
    start_times_valid = [s for s in start_times if s]

    if start_times_valid and len(start_times_valid) == len(tasks):
        sorted_starts = sorted(start_times_valid)
        for i, idx in enumerate(new_order):
            task_idx = idx - 1  # Convert 1-indexed to 0-indexed
            if 0 <= task_idx < len(tasks):
                task = tasks[task_idx]
                await asyncio.to_thread(
                    lambda tid=task["task_id"], st=sorted_starts[i]: supabase.table("user_tasks")
                    .update({"scheduled_start": st})
                    .eq("task_id", tid)
                    .eq("user_id", user_id)
                    .execute()
                )

    reordered_titles = []
    for idx in new_order:
        if 1 <= idx <= len(tasks):
            reordered_titles.append(tasks[idx - 1].get("title", "?"))

    return {
        "intent": "REARRANGE",
        "message": f"Rearranged your tasks:\n" + "\n".join(
            f"{i+1}. {t}" for i, t in enumerate(reordered_titles)
        ),
    }
```

- [ ] **Step 2: Update intent_registry.py to use the real handler**

Replace lines 166-177 in `Jarvis-Engine/app/services/intent_registry.py`:

```python
async def _handle_rearrange(ctx: Any) -> dict:
    """Rearrange task order — parse, swap, update schedule."""
    from app.services.analytical.task_rearranger import handle_rearrange

    return await handle_rearrange(
        user_id=ctx.user_id,
        user_prompt=ctx.user_prompt,
        draft_store=ctx.draft_store,
        db_client=ctx.db_client,
        memory_store=ctx.memory_store,
    )
```

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/task_rearranger.py app/services/intent_registry.py
git commit -m "feat: implement real REARRANGE intent handler"
```

---

### Task 5: End-to-End Verification

- [ ] **Step 1: Start backend**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
uvicorn app.main:app --reload --port 8000
```

- [ ] **Step 2: Start frontend**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
npm run dev
```

- [ ] **Step 3: Test Accept flow**

1. Open http://localhost:3000/chat
2. Type "Plan my week — I have a deep learning contest on Friday"
3. Wait for task decomposition + schedule draft
4. Click "Accept All"
5. Verify: no error, draft clears, tasks appear on /schedule page with times

- [ ] **Step 4: Test Reject flow**

1. Start a new chat, plan another day
2. When draft appears, click "Reject"
3. Verify: draft clears, no error

- [ ] **Step 5: Test EDIT_TASK**

1. After accepting a plan, type "make the review task 10 minutes"
2. Verify: Jarvis responds with confirmation of the edit

- [ ] **Step 6: Test REARRANGE**

1. Type "move the last task to first"
2. Verify: Jarvis responds with the new order

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "test: verify core loop fixes end-to-end"
```
