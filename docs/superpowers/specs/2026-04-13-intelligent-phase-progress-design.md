# Intelligent Phase Progress System — Design Spec

**Date:** 2026-04-13
**Author:** Madhav + Claude (Opus 4.6)
**Status:** Approved
**Brainstorm session:** `.superpowers/brainstorm/98113-1776031392/`

> **Vision:** Jarvis is a Tony Stark-style hyper-personalized intelligence. Every interaction should make it FEEL intelligent — not like a loading screen with dots, but like a brain at work that shows what it found, what it learned, and how it's reasoning.

---

## Problem Statement

The current phase progress UX is:
- Static hardcoded labels ("Digesting your brain dump... 12ms")
- No information about WHAT each phase found
- No sub-agent/module visibility
- No learning indicators (PEARL observations invisible)
- No tool-call detail for developers
- Feels like a generic chatbot, not a personal intelligence

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Default UX | Option B — fun spinner verbs + module summaries | Balance of personality and information |
| Developer mode | Option C — full tool-call tree, hooks, tokens | Toggle in settings + model selector bar |
| Spinner verb style | 80% context-aware, 20% random wildcards | Relevant but occasionally delightful |
| Spinner verb tone | Tony Stark wit + Claude Code silly | Both professional and playful |
| Learning moments | Inline (always) + Toast (high-confidence patterns) | Single observation = inline, recurring pattern = toast |
| Toast threshold | confidence >= 0.7 AND occurrence_count >= 3 | Proves Jarvis watched across sessions, not just parroting |
| Sub-agent visibility | Module-level (default), tool-call tree (dev mode) | Maps to Claude Code's spinner → expand pattern |
| Sub-step SSE events | Separate `tool_use` events per sub-step | Real-time granular streaming, frontend ignores if dev mode off |
| Dev mode toggle location | Settings page (persistent) + gear icon in model selector bar (quick) | Accessible without being prominent |

---

## Architecture

### SSE Event Contract (v2/stream)

Existing events (unchanged):
- `phase` — node completed in LangGraph graph
- `step` — intent classified, pipeline done
- `thinking` — reasoning token
- `message` — response token
- `complete` — full ChatResponse payload
- `error` — error

New events (`tool_use` — covers LLM calls, DB queries, hook decisions, solver runs; named after Claude Code's tool execution pattern, not just LLM tool_use blocks):
```
event: tool_use
data: {
  "module": "planning_module",
  "tool": "fetch_constraints",
  "status": "started" | "done" | "error",
  "detail": {                          // only on "done"
    "rows": 5,
    "duration_ms": 87,
    "model": "gemma-4-26b-a4b",        // if LLM call
    "tokens_in": 312,                   // if LLM call
    "tokens_out": 89,                   // if LLM call
    "route": "local" | "cloud",         // if LLM call
    "hook": "PreModuleExecution",        // if hook ran
    "hook_decision": "ALLOW"             // if hook ran
  }
}

event: memory_extracted
data: {
  "type": "observation",
  "content": "user is curious about mindfulness",
  "confidence": 0.45
}

event: pattern_detected
data: {
  "type": "behavioral_pattern",
  "content": "user pushes morning tasks to afternoon",
  "confidence": 0.82,
  "occurrence_count": 5,
  "action": "constraint_bridged"         // what PEARL did about it
}
```

### Enriched phase events

Existing phase events gain a `detail` object and a server-picked `verb`:
```json
{
  "phase": "brain_dump_extraction",
  "verb": "Noodling on",
  "detail": {
    "intent": "CHAT",
    "model": "gemma-4-26b-a4b",
    "duration_ms": 1200
  }
}
```

The `verb` is picked server-side (80/20 context/wildcard) so it's consistent if the same event is re-rendered.

---

## Component Design

### 1. IntelligentTrace.tsx (replaces PhaseProgress.tsx + PipelineTrace.tsx)

Consolidates the two duplicate phase display components into one.

**Default mode (Option B):**
```
✓ Recollecting your context... 0.3s
  → 3 memories loaded · conversation: 4 turns

✓ Noodling on your message... 1.2s
  → Intent: CHAT — just a conversation

⟳ Cogitating a response... 3.4s
  → gemma-4-26b · 847 tok/s

✓ Absorbing what I learned... 0.2s
  → 🧠 Noted: "user is curious about mindfulness"
```

**Dev mode (Option C) — same phases but with tool-call tree:**
```
✓ Recollecting your context... 0.3s
  → 3 memories · 4 turns · UserModel cache: hit
  ⚙ load_context → no DB call (cached)

✓ Noodling on brain dump... 1.2s
  → Intent: CHAT
  ⚙ route_llm_call(brain_dump_extraction) → gemma-4-26b (local) · 312/89 tok
  🔒 PreModuleExecution → ALLOW (user-initiated)

✓ Cogitating response... 3.4s
  → conversation_module · 847 tok/s
  ⚙ route_llm_call(voice_of_jarvis) → gemma-4-26b (local) · 1,247/523 tok
  📎 conversation_history: 4 msgs · memory_context: 127 chars
  ⏱ TTFT: 234ms · Stream: 1,892ms

✓ Absorbing learnings... 0.2s
  → 🧠 Noted: "user curious about mindfulness"
  ⚙ observation_loop · extract_memories: 1 new · PEARL: no patterns
  🔒 PreMemoryWrite → ALLOW
```

**PLAN_DAY with sub-graph visibility (dev mode):**
```
⟳ planning_module
  ├─ fetch_constraints → Supabase · 5 rows, 87ms
  ├─ route_llm_call(habit_translation) → gemma-4-26b · 2.1s · 3 slots
  ├─ expand_slots → 3 semantic → 12 time slots
  ├─ route_llm_call(socratic_chunker) → gemma-4-26b · 4.3s → 7 tasks
  ├─ fuse_tasks → 2 pending merged → 9 total
  ├─ OR-Tools CP-SAT → OPTIMAL 87ms · 9 tasks · 48h horizon
  └─ PreScheduleModify hook → ASK "Schedule 9 tasks?"
```

### 2. spinnerVerbs.ts

```typescript
// Context-aware verb pools (80% chance)
const PHASE_VERBS: Record<string, string[]> = {
  loading_context:       ["Recollecting", "Summoning", "Dusting off", "Booting up", "Rehydrating"],
  brain_dump_extraction: ["Deciphering", "Noodling on", "Untangling", "Dissecting", "Decoding", "Unpacking"],
  intent_classified:     ["Sussing out", "Reading between the lines", "Deducing", "Profiling"],
  planning:              ["Orchestrating", "Choreographing", "Tetris-ing", "Blueprinting", "War-rooming"],
  habits_fetched:        ["Rounding up", "Herding", "Cataloguing", "Mustering"],
  translating:           ["Weaving", "Translating", "Mapping out", "Threading"],
  decomposing:           ["Decomposing", "Socratic-chunking", "Slicing into micro-tasks", "Breaking down"],
  scheduling:            ["Crunching", "Optimizing", "Number-wrangling", "Clockwork-ing", "Tetrimino-ing"],
  researching:           ["Spelunking", "Excavating", "Rummaging the web", "Sleuthing"],
  coaching:              ["Checking in on", "Pep-talking", "Reviewing your wins"],
  ingesting:             ["Munching on", "Digesting", "Absorbing", "Inhaling"],
  synthesizing:          ["Crafting", "Weaving", "Distilling", "Bottling up", "Composing"],
  responding:            ["Composing", "Penning", "Wordsmithing", "Articulating"],
  learning:              ["Absorbing", "Filing away", "Cerebrating", "Etching into memory", "Jotting down"],
};

// Wildcard pool (20% chance — shared across all phases)
const WILDCARD_VERBS: string[] = [
  "Jarvising", "Flambéing", "Moonwalking through", "Discombobulating",
  "Prestidigitating", "Combobulating", "Quantum-tunneling",
  "Vibing with", "Percolating", "Gallivanting through",
  "Shenaniganing", "Sock-hopping through", "Razzle-dazzling",
  "Arc-reactoring", "Hullaballooing over", "Stark-industrializing",
  "Beboppin' through", "Flibbertigibbeting", "Canoodling with",
  "Lollygagging over", "Tomfoolering with",
  "Hyperspacing through", "Wibbling at", "Whatchamacalliting",
];

export function getSpinnerVerb(phase: string): string {
  const useWildcard = Math.random() < 0.2;
  if (useWildcard) {
    return WILDCARD_VERBS[Math.floor(Math.random() * WILDCARD_VERBS.length)];
  }
  const pool = PHASE_VERBS[phase] || PHASE_VERBS["responding"];
  return pool[Math.floor(Math.random() * pool.length)];
}
```

### 3. LearningToast.tsx

A slide-in card that appears at the bottom of the chat when PEARL detects a high-confidence pattern.

**Trigger condition:** `pattern_detected` SSE event with `confidence >= 0.7 AND occurrence_count >= 3`

**Appearance:** Warm card with brain icon, pattern description, and what Jarvis did about it.
```
┌─────────────────────────────────────────────────────┐
│ 🧠 Sir, I've noticed something                     │
│                                                     │
│ You push morning deep-work to afternoon every time. │
│ I've adjusted: no complex tasks before 11am now.    │
│                                                     │
│                              [Got it]  [Undo]       │
└─────────────────────────────────────────────────────┘
```

Auto-dismisses after 10 seconds if not interacted with. "Undo" reverts the constraint bridge. Dismissed toasts are still visible in the phase trace inline (they don't disappear entirely — the toast is just the prominent notification).

### 4. DevModeToggle

**Settings page:** Checkbox "Developer Mode — Show tool calls, hooks, token counts, model routing"

**Model selector bar:** Small `⚙` gear icon next to the model buttons. Click toggles dev mode. When active, gear turns amber and shows "DEV" label.

**Hook:** `useDevMode()` reads/writes `localStorage("jarvis-dev-mode")`.

---

## Backend Changes Required

### 1. Emit `tool_use` events from LangGraph nodes

Each module wrapper node (e.g., `_planning_module_node`) emits `tool_use` events via the progress_queue when:
- A sub-graph node starts/completes
- An LLM call is made (model, tokens, route)
- A hook decision is made
- A DB query runs

### 2. Emit `memory_extracted` and `pattern_detected` from observation loop

`run_observation_loop` currently stubs all functions. When implemented:
- `extract_and_store_memories()` → emit `memory_extracted` event
- `detect_pearl_patterns()` → emit `pattern_detected` event if any found

### 3. Server-side verb selection

Add `verb` field to phase event data. The verb is picked in the backend (not frontend) so it's deterministic per event — if the same message is re-rendered, same verb appears.

```python
from app.utils.spinner_verbs import get_spinner_verb

yield f"event: phase\ndata: {json.dumps({'phase': phase, 'verb': get_spinner_verb(phase), **detail})}\n\n"
```

### 4. Enriched detail payloads

Each phase event includes a `detail` dict with module-specific information:
- `load_context`: memories_count, conversation_turns, cache_status
- `brain_dump_extraction`: intent, model, tokens
- `classify_intent`: intent, method (rule-based vs LLM)
- Module nodes: model, tokens_in, tokens_out, route, duration_ms
- `observation_loop`: memories_extracted_count, patterns_detected_count

---

## Files to Create/Modify

### Frontend (new files)
- `lib/spinnerVerbs.ts` — verb pools + `getSpinnerVerb(phase)`
- `components/app/IntelligentTrace.tsx` — replaces PhaseProgress + PipelineTrace
- `components/app/LearningToast.tsx` — PEARL pattern toast
- `lib/hooks/useDevMode.ts` — dev mode state hook

### Frontend (modify)
- `components/app/ModelModeSelector.tsx` — add gear icon toggle
- `lib/api.ts` — handle `tool_use`, `memory_extracted`, `pattern_detected` SSE events
- `lib/types.ts` — add ToolUseEvent, MemoryExtractedEvent, PatternDetectedEvent types
- `lib/hooks/useJarvisChat.ts` — wire new events to state
- `lib/constants.ts` — keep PHASE_NAMES as fallback for completed phase labels and ThinkingProcess.tsx; spinner verbs used only for active/running phase display
- `components/app/JarvisResponse.tsx` — use IntelligentTrace instead of PhaseProgress

### Backend (new files)
- `app/utils/spinner_verbs.py` — server-side verb selection

### Backend (modify)
- `app/api/v1/endpoints/chat.py` — enrich phase events with verb + detail, emit tool_use events
- `app/core/observation.py` — emit memory_extracted and pattern_detected via progress_callback
- `app/orchestrator/graph.py` — pass progress_callback to module nodes for tool_use events
- `app/modules/planning_graph.py` — emit tool_use events from sub-graph nodes

### Backend (already fixed in this session)
- `app/modules/planning_graph.py` — ExecutionGraph import (was from wrong module)
- `app/orchestrator/graph.py` — IntentType enums instead of strings

---

## Out of Scope (future)

- Observation loop real implementation (separate spec — this is the UI for it)
- PEARL pattern detection algorithm
- MemorySaver checkpointing (requires UserModel serialization)
- Consent request UI (consent_request SSE events emitted but no frontend handler yet)
- DKT/RL/SARIMAX integration
- PII filter upgrade (Guardrails AI / Llama 3.2-1B)
