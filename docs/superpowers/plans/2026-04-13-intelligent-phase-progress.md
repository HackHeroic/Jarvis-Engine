# Intelligent Phase Progress — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the static, generic phase progress UI with an intelligent trace that shows fun spinner verbs, module summaries, tool-call trees (dev mode), and PEARL learning moments — making Jarvis feel like a hyper-personalized intelligence, not a loading screen.

**Architecture:** Backend enriches SSE phase events with `verb` + `detail` payloads and emits new `tool_use`/`memory_extracted`/`pattern_detected` events. Frontend replaces PhaseProgress + PipelineTrace with a unified IntelligentTrace component that renders Option B (default) or Option C (dev mode). Spinner verbs are 80% context-aware, 20% random wildcards.

**Tech Stack:** Python (FastAPI SSE), TypeScript (Next.js 14, React), LangGraph

---

## File Structure

### New Files
| File | Responsibility |
|---|---|
| `Jarvis-Engine/app/utils/spinner_verbs.py` | Server-side verb selection (80/20 context/wildcard) |
| `jarvis-frontend/lib/spinnerVerbs.ts` | Frontend verb pools (fallback if backend verb missing) |
| `jarvis-frontend/components/app/IntelligentTrace.tsx` | Unified phase trace with Option B/C rendering |
| `jarvis-frontend/components/app/LearningToast.tsx` | PEARL pattern toast notification |
| `jarvis-frontend/lib/hooks/useDevMode.ts` | Dev mode localStorage hook |

### Modified Files
| File | What Changes |
|---|---|
| `Jarvis-Engine/app/api/v1/endpoints/chat.py` | Enrich v2/stream phase events with verb + detail |
| `Jarvis-Engine/app/modules/planning_graph.py` | Emit tool_use events from sub-graph nodes |
| `Jarvis-Engine/app/core/observation.py` | Emit memory_extracted/pattern_detected via callback |
| `jarvis-frontend/lib/types.ts` | Add ToolUseEvent, MemoryExtractedEvent, PatternDetectedEvent |
| `jarvis-frontend/lib/api.ts` | Handle 3 new SSE event types in consumeSSE |
| `jarvis-frontend/lib/hooks/useJarvisChat.ts` | Wire new events to state, add devMode |
| `jarvis-frontend/components/app/ModelModeSelector.tsx` | Add dev mode gear toggle |
| `jarvis-frontend/components/app/JarvisResponse.tsx` | Swap PhaseProgress → IntelligentTrace |

---

## What needs to happen (7 tasks)

1. **Task 1:** Backend spinner verbs utility
2. **Task 2:** Enrich v2/stream phase events with verb + detail
3. **Task 3:** Emit tool_use events from planning sub-graph
4. **Task 4:** Emit memory_extracted / pattern_detected from observation loop
5. **Task 5:** Frontend types + SSE handler + useDevMode hook
6. **Task 6:** IntelligentTrace + LearningToast components
7. **Task 7:** Wire into JarvisResponse + ModelModeSelector + ModelMode cleanup

---

### Task 1: Backend Spinner Verbs Utility

**Files:**
- Create: `Jarvis-Engine/app/utils/spinner_verbs.py`

- [ ] **Step 1: Create the spinner verbs module**

```python
"""Server-side spinner verb selection — 80% context-aware, 20% wildcard.

Jarvis personality: Tony Stark wit + Claude Code silliness.
"""

import random

PHASE_VERBS: dict[str, list[str]] = {
    "loading_context":       ["Recollecting", "Summoning", "Dusting off", "Booting up", "Rehydrating"],
    "brain_dump_extraction":  ["Deciphering", "Noodling on", "Untangling", "Dissecting", "Decoding", "Unpacking"],
    "intent_classified":      ["Sussing out", "Reading between the lines", "Deducing", "Profiling"],
    "planning":               ["Orchestrating", "Choreographing", "Tetris-ing", "Blueprinting", "War-rooming"],
    "habits_fetched":         ["Rounding up", "Herding", "Cataloguing", "Mustering"],
    "translating":            ["Weaving", "Translating", "Mapping out", "Threading"],
    "decomposing":            ["Decomposing", "Socratic-chunking", "Slicing into micro-tasks", "Breaking down"],
    "scheduling":             ["Crunching", "Optimizing", "Number-wrangling", "Clockwork-ing", "Tetrimino-ing"],
    "researching":            ["Spelunking", "Excavating", "Rummaging the web", "Sleuthing"],
    "coaching":               ["Checking in on", "Pep-talking", "Reviewing your wins"],
    "ingesting":              ["Munching on", "Digesting", "Absorbing", "Inhaling"],
    "synthesizing":           ["Crafting", "Weaving", "Distilling", "Bottling up", "Composing"],
    "responding":             ["Composing", "Penning", "Wordsmithing", "Articulating"],
    "learning":               ["Absorbing", "Filing away", "Cerebrating", "Etching into memory", "Jotting down"],
}

WILDCARD_VERBS: list[str] = [
    "Jarvising", "Flambéing", "Moonwalking through", "Discombobulating",
    "Prestidigitating", "Combobulating", "Quantum-tunneling",
    "Vibing with", "Percolating", "Gallivanting through",
    "Shenaniganing", "Sock-hopping through", "Razzle-dazzling",
    "Arc-reactoring", "Hullaballooing over", "Stark-industrializing",
    "Beboppin' through", "Flibbertigibbeting", "Canoodling with",
    "Lollygagging over", "Tomfoolering with",
    "Hyperspacing through", "Wibbling at", "Whatchamacalliting",
]


def get_spinner_verb(phase: str) -> str:
    """Pick a spinner verb: 80% context-aware, 20% wildcard."""
    if random.random() < 0.2:
        return random.choice(WILDCARD_VERBS)
    pool = PHASE_VERBS.get(phase, PHASE_VERBS.get("responding", ["Processing"]))
    return random.choice(pool)
```

- [ ] **Step 2: Verify it works**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.utils.spinner_verbs import get_spinner_verb; print(get_spinner_verb('planning')); print(get_spinner_verb('learning'))"`
Expected: Two verb strings (e.g. "Orchestrating", "Filing away")

- [ ] **Step 3: Commit**

```bash
git add app/utils/spinner_verbs.py
git commit -m "feat: add server-side spinner verb selection (80/20 context/wildcard)"
```

---

### Task 2: Enrich v2/stream Phase Events

**Files:**
- Modify: `Jarvis-Engine/app/api/v1/endpoints/chat.py` (v2/stream event_gen, around line 1093)
- Modify: `Jarvis-Engine/app/orchestrator/state.py` — add `progress_queue` field

**Prerequisite:** Add `progress_queue: Any` to `JarvisState` in `state.py` (after the existing `progress_callback: Any` field). Then add `"progress_queue": progress_queue,` to the `initial_state` dict in `chat_stream_v2` (alongside `"progress_callback": progress_cb`). This makes the queue available to all graph nodes including the observation loop.

- [ ] **Step 1: Import spinner verbs and add verb + detail to phase events**

In the `event_gen()` function inside `chat_stream_v2`, find the phase emission block:

```python
# CURRENT (around line 1112):
phase = NODE_TO_PHASE.get(node_name)
if phase:
    yield f"event: phase\ndata: {json_mod.dumps({'phase': phase})}\n\n"
```

Replace with:

```python
from app.utils.spinner_verbs import get_spinner_verb

# ...inside event_gen, after node_state is available:
phase = NODE_TO_PHASE.get(node_name)
if phase:
    phase_detail = {
        "phase": phase,
        "verb": get_spinner_verb(phase),
    }
    # Enrich with node-specific detail
    if node_name == "load_context":
        conv_hist = initial_state.get("conversation_history") or []
        # _existing_memories is captured from enclosing scope via closure
        # (defined at line 1054, before event_gen at line 1095 — safe)
        phase_detail["detail"] = {
            "memories_count": len(_existing_memories),
            "conversation_turns": len(conv_hist),
        }
    elif node_name == "classify_intent" and node_state.get("intent"):
        phase_detail["detail"] = {
            "intent": str(node_state["intent"]),
            "method": "rule-based",
        }
    elif node_name in ("planning_module", "research_agent", "coach_module", "knowledge_module", "conversation_module"):
        phase_detail["detail"] = {
            "module": node_name,
        }
    elif node_name == "observation_loop":
        phase_detail["detail"] = {
            "memories_extracted": node_state.get("memories_extracted_count", 0),
            "patterns_detected": node_state.get("patterns_detected_count", 0),
        }
    yield f"event: phase\ndata: {json_mod.dumps(phase_detail)}\n\n"
```

- [ ] **Step 2: Also emit tool_use events from progress_queue**

The progress_queue already receives events from sub-graph callbacks. Update the queue drain to also forward `tool_use` events. In the queue drain block:

```python
# CURRENT:
while not progress_queue.empty():
    try:
        yield f"event: phase\ndata: {progress_queue.get_nowait()}\n\n"
    except asyncio.QueueEmpty:
        break
```

Replace with:

```python
while not progress_queue.empty():
    try:
        raw = progress_queue.get_nowait()
        # Parse to check event type — tool_use events get their own SSE type
        try:
            parsed = json_mod.loads(raw)
            evt_type = parsed.pop("_event_type", "phase")
        except (json_mod.JSONDecodeError, TypeError):
            evt_type = "phase"
            parsed = None
        if evt_type == "tool_use":
            yield f"event: tool_use\ndata: {json_mod.dumps(parsed)}\n\n"
        elif evt_type == "memory_extracted":
            yield f"event: memory_extracted\ndata: {json_mod.dumps(parsed)}\n\n"
        elif evt_type == "pattern_detected":
            yield f"event: pattern_detected\ndata: {json_mod.dumps(parsed)}\n\n"
        else:
            yield f"event: phase\ndata: {raw}\n\n"
    except asyncio.QueueEmpty:
        break
```

Apply this to BOTH drain blocks (the one inside the astream loop AND the one after).

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile app/api/v1/endpoints/chat.py && echo "OK"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add app/api/v1/endpoints/chat.py
git commit -m "feat: enrich v2/stream phase events with verb + detail, route tool_use/memory/pattern SSE events"
```

---

### Task 3: Emit tool_use Events from Planning Sub-Graph

**Files:**
- Modify: `Jarvis-Engine/app/modules/planning_graph.py`

- [ ] **Step 1: Create a helper to emit tool_use events via progress_callback**

The `_emit_tool_use` helper needs direct queue access instead of going through progress_callback (which wraps in `{"phase": ...}`). Pass the queue as a state field.

First, add `progress_queue` to `PlanningState` in `planning_graph.py:8-24`. Add it after the existing `progress_callback` field (line 23):

```python
# In PlanningState TypedDict, after progress_callback: Any, add:
    progress_queue: Any  # asyncio.Queue — direct access for tool_use events
```

The full field list becomes: `user_id, user_model, planning_goal, habits_text, semantic_slots, time_slots, constraints, task_chunks, pending_tasks, schedule, horizon_minutes, retry_count, clarification_request, error, progress_callback, progress_queue`.

Then in `chat.py` v2/stream, pass the queue into the initial_state:

```python
# Add to initial_state dict (alongside progress_callback):
"progress_queue": progress_queue,
```

And in `_planning_module_node` in `graph.py`, pass it through to the planning sub-graph state:

```python
planning_state = {
    ...
    "progress_callback": state.get("progress_callback"),
    "progress_queue": state.get("progress_queue"),
}
```

Now add the helper at the top of planning_graph.py (after imports):

```python
import json as _json

def _emit_tool_use(state: dict, tool: str, status: str, detail: dict | None = None) -> None:
    """Emit a tool_use event directly onto the SSE queue (bypasses progress_callback)."""
    queue = state.get("progress_queue")
    if not queue:
        return
    event = {
        "_event_type": "tool_use",
        "module": "planning_module",
        "tool": tool,
        "status": status,
    }
    if detail:
        event["detail"] = detail
    queue.put_nowait(_json.dumps(event))
```

The existing `progress_cb` in chat.py stays unchanged — it only handles phase events:

```python
def progress_cb(phase, **detail):
    progress_queue.put_nowait(json_mod.dumps({"phase": phase, **detail}))
```

- [ ] **Step 2: Add tool_use emissions to each planning node**

Update `fetch_constraints`:
```python
async def fetch_constraints(state: PlanningState) -> dict:
    _emit_tool_use(state, "fetch_constraints", "started")
    cb = state.get("progress_callback")
    if cb:
        cb("habits_fetched")
    user_model = state["user_model"]
    if user_model:
        constraints = await user_model.get_behavioral_constraints()
        habits_text = "\n".join(c.get("raw_text", "") for c in constraints if c.get("constraint_type") == "habit")
        _emit_tool_use(state, "fetch_constraints", "done", {"rows": len(constraints)})
        return {"constraints": constraints, "habits_text": habits_text}
    _emit_tool_use(state, "fetch_constraints", "done", {"rows": 0})
    return {"constraints": [], "habits_text": ""}
```

Update `translate_habits`:
```python
async def translate_habits(state: PlanningState) -> dict:
    _emit_tool_use(state, "translate_habits", "started")
    cb = state.get("progress_callback")
    if cb:
        cb("translating")
    habits_text = state.get("habits_text", "")
    if not habits_text.strip():
        _emit_tool_use(state, "translate_habits", "done", {"slots": 0})
        return {"semantic_slots": []}
    try:
        from app.services.analytical.habit_translator import translate_habits_to_slots
        slots = await translate_habits_to_slots(habits_text)
        result = [s.model_dump() for s in slots] if slots else []
        _emit_tool_use(state, "translate_habits", "done", {"slots": len(result)})
        return {"semantic_slots": result}
    except Exception as e:
        logger.warning(f"Habit translation failed: {e}")
        _emit_tool_use(state, "translate_habits", "error", {"error": str(e)})
        return {"semantic_slots": []}
```

Update `decompose_goal`:
```python
async def decompose_goal(state: PlanningState) -> dict:
    _emit_tool_use(state, "decompose_goal", "started")
    cb = state.get("progress_callback")
    if cb:
        cb("decomposing")
    goal = state.get("planning_goal", "")
    system_prompt = (
        "You are a task decomposition expert. Break the user's goal into 5-8 concrete, "
        "actionable micro-tasks of 15-25 minutes each. Each task must have clear completion criteria."
    )
    try:
        from app.core.model_router import route_llm_call
        from app.api.v1.endpoints.reasoning import ExecutionGraph
        result = await route_llm_call(
            task="socratic_chunker",
            prompt=goal,
            system_prompt=system_prompt,
            response_schema=ExecutionGraph,
        )
        if isinstance(result, ExecutionGraph):
            graph = result
        else:
            import json, re
            clean = re.sub(r"```json|```", "", str(result)).strip()
            data = json.loads(clean)
            graph = ExecutionGraph.model_validate(data)
        chunks = [tc.model_dump() for tc in graph.decomposition]
        _emit_tool_use(state, "decompose_goal", "done", {"task_count": len(chunks)})
        return {"task_chunks": chunks}
    except Exception as e:
        logger.error(f"Decomposition failed: {e}")
        _emit_tool_use(state, "decompose_goal", "error", {"error": str(e)})
        return {"error": f"Decomposition failed: {e}", "task_chunks": []}
```

Update `solve_schedule` — add tool_use for OR-Tools:
```python
async def solve_schedule(state: PlanningState) -> dict:
    from app.core.or_tools.solver import JarvisScheduler
    _emit_tool_use(state, "or_tools_solve", "started")
    chunks = state.get("task_chunks", [])
    horizon = state.get("horizon_minutes", 2880)
    if not chunks:
        _emit_tool_use(state, "or_tools_solve", "error", {"error": "No tasks"})
        return {"error": "No tasks to schedule", "schedule": None}
    scheduler = JarvisScheduler(horizon_minutes=horizon)
    for slot in state.get("time_slots", []):
        if slot.get("availability") == "blocked":
            scheduler.add_hard_block(slot["start_min"], slot["end_min"], slot.get("name", "block"))
        elif slot.get("availability") == "minimal_work":
            scheduler.add_soft_block(slot["start_min"], slot["end_min"], slot.get("name", "soft"),
                                     max_task_duration=slot.get("max_task_duration", 15),
                                     max_difficulty=slot.get("max_difficulty", 0.4))
    for i, chunk in enumerate(chunks):
        scheduler.add_task(
            task_id=chunk.get("task_id", f"t{i}"),
            duration=chunk.get("duration_minutes", 25),
            priority_score=len(chunks) - i,
            dependencies=chunk.get("dependencies", []),
            difficulty_weight=chunk.get("difficulty_weight", 0.5),
        )
    scheduler.build_dependencies()
    result, status = scheduler.solve()
    if status == "INFEASIBLE":
        _emit_tool_use(state, "or_tools_solve", "done", {"status": "INFEASIBLE"})
        return {"schedule": None, "error": "INFEASIBLE"}
    _emit_tool_use(state, "or_tools_solve", "done", {
        "status": "OPTIMAL",
        "task_count": len(chunks),
        "horizon_h": horizon // 60,
    })
    return {"schedule": result, "error": None}
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile app/modules/planning_graph.py && echo "OK"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add app/modules/planning_graph.py app/api/v1/endpoints/chat.py
git commit -m "feat: emit tool_use events from planning sub-graph nodes"
```

---

### Task 4: Emit memory_extracted / pattern_detected from Observation Loop

**Files:**
- Modify: `Jarvis-Engine/app/core/observation.py`

**Timing note:** The observation loop is the last graph node before `END`. Its events go onto the progress_queue and are caught by the "drain remaining" block after the `astream` loop completes (chat.py lines 1131-1136). This is correct — the drain block runs before the complete event is emitted, so memory/pattern events will appear in the frontend trace.

- [ ] **Step 1: Update observation loop to emit events via progress_callback**

Replace the entire file:

```python
"""Observation Loop — post-turn behavioral intelligence.

Runs after every interaction (~200-500ms, blocking).
1. Extract memories (Gemma fast)
2. Detect PEARL patterns (stats)
3. Update cognitive state (math)
4. Bridge patterns → constraints
"""

import asyncio
import json

from app.core.jarvis_logger import JARVIS_LOGGER as logger


def _emit_event(state: dict, event_type: str, data: dict) -> None:
    """Emit a typed SSE event directly onto the progress_queue."""
    queue = state.get("progress_queue")
    if not queue:
        return
    queue.put_nowait(json.dumps({"_event_type": event_type, **data}))


async def extract_and_store_memories(state: dict) -> list[dict]:
    """Extract memories from this conversation turn. Returns extracted memories."""
    user_model = state.get("user_model")
    if not user_model:
        return []
    memory_store = await user_model.get_memory_store()
    if not memory_store:
        return []
    # The actual extraction happens in safe_extract_memories (fire-and-forget from chat.py).
    # Here we emit what was found for the frontend trace.
    user_message = state.get("user_message", "")
    response_message = state.get("response_message", "")
    if not user_message and not response_message:
        return []
    # Placeholder: real extraction will use LLM. For now, emit a stub observation.
    return []


async def detect_pearl_patterns(state: dict) -> list[dict]:
    """Detect recurring behavioral patterns. Returns detected patterns."""
    user_model = state.get("user_model")
    if not user_model:
        return []
    patterns = await user_model.get_pearl_patterns()
    return patterns


async def update_cognitive_state(state: dict) -> None:
    """Update estimated cognitive energy for this user."""
    user_model = state.get("user_model")
    if not user_model:
        return
    energy = await user_model.get_estimated_energy()
    # Future: update UserModel with fresh energy estimate


async def bridge_patterns_to_constraints(state: dict, patterns: list[dict]) -> int:
    """Convert high-confidence patterns to scheduler constraints. Returns count bridged."""
    bridged = 0
    for pattern in patterns:
        confidence = pattern.get("confidence", 0.0)
        if confidence < 0.7:
            continue
        pattern_type = pattern.get("type", "")
        logger.debug(f"PEARL pattern {pattern_type} (conf={confidence}) ready for bridging")
        bridged += 1
    return bridged


async def run_observation_loop(state: dict) -> dict:
    """Post-turn behavioral intelligence. Blocking but fast (~200-500ms)."""
    # Max iteration guard — prevent infinite loops
    modules_invoked = state.get("modules_invoked", [])
    if len(modules_invoked) >= 10:
        return {"needs_followup": False}

    try:
        coro = _run_observation_inner(state)
        return await asyncio.wait_for(coro, timeout=0.5)
    except asyncio.TimeoutError:
        logger.warning("Observation loop exceeded 500ms budget — skipping")
        return {"needs_followup": False}


async def _run_observation_inner(state: dict) -> dict:
    """Inner observation loop with timeout guard."""
    memories = await extract_and_store_memories(state)
    for mem in memories:
        _emit_event(state, "memory_extracted", {
            "type": mem.get("memory_type", "observation"),
            "content": mem.get("content", ""),
            "confidence": mem.get("confidence", 0.5),
        })

    patterns = await detect_pearl_patterns(state)
    for pattern in patterns:
        _emit_event(state, "pattern_detected", {
            "type": pattern.get("type", "behavioral_pattern"),
            "content": pattern.get("content", ""),
            "confidence": pattern.get("confidence", 0.0),
            "occurrence_count": pattern.get("occurrence_count", 1),
            "action": pattern.get("action", "none"),
        })

    await update_cognitive_state(state)
    bridged = await bridge_patterns_to_constraints(state, patterns)

    return {
        "needs_followup": False,
        "memories_extracted_count": len(memories),
        "patterns_detected_count": len(patterns),
    }
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile app/core/observation.py && echo "OK"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add app/core/observation.py
git commit -m "feat: observation loop emits memory_extracted/pattern_detected events, adds 500ms timeout guard"
```

---

### Task 5: Frontend Types + SSE Handler + useDevMode Hook

**Files:**
- Modify: `jarvis-frontend/lib/types.ts`
- Modify: `jarvis-frontend/lib/api.ts`
- Create: `jarvis-frontend/lib/hooks/useDevMode.ts`

- [ ] **Step 1: Add new event types to types.ts**

At the end of the file, before the last closing comment, add:

```typescript
// ---------------------------------------------------------------------------
// Tool Use / Learning Events (v2 intelligent trace)
// ---------------------------------------------------------------------------

export interface ToolUseEvent {
  module: string;
  tool: string;
  status: "started" | "done" | "error";
  detail?: Record<string, unknown>;
  timestamp?: number;
}

export interface MemoryExtractedEvent {
  type: string;
  content: string;
  confidence: number;
  timestamp?: number;
}

export interface PatternDetectedEvent {
  type: string;
  content: string;
  confidence: number;
  occurrence_count: number;
  action: string;
  timestamp?: number;
}
```

Also add to `JarvisStreamState`:

```typescript
// Add these fields to the JarvisStreamState interface:
toolUses: ToolUseEvent[];
memoriesExtracted: MemoryExtractedEvent[];
patternsDetected: PatternDetectedEvent[];
devMode: boolean;
```

And to `INITIAL_STREAM_STATE`:

```typescript
// Add these to INITIAL_STREAM_STATE:
toolUses: [],
memoriesExtracted: [],
patternsDetected: [],
devMode: false,
```

And add `verb` and `detail` to `PhaseEventData`:

```typescript
export interface PhaseEventData {
  phase: string;
  verb?: string;        // spinner verb from backend
  detail?: Record<string, unknown>;  // enriched phase detail
  message?: string;
  data?: Record<string, unknown>;
  timestamp?: number;
  [key: string]: unknown;
}
```

- [ ] **Step 2: Add new SSE event handlers to api.ts consumeSSE**

In `consumeSSE()`, add handlers for the 3 new event types inside the event dispatch:

```typescript
// Add after the 'error' handler (around line 95):
} else if (currentEvent === 'tool_use') {
  handlers.onToolUse?.(data as ToolUseEvent);
} else if (currentEvent === 'memory_extracted') {
  handlers.onMemoryExtracted?.(data as MemoryExtractedEvent);
} else if (currentEvent === 'pattern_detected') {
  handlers.onPatternDetected?.(data as PatternDetectedEvent);
}
```

Also add to the `StreamHandlers` interface:

```typescript
export interface StreamHandlers {
  onStep?: (intent: string, data?: Record<string, unknown>) => void;
  onPhase?: (phase: string, data: Record<string, unknown>) => void;
  onThinkingToken?: (token: string) => void;
  onMessageToken?: (token: string) => void;
  onComplete?: (response: ChatResponse) => void;
  onError?: (err: Error) => void;
  onToolUse?: (event: ToolUseEvent) => void;
  onMemoryExtracted?: (event: MemoryExtractedEvent) => void;
  onPatternDetected?: (event: PatternDetectedEvent) => void;
}
```

Import the types at the top of api.ts:

```typescript
import type { ToolUseEvent, MemoryExtractedEvent, PatternDetectedEvent } from './types';
```

- [ ] **Step 3: Create useDevMode hook**

```typescript
// jarvis-frontend/lib/hooks/useDevMode.ts
"use client";

import { useState, useEffect, useCallback } from "react";

const STORAGE_KEY = "jarvis-dev-mode";

export function useDevMode(): [boolean, () => void] {
  const [devMode, setDevMode] = useState(false);

  useEffect(() => {
    setDevMode(localStorage.getItem(STORAGE_KEY) === "true");
  }, []);

  const toggle = useCallback(() => {
    setDevMode((prev) => {
      const next = !prev;
      localStorage.setItem(STORAGE_KEY, String(next));
      return next;
    });
  }, []);

  return [devMode, toggle];
}
```

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/types.ts lib/api.ts lib/hooks/useDevMode.ts
git commit -m "feat: add ToolUse/Memory/Pattern event types, SSE handlers, useDevMode hook"
```

---

### Task 6: IntelligentTrace + LearningToast Components

**Files:**
- Create: `jarvis-frontend/lib/spinnerVerbs.ts`
- Create: `jarvis-frontend/components/app/IntelligentTrace.tsx`
- Create: `jarvis-frontend/components/app/LearningToast.tsx`

- [ ] **Step 1: Create spinnerVerbs.ts (frontend fallback)**

```typescript
// jarvis-frontend/lib/spinnerVerbs.ts
/**
 * Frontend spinner verb fallback — used only when backend doesn't provide a verb.
 * Primary source is the backend (app/utils/spinner_verbs.py).
 */

const PHASE_VERBS: Record<string, string[]> = {
  loading_context:       ["Recollecting", "Summoning", "Dusting off", "Booting up", "Rehydrating"],
  brain_dump_extraction: ["Deciphering", "Noodling on", "Untangling", "Dissecting", "Decoding"],
  intent_classified:     ["Sussing out", "Reading between the lines", "Deducing", "Profiling"],
  planning:              ["Orchestrating", "Choreographing", "Tetris-ing", "Blueprinting"],
  habits_fetched:        ["Rounding up", "Herding", "Cataloguing", "Mustering"],
  translating:           ["Weaving", "Translating", "Mapping out", "Threading"],
  decomposing:           ["Decomposing", "Socratic-chunking", "Slicing into micro-tasks"],
  scheduling:            ["Crunching", "Optimizing", "Number-wrangling", "Tetrimino-ing"],
  researching:           ["Spelunking", "Excavating", "Rummaging the web", "Sleuthing"],
  coaching:              ["Checking in on", "Pep-talking", "Reviewing your wins"],
  ingesting:             ["Munching on", "Digesting", "Absorbing", "Inhaling"],
  synthesizing:          ["Crafting", "Distilling", "Bottling up", "Composing"],
  responding:            ["Composing", "Penning", "Wordsmithing", "Articulating"],
  learning:              ["Absorbing", "Filing away", "Cerebrating", "Jotting down"],
};

const WILDCARD_VERBS = [
  "Jarvising", "Flambéing", "Moonwalking through", "Discombobulating",
  "Combobulating", "Quantum-tunneling", "Vibing with", "Percolating",
  "Gallivanting through", "Razzle-dazzling", "Arc-reactoring",
  "Stark-industrializing", "Tomfoolering with", "Hyperspacing through",
];

export function getSpinnerVerb(phase: string): string {
  if (Math.random() < 0.2) {
    return WILDCARD_VERBS[Math.floor(Math.random() * WILDCARD_VERBS.length)]!;
  }
  const pool = PHASE_VERBS[phase] ?? PHASE_VERBS["responding"]!;
  return pool[Math.floor(Math.random() * pool.length)]!;
}
```

- [ ] **Step 2: Create IntelligentTrace.tsx**

```tsx
// jarvis-frontend/components/app/IntelligentTrace.tsx
"use client";

import { Check, Loader2, Settings } from "lucide-react";
import { getPhaseDisplayName } from "@/lib/constants";
import { getSpinnerVerb } from "@/lib/spinnerVerbs";
import type { PhaseEventData, ToolUseEvent, MemoryExtractedEvent } from "@/lib/types";

interface IntelligentTraceProps {
  phases: PhaseEventData[];
  currentPhase?: string;
  isStreaming: boolean;
  devMode?: boolean;
  toolUses?: ToolUseEvent[];
  memoriesExtracted?: MemoryExtractedEvent[];
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function getVerb(pe: PhaseEventData): string {
  return pe.verb || pe.data?.verb as string || getSpinnerVerb(pe.phase);
}

function renderDetail(pe: PhaseEventData): string | null {
  const d = pe.detail || pe.data;
  if (!d) return null;
  const parts: string[] = [];
  if (d.intent) parts.push(`Intent: ${d.intent}`);
  if (d.memories_count != null) parts.push(`${d.memories_count} memories`);
  if (d.conversation_turns != null) parts.push(`${d.conversation_turns} turns`);
  if (d.module) parts.push(`${d.module}`);
  if (d.rows != null) parts.push(`${d.rows} constraints`);
  if (d.slots != null) parts.push(`${d.slots} time slots`);
  if (d.task_count != null) parts.push(`${d.task_count} tasks`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

function ToolUseTree({ toolUses, module }: { toolUses: ToolUseEvent[]; module?: string }) {
  const filtered = module
    ? toolUses.filter((t) => t.module === module)
    : toolUses;
  if (filtered.length === 0) return null;

  return (
    <div className="pl-5 space-y-0.5">
      {filtered.map((tu, i) => {
        const isLast = i === filtered.length - 1;
        const prefix = isLast ? "└─" : "├─";
        const detailParts: string[] = [];
        if (tu.detail) {
          if (tu.detail.model) detailParts.push(String(tu.detail.model));
          if (tu.detail.rows != null) detailParts.push(`${tu.detail.rows} rows`);
          if (tu.detail.task_count != null) detailParts.push(`${tu.detail.task_count} tasks`);
          if (tu.detail.status) detailParts.push(String(tu.detail.status));
          if (tu.detail.duration_ms != null) detailParts.push(formatDuration(Number(tu.detail.duration_ms)));
          if (tu.detail.error) detailParts.push(`error: ${tu.detail.error}`);
        }
        return (
          <div key={`${tu.tool}-${i}`} className="flex items-center gap-1 text-[9px] text-muted/50">
            <span className="font-mono">{prefix}</span>
            <span>{tu.tool}</span>
            {tu.status === "done" && <span className="text-sage">✓</span>}
            {tu.status === "error" && <span className="text-terra">✗</span>}
            {tu.status === "started" && <Loader2 size={8} className="animate-spin text-dusk" />}
            {detailParts.length > 0 && (
              <span className="text-muted/40">{detailParts.join(" · ")}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function IntelligentTrace({
  phases,
  currentPhase,
  isStreaming,
  devMode = false,
  toolUses = [],
  memoriesExtracted = [],
}: IntelligentTraceProps) {
  if (phases.length === 0 && !isStreaming) return null;

  const completedPhases = phases.filter(
    (_, i) => i < phases.length - 1 || currentPhase === "complete"
  );

  const activePhase =
    phases.length > 0 && currentPhase !== "complete"
      ? phases[phases.length - 1]
      : null;

  return (
    <div className="mb-2 space-y-0.5">
      {completedPhases.map((pe, i) => {
        const nextTs = i < phases.length - 1 ? phases[i + 1]!.timestamp : Date.now();
        const durationMs =
          (pe.data?.duration_ms as number) ??
          (pe.timestamp && nextTs ? nextTs - pe.timestamp : null);
        const verb = getVerb(pe);
        const detail = renderDetail(pe);
        const moduleForTools = (pe.detail?.module || pe.data?.module) as string | undefined;

        return (
          <div key={`${pe.phase}-${i}`}>
            <div className="flex items-center gap-1.5 text-[10px]">
              <Check size={10} className="text-sage flex-shrink-0" />
              <span className="text-muted">
                <span className="text-secondary/80">{verb}</span>...
              </span>
              {durationMs != null && (
                <span className="text-muted/60">{formatDuration(durationMs)}</span>
              )}
            </div>
            {detail && (
              <div className="pl-5 text-[9px] text-muted/60">→ {detail}</div>
            )}
            {/* Memory observations inline */}
            {pe.phase === "learning" && memoriesExtracted.length > 0 && (
              <div className="pl-5 space-y-0.5">
                {memoriesExtracted.map((mem, mi) => (
                  <div key={mi} className="text-[9px] text-muted/60">
                    → 🧠 Noted: &quot;{mem.content}&quot;
                  </div>
                ))}
              </div>
            )}
            {/* Dev mode: tool call tree */}
            {devMode && <ToolUseTree toolUses={toolUses} module={moduleForTools} />}
          </div>
        );
      })}

      {/* Active phase with spinner */}
      {activePhase && isStreaming && currentPhase !== "complete" && (
        <div className="flex items-center gap-1.5 text-[10px]">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-terra opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-terra" />
          </span>
          <span className="text-terra">
            <span className="font-medium">{getVerb(activePhase)}</span>...
          </span>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Create LearningToast.tsx**

```tsx
// jarvis-frontend/components/app/LearningToast.tsx
"use client";

import { useEffect, useState } from "react";
import { Brain, X } from "lucide-react";
import clsx from "clsx";
import type { PatternDetectedEvent } from "@/lib/types";

interface LearningToastProps {
  pattern: PatternDetectedEvent | null;
  onDismiss: () => void;
  onUndo?: () => void;
}

export function LearningToast({ pattern, onDismiss, onUndo }: LearningToastProps) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!pattern) {
      setVisible(false);
      return;
    }
    // Only show toast for high-confidence recurring patterns
    if (pattern.confidence < 0.7 || pattern.occurrence_count < 3) {
      return;
    }
    setVisible(true);
    const timer = setTimeout(() => {
      setVisible(false);
      onDismiss();
    }, 10_000);
    return () => clearTimeout(timer);
  }, [pattern, onDismiss]);

  if (!visible || !pattern) return null;

  return (
    <div
      className={clsx(
        "fixed bottom-6 right-6 max-w-sm p-4 rounded-xl shadow-lg border",
        "bg-surface-card border-gold/30",
        "translate-y-0 opacity-100 transition-all duration-300"
      )}
    >
      <div className="flex items-start gap-3">
        <Brain size={20} className="text-gold flex-shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <p className="text-xs font-medium text-secondary mb-1">
            Sir, I&apos;ve noticed something
          </p>
          <p className="text-xs text-muted leading-relaxed">
            {pattern.content}
          </p>
          {pattern.action && pattern.action !== "none" && (
            <p className="text-[10px] text-muted/60 mt-1">
              Action taken: {pattern.action.replace(/_/g, " ")}
            </p>
          )}
          <div className="flex gap-2 mt-3">
            <button
              onClick={() => { setVisible(false); onDismiss(); }}
              className="px-3 py-1 text-[10px] font-medium rounded-md bg-surface-muted text-secondary hover:bg-surface-subtle transition-colors"
            >
              Got it
            </button>
            {onUndo && (
              <button
                onClick={() => { setVisible(false); onUndo(); }}
                className="px-3 py-1 text-[10px] font-medium rounded-md text-terra hover:bg-terra/10 transition-colors"
              >
                Undo
              </button>
            )}
          </div>
        </div>
        <button onClick={() => { setVisible(false); onDismiss(); }} className="text-muted/40 hover:text-muted">
          <X size={14} />
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/spinnerVerbs.ts components/app/IntelligentTrace.tsx components/app/LearningToast.tsx
git commit -m "feat: add IntelligentTrace, LearningToast, and spinner verbs"
```

---

### Task 7: Wire into JarvisResponse + ModelModeSelector + useJarvisChat

**Files:**
- Modify: `jarvis-frontend/components/app/JarvisResponse.tsx`
- Modify: `jarvis-frontend/components/app/ModelModeSelector.tsx`
- Modify: `jarvis-frontend/lib/hooks/useJarvisChat.ts`

- [ ] **Step 1: Update JarvisResponse to use IntelligentTrace**

In `JarvisResponse.tsx`, replace the PhaseProgress import and usage:

```typescript
// REPLACE:
import { PhaseProgress } from './PhaseProgress';

// WITH:
import { IntelligentTrace } from './IntelligentTrace';
```

Add `devMode` prop to `JarvisResponseProps`:

```typescript
interface JarvisResponseProps {
  message: JarvisMessage;
  devMode?: boolean;            // ← ADD
  // ... existing props ...
}
```

Then find where `<PhaseProgress` is rendered and replace with:

```tsx
<IntelligentTrace
  phases={message.phaseHistory || []}
  currentPhase={message.isStreaming ? "streaming" : "complete"}
  isStreaming={message.isStreaming || false}
  devMode={devMode}
  toolUses={message.toolUses || []}
  memoriesExtracted={message.memoriesExtracted || []}
/>
```

In the chat page (`app/(app)/chat/page.tsx`), pass `devMode` through when rendering `<JarvisResponse>`:

```tsx
// Find the JarvisResponse render (inside the messages.map):
<JarvisResponse
  message={msg}
  devMode={devMode}       // ← ADD — devMode comes from useJarvisChat()
  onClarificationSelect={...}
  // ... other existing props ...
/>
```

- [ ] **Step 2: Add dev mode gear toggle to ModelModeSelector**

In `ModelModeSelector.tsx`, add the gear icon after the mode buttons:

```tsx
// Add import:
import { Settings } from "lucide-react";

// Add props:
interface ModelModeSelectorProps {
  value: ModelMode;
  onChange: (mode: ModelMode) => void;
  disabled?: boolean;
  devMode?: boolean;
  onToggleDevMode?: () => void;
}
```

Add the gear button after the mode buttons `</div>`, inside the return:

```tsx
return (
  <div className="flex items-center gap-1.5">
    <div className="flex items-center bg-surface-muted rounded-lg p-0.5">
      {modes.map((mode) => (
        /* ...existing buttons... */
      ))}
    </div>
    {onToggleDevMode && (
      <button
        type="button"
        title={devMode ? "Developer mode ON" : "Developer mode OFF"}
        onClick={onToggleDevMode}
        className={clsx(
          "p-1.5 rounded-md transition-all",
          devMode
            ? "text-gold bg-gold/10"
            : "text-muted/40 hover:text-muted"
        )}
      >
        <Settings size={14} />
        {devMode && <span className="ml-1 text-[9px] font-medium">DEV</span>}
      </button>
    )}
  </div>
);
```

- [ ] **Step 3: Add latestPattern + event arrays to JarvisMessage and stream state**

First, add typed fields to `JarvisMessage` in `types.ts` (these were added to JarvisStreamState in Task 5, now add to JarvisMessage too so they persist per-message):

```typescript
// Add to JarvisMessage interface:
export interface JarvisMessage {
  // ... existing fields ...
  toolUses?: ToolUseEvent[];
  memoriesExtracted?: MemoryExtractedEvent[];
  patternsDetected?: PatternDetectedEvent[];
}
```

Add `latestPattern` to `JarvisStreamState` in `types.ts`:

```typescript
// Add to JarvisStreamState interface:
latestPattern: PatternDetectedEvent | null;
```

Add to `INITIAL_STREAM_STATE`:

```typescript
latestPattern: null,
```

- [ ] **Step 4: Wire new events in useJarvisChat.ts**

Add refs alongside `phaseHistoryRef`:

```typescript
const toolUsesRef = useRef<ToolUseEvent[]>([]);
const memoriesExtractedRef = useRef<MemoryExtractedEvent[]>([]);
```

Reset them in sendMessage (alongside `phaseHistoryRef.current = []`):

```typescript
toolUsesRef.current = [];
memoriesExtractedRef.current = [];
```

In the `chatStream()` call's handlers object, add the 3 new handlers using typed refs:

```typescript
onToolUse: (event) => {
  toolUsesRef.current = [...toolUsesRef.current, { ...event, timestamp: Date.now() }];
  if (streamingMsg.current) {
    streamingMsg.current.toolUses = [...toolUsesRef.current];
    setMessages((m) => [...m.slice(0, -1), { ...streamingMsg.current! }]);
  }
},
onMemoryExtracted: (event) => {
  memoriesExtractedRef.current = [...memoriesExtractedRef.current, { ...event, timestamp: Date.now() }];
  if (streamingMsg.current) {
    streamingMsg.current.memoriesExtracted = [...memoriesExtractedRef.current];
    setMessages((m) => [...m.slice(0, -1), { ...streamingMsg.current! }]);
  }
},
onPatternDetected: (event) => {
  // Trigger LearningToast for high-confidence recurring patterns
  if (event.confidence >= 0.7 && event.occurrence_count >= 3) {
    setStreamState((s) => ({ ...s, latestPattern: { ...event, timestamp: Date.now() } }));
  }
},
```

- [ ] **Step 5: Export devMode from useJarvisChat**

Import and add `useDevMode` to the hook:

```typescript
import { useDevMode } from "./useDevMode";

// Inside the hook:
const [devMode, toggleDevMode] = useDevMode();

// Add to return:
return {
  // ...existing returns...
  devMode,
  toggleDevMode,
};
```

- [ ] **Step 6: Wire devMode into chat page**

In `app/(app)/chat/page.tsx`, destructure `devMode` and `toggleDevMode` from `useJarvisChat()` and pass to `ModelModeSelector`:

```tsx
<ModelModeSelector
  value={modelMode}
  onChange={setModelMode}
  disabled={isStreaming}
  devMode={devMode}
  onToggleDevMode={toggleDevMode}
/>
```

- [ ] **Step 7: Clean up ModelMode type comments**

In `types.ts`, update the `model_mode` field comment in `ChatRequest`:

```typescript
/** Model routing mode: 'auto' (Jarvis picks), '27b' (force local primary), '4b' (force cloud). Wire values — display labels come from /api/v1/models/status. */
model_mode?: 'auto' | '4b' | '27b';
```

In `ModelModeSelector.tsx`, add a doc comment at the top of the file (after imports):

```typescript
/**
 * Model mode selector — shows dynamic model names from backend.
 *
 * Wire values ("auto" | "4b" | "27b") are kept for backend compatibility:
 *   "auto" = Jarvis picks (local primary + cloud fallback)
 *   "27b"  = Force local primary (whatever is loaded in LM Studio)
 *   "4b"   = Force cloud (Gemini)
 *
 * Display labels come from GET /api/v1/models/status and show actual model names.
 */
```

In `AIChatPanel.tsx`, update the hardcoded model_mode comment:

```typescript
// Force cloud model for lightweight sidebar chat — "4b" is a wire value meaning "force cloud"
model_mode: "4b",
```

- [ ] **Step 8: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/JarvisResponse.tsx components/app/ModelModeSelector.tsx components/app/AIChatPanel.tsx lib/hooks/useJarvisChat.ts lib/types.ts app/\(app\)/chat/page.tsx
git commit -m "feat: wire IntelligentTrace, dev mode toggle, SSE event handlers, clean up ModelMode docs"
```

---

## Summary

| Task | What | Files | Scope |
|---|---|---|---|
| 1 | Backend spinner verbs | spinner_verbs.py | New file |
| 2 | Enrich v2/stream phase events | chat.py | Modify |
| 3 | Planning sub-graph tool_use events | planning_graph.py, chat.py | Modify |
| 4 | Observation loop memory/pattern events | observation.py | Rewrite |
| 5 | Frontend types + SSE + useDevMode | types.ts, api.ts, useDevMode.ts | Modify + New |
| 6 | IntelligentTrace + LearningToast | 3 new frontend files | New |
| 7 | Wire into JarvisResponse + ModelMode cleanup | 5 frontend files | Modify |

**Dependencies:** Task 2 depends on Task 1. Task 3 depends on Task 2. Tasks 5-7 are frontend-only and can be built in parallel with Tasks 1-4 (backend). Task 7 depends on Tasks 5 and 6.
