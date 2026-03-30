# Jarvis Spec-Compliance Fix Design

**Date:** 2026-03-30
**Goal:** Align backend + frontend implementation with the architecture reset spec (2026-03-28), strictly following every flow diagram.
**Deadline:** April 1, 2026 (VC pitch demo-ready)

---

## Context

An audit of the codebase against `2026-03-28-jarvis-architecture-reset-design.md` revealed significant deviations in model routing, intent handling, registry framework usage, draft negotiation, PEARL integration, and frontend wiring. This spec defines exact fixes to bring the implementation into **strict compliance** with every architecture flow diagram.

---

## The Core Loop (MUST Match Exactly)

```
User message
    │
    ▼
┌──────────────────────┐
│   MEMORY RETRIEVAL   │  Score & retrieve top-K from user_memories
│   (read side)        │  Score = Relevance × Recency × Importance × Confidence
│   Top-K injection    │  Always include: active constraints, goals, patterns (confidence > 0.6)
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   BRAIN DUMP         │  Primary: Gemini 2.5 Flash (cloud, fast, schema-reliable)
│   EXTRACTION         │  Fallback: Qwen-4B + JSON schema mode (local)
│   (ALWAYS RUNS)      │  Output: BrainDumpExtraction (Pydantic)
│                      │  NEVER uses 27B for this step
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   INTENT REGISTRY    │  Embed input → cosine similarity vs all intent descriptions
│   CLASSIFICATION     │  Qwen-4B classifies into best-matching registered intent
│                      │  Static + learned intents. Fallback: CHAT
└──────────┬───────────┘
           │
           ├── [max_similarity < 0.65] ──────────────────────────────────────────┐
           │                                                                      ▼
           │                                               ┌──────────────────────────────┐
           │                                               │   INTENT DISCOVERY ENGINE    │
           │                                               │                              │
           │                                               │  Real-time: embedding gap    │
           │                                               │   → increment freq counter   │
           │                                               │   → route to CHAT for now    │
           │                                               │                              │
           │                                               │  Batch (async):              │
           │                                               │   semantic clustering of     │
           │                                               │   CHAT fallbacks             │
           │                                               │                              │
           │                                               │  counter ≥ 3 OR cluster hit: │
           │                                               │   Gemini generates           │
           │                                               │   IntentBlueprint schema     │
           │                                               │                              │
           │                                               │  Supervised mode:            │
           │                                               │   → propose to user          │
           │                                               │  Autonomous mode:            │
           │                                               │   → auto-register + notify   │
           │                                               └──────────────────────────────┘
           │
           ▼
┌──────────────────────┐
│   INTENT REGISTRY    │  BaseRegistry pattern — extensible
│   DISPATCH           │  Looks up handler, calls it. NO if/elif cascade.
└──────────┬───────────┘
           │
           ├── PLAN_DAY ──► Plan Day Pipeline (see below)
           ├── EDIT_TASK ──► Modify task in Supabase → trigger_replan
           ├── REARRANGE ──► Swap/move tasks → trigger_replan
           ├── ADD_CONSTRAINT ──► Store in behavioral_constraints → replan
           ├── ACCEPT_DRAFT ──► Persist draft to user_tasks + calendar
           ├── REJECT_DRAFT ──► Discard + ask why → build memory
           ├── INGEST_DOCUMENT ──► Document Intelligence Pipeline
           ├── CHECK_PROGRESS ──► Query tasks + completion stats
           ├── DYNAMIC_INTENT ──► Load IntentBlueprint → Execute steps
           └── CHAT ──► Voice of Jarvis (general conversation)
                                    │
                                    ▼
                        ┌──────────────────────┐
                        │   VOICE OF JARVIS    │  Primary: Qwen-4B local
                        │   (response synth)   │  Fallback: Gemini 2.5 Flash
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  MEMORY EXTRACTION   │  Primary: Qwen-4B local (fire-and-forget)
                        │  (write side)        │  Extract facts, preferences, patterns
                        │                      │  → PEARL pattern detection
                        │                      │  → Store in user_memories
                        └──────────────────────┘
```

**CRITICAL CORRECTION from previous spec version:** Brain dump extraction ALWAYS runs (even for "hi"). It just needs to be fast because it uses **Gemini 2.5 Flash** (primary) or **Qwen-4B** (fallback) — NOT 27B. The current code uses 27B for brain dump, which is why "hi" takes ~10 seconds.

---

## Workstream 1: Backend Architecture Flow

### 1.1 Fix LLM Routing (Inverted from Current)

The spec explicitly states "LLM Routing (Inverted from Current)". The routing table must be:

| Task | Primary | Fallback | Rationale |
|------|---------|----------|-----------|
| Brain dump extraction | **Gemini 2.5 Flash** | Qwen-4B + JSON schema mode | Schema reliability critical |
| Task decomposition (Socratic Chunker) | **Gemini 2.5 Flash** | Qwen-27B + JSON schema mode (Phase 1) / Qwen-8B (Phase 2 when available) | Quality of decomposition matters most |
| Habit translation | **Gemini 2.5 Flash** | Qwen-4B | Structured output needed |
| Intent classification | **Qwen-4B local** (JSON schema mode) | Gemini 2.5 Flash | Fast, simple classification — local is fine |
| Voice of Jarvis synthesis | **Qwen-4B local** | Gemini 2.5 Flash | Creative text, local handles well |
| Memory extraction | **Qwen-4B local** | Gemini 2.5 Flash | Background task, doesn't need to be perfect |
| Real-time web search | **Gemini 2.5 Flash** | N/A | Only cloud can do this |
| Intent blueprint generation | **Gemini 2.5 Flash** | Qwen-4B | Needs deep understanding to generate valid step sequences |
| Intent pattern clustering (batch) | **Qwen-4B local** | Gemini 2.5 Flash | Async background task, local quality sufficient |

**Current code violations:**
- `control_policy.py:_run_brain_dump_extraction` uses SLM_ROUTER_MODEL (4B) but the spec says **Gemini Flash primary** → Fix: route to Gemini Flash first, fall back to 4B locally
- `control_policy.py` decomposition path uses LOCAL_LLM_MODEL (27B) as primary → Fix: route to Gemini Flash first, fall back to 27B locally
- `habit_translator.py` uses 27B as primary → Fix: route to Gemini Flash first, fall back to 4B locally
- Voice of Jarvis correctly uses 4B
- Memory extraction correctly uses 4B

**Files to modify:**
- `app/models/brain/litellm_conf.py` — Add routing helpers: `gemini_primary_route(prompt, schema, fallback_model)` and `local_primary_route(prompt, schema)`
- `app/services/analytical/control_policy.py` — Brain dump extraction calls `gemini_primary_route`, intent classification calls `local_primary_route`
- `app/services/analytical/habit_translator.py` — Switch to `gemini_primary_route`

**Key constraint preserved:** Never run two 27B calls concurrently (OOM on 24GB M4 Pro). With Gemini Flash as primary for structured tasks, 27B is only used as fallback for decomposition — and that's sequential anyway.

### 1.2 Intent Classification & Registry (Embedding-Based + Discovery Engine)

**Problem:** Intent registry has stub handlers. Control policy uses if/elif cascade. Classification uses LLM prompt only, not embedding similarity.

**The spec requires a TWO-STAGE classification:**

**Stage 1 — Embedding Match:**
- Embed the user input
- Compute cosine similarity against all registered intent descriptions (each intent has a description embedding)
- If `max_similarity >= 0.65` → route to best-matching intent
- If `max_similarity < 0.65` → trigger Intent Discovery Engine + route to CHAT as fallback

**Stage 2 — Qwen-4B Confirmation:**
- Qwen-4B local classifies into the best-matching registered intent (fast, ~100ms)
- This confirms/overrides the embedding match for edge cases

**The registry framework is the architectural backbone. Every extensible subsystem uses it:**

| Registry | Fallback | Current Entries | Adding New |
|----------|----------|----------------|------------|
| Intent Registry | CHAT | PLAN_DAY, EDIT_TASK, REARRANGE, ADD_CONSTRAINT, ACCEPT_DRAFT, REJECT_DRAFT, INGEST_DOCUMENT, CHECK_PROGRESS, DYNAMIC_INTENT, CHAT | Define handler + register |
| Document Type Registry | reference | practice_problems, lecture_notes, syllabus, assignment, reference | Define handler + register |
| Memory Type Registry | fact | fact, preference, behavioral_pattern, temporal_event, goal, feedback, constraint | Define type config + register |
| PEARL Pattern Registry | N/A | skip_time_window, duration_preference, deadline_buffer | Define detector query + register |

**Fix for Intent Registry:**

1. **Replace stubs with real async handlers** in `intent_registry.py`:

```python
# Each handler receives IntentContext and returns ChatResponse
async def handle_plan_day(ctx: IntentContext) -> ChatResponse:
    return await _run_plan_day_flow(ctx)

async def handle_edit_task(ctx: IntentContext) -> ChatResponse:
    # Modify task in Supabase → trigger_replan
    ...

async def handle_rearrange(ctx: IntentContext) -> ChatResponse:
    # Swap/move tasks → trigger_replan
    ...

async def handle_add_constraint(ctx: IntentContext) -> ChatResponse:
    # Store in behavioral_constraints → trigger_replan
    ...

async def handle_accept_draft(ctx: IntentContext) -> ChatResponse:
    # Persist current pending draft to user_tasks
    ...

async def handle_reject_draft(ctx: IntentContext) -> ChatResponse:
    # Discard draft + ask why → store rejection reason as memory
    ...

async def handle_check_progress(ctx: IntentContext) -> ChatResponse:
    # Query tasks + completion stats → Voice of Jarvis summary
    ...

async def handle_dynamic_intent(ctx: IntentContext) -> ChatResponse:
    # Load IntentBlueprint → Execute steps
    ...

async def handle_chat(ctx: IntentContext) -> ChatResponse:
    # General conversation → Voice of Jarvis
    ...
```

2. **Control policy dispatch uses registry** (NO if/elif):

```python
handler = intent_registry.get(classified_intent)
if handler is None:
    handler = intent_registry.get("CHAT")  # fallback
response = await handler.handler(intent_context)
```

### 1.2b Intent Discovery Engine (Self-Evolving Intents)

When `max_similarity < 0.65` (no good intent match), the system doesn't just fallback to CHAT — it learns:

```
Real-time (per request):
  1. Log the embedding gap (user input + embedding + timestamp)
  2. Increment frequency counter for this embedding region
  3. Route to CHAT for now (user still gets a response)

Batch (async background):
  4. Semantic clustering of accumulated CHAT fallbacks
  5. When counter >= 3 OR cluster hit detected:
     → Gemini 2.5 Flash generates IntentBlueprint schema
       (name, description, handler_steps, required_context, example_triggers)

Registration:
  6a. Supervised mode → propose to user: "I noticed you often ask about X. Should I learn this as a new intent?"
  6b. Autonomous mode → auto-register + notify user: "I learned a new intent: X"
```

**IntentBlueprint** (schema to be fully defined during implementation):
- `name`: intent identifier
- `description`: natural language description (used for embedding match)
- `handler_steps`: sequence of actions to execute
- `required_context`: what data the handler needs from BrainDumpExtraction
- `example_triggers`: sample user inputs that match this intent

**DYNAMIC_INTENT handler:** Loads the IntentBlueprint and executes its `handler_steps` sequence. This is how learned intents run without hardcoded handlers.

**LLM routing for discovery:**
- Intent blueprint generation: Gemini 2.5 Flash (primary), Qwen-4B (fallback)
- Intent pattern clustering: Qwen-4B local (primary), Gemini 2.5 Flash (fallback)

**Files to create/modify:**
- `app/services/intent_discovery.py` — New: frequency counter, clustering, blueprint generation
- `app/services/intent_registry.py` — Add DYNAMIC_INTENT handler, embedding match logic
- `app/schemas/context.py` — Add IntentBlueprint schema

### 1.3 Plan Day Pipeline (Sequential, Strict Order)

Must follow this exact sequence:

```
1. Fetch behavioral_constraints from Supabase
2. Translate habits → SemanticTimeSlots (Gemini Flash primary, Qwen-4B fallback)
3. Memory → Constraint Bridge (convert behavioral_pattern memories to OR-Tools TimeSlots)
4. Socratic Chunker: decompose goal → TaskChunks (Gemini Flash primary, Qwen-8B fallback)
5. Fusion with pending tasks (get_all_pending_tasks, namespace {goal_id}_{task_id})
6. compute_horizon_from_deadlines + compute_adaptive_daily_cap
7. OR-Tools CP-SAT Solver (hard blocks, soft blocks, TMT priority, dependencies)
8. Create DRAFT (NOT persisted to user_tasks yet — stored in draft_schedules table with 24h TTL)
```

**Note:** Step 3 (Memory → Constraint Bridge) is a NEW pipeline step not in the original CLAUDE.md flow. It converts PEARL-detected behavioral_pattern memories into OR-Tools TimeSlots. Implementers must insert this between habit translation and decomposition.

**Multi-Goal Fusion (when new tasks added over existing):**
1. `get_all_pending_tasks` — fetch existing pending chunks from Supabase
2. Namespace new chunks: `{goal_id}_{task_id}` to prevent collisions
3. Merge into `master_chunk_list`
4. `compute_horizon_from_deadlines` across ALL goals
5. Single OR-Tools solve covers all goals; TMT prioritizes by nearest deadline
6. `_persist_fused_tasks` — replace all pending rows atomically

### 1.4 Draft Negotiation Loop (Full Implementation)

The draft negotiation is a **loop**, not a one-shot:

```
DRAFT Created → User Reviews →
  ├── Accept All → Persist to user_tasks + PEARL observe what was accepted
  ├── Edit Task → Modify task fields → Re-solve OR-Tools → Updated Draft → Review again
  ├── Reject → Ask why → Store rejection reason as memory → Generate new approach → Review again
  ├── Chat to Modify → "Move DSA to afternoon" → Re-decompose if needed → Re-solve → Review again
  └── Rearrange → Swap task positions → Re-solve → Updated Draft → Review again
```

**Required endpoints (3 exist, 2 missing):**
- `GET /drafts/{draft_id}` — Retrieve draft state (EXISTS)
- `POST /drafts/{draft_id}/accept` — Accept + persist (EXISTS)
- `POST /drafts/{draft_id}/reject` — Reject + store reason as memory (EXISTS but doesn't build memory)
- `PATCH /drafts/{draft_id}/tasks/{task_id}` — Edit individual task (EXISTS)
- `POST /drafts/{draft_id}/rearrange` — **MISSING** — Swap task order, re-trigger OR-Tools
- `POST /drafts/{draft_id}/chat` — **MISSING** — Natural language modification, re-decompose if needed

**Fix for reject endpoint:** After rejection, extract the rejection reason and store it as a memory in user_memories (type: feedback). This builds Jarvis's understanding of user preferences.

**Fix for accept endpoint:** After acceptance:
1. Persist to user_tasks
2. Background: trigger_replan for remaining tasks if needed
3. Background: PEARL observe what was accepted/edited (feeds pattern detection)

### 1.5 Document Intelligence Pipeline (Registry-Based)

Must follow this flow:

```
Document Arrives → Docling: Extract Structured Text
    → Generate Classification Prompt from Registry (auto-discovers registered types)
    → LLM Classifies Using Registry Descriptions
    → Registry Lookup: get_or_fallback (fallback = "reference")
    → Execute Registered Handler
    → Check handler.metadata.modifies_tasks?
        ├── Yes → trigger_replan
        └── No → Notify: Material linked
```

**Document Type Registry entries (5 core types):**

| Type | Handler | modifies_tasks |
|------|---------|---------------|
| practice_problems | Extract individual problems, match to tasks by topic, add as completion criteria, propose practice tasks | Yes → trigger_replan |
| lecture_notes | Extract concepts, link to tasks as study material | No |
| syllabus | Extract topics + deadlines, update task deadlines, propose new tasks | Yes → trigger_replan |
| assignment | Extract requirements + deadline, add as completion criteria OR create new task | Yes → trigger_replan |
| reference | Chunk + store in ChromaDB, link by similarity | No |

**Key principle:** The classifier auto-discovers new types because it reads from the registry. Adding a new document type tomorrow = define handler + register(). No code changes to the pipeline.

### 1.6 PEARL Pattern Detection (Fully Integrated)

**Problem:** `detect_patterns()` exists but is never called. Only 2 of 3 patterns implemented.

**Flow from spec:**

```
User Behavior Signals (Complete, Skip, Edit, Reject, Accept)
    → Aggregate by Category
    → 3 Detectors:
        1. Time Window Patterns (skips before 10AM?)
        2. Duration Patterns (always edits to shorter?)
        3. Deadline Patterns (always extends by 2 days?)
    → Threshold: 3+ observations AND rate > 70%?
        ├── Yes (New) → Create behavioral_pattern memory
        ├── Yes (Existing) → Reinforce pattern (stability++, confidence++)
        └── No → Discard (insufficient evidence)
    → Memory → Constraint Bridge:
        ├── SoftBlock → Add as soft block in OR-Tools
        ├── AdjustDefaults → Adjust default chunk duration in Socratic Chunker
        ├── AdjustHorizon → Adjust horizon buffer
        └── ProactiveSurface → "I noticed you always skip morning tasks..."
```

**Fixes:**
1. **Add `detect_deadline_buffer()`** to PEARL registry — observes recurring deadline extensions
2. **Rename `completion_time_preference` to `duration_preference`** to match spec naming
3. **Call `detect_patterns()` after these events** (fire-and-forget via asyncio.create_task):
   - Task completion (`POST /tasks/{task_id}/complete`)
   - Task skip (`POST /tasks/{task_id}/skip`)
   - Draft rejection (`POST /drafts/{draft_id}/reject`)
   - Draft acceptance (`POST /drafts/{draft_id}/accept`)
   - Task edit (`PATCH /drafts/{draft_id}/tasks/{task_id}`)
4. **Wire constraint bridge** so detected patterns actually become soft blocks in next OR-Tools solve

### 1.7 Memory System (3-Tier, SM-2 Decay, Strict Lifecycle)

**The memory lifecycle MUST follow this exact state machine:**

```
[*] → NewMemory (extracted from conversation or behavior)
  │
  ▼
Active (stored with confidence=0.5, stability=1.0)
  │
  ├── User repeats preference OR behavior confirmed
  │   → Reinforced: stability++, confidence += 0.1, strength = 1.0
  │   → Back to Active
  │
  ├── Time passes without reinforcement
  │   → Decaying: strength = Initial × exp(-t / (stability × 7days))
  │   │
  │   ├── Reinforced again → Back to Active (stability++, confidence++, strength=1.0)
  │   └── strength < 0.1 → Archived (pruned from active queries, kept for history)
  │
  └── Contradiction detected
      → Superseded: old.superseded_by = new.id
      → Old memory NOT deleted — preserved for pattern analysis
        ("User changed from morning to night person in March")
```

**Scoring formula (all 4 factors required):**
```
Score = Relevance × Recency × Importance × Confidence

Relevance = cosine_similarity(query_embedding, memory_embedding)
Recency   = strength × exp(-t / (stability × 7days))
Importance = IMPORTANCE_WEIGHTS (from architecture spec code listing):
  constraint: 1.0, behavioral_pattern: 0.9, preference: 0.8,
  temporal_event: 0.8, goal: 0.7, fact: 0.6, feedback: 0.5
Confidence = reinforcement-based (starts 0.5, grows with each confirmation)
```

**SM-2 Decay (Ebbinghaus Forgetting Curve):**
```
Memory_Strength(t) = Initial × exp(-t / (stability × base_halflife))
  • t = hours since last reinforcement
  • stability starts at 1.0, increments each reinforcement (capped at 20)
  • base_halflife = 168 hours (7 days)
  • At stability=1: half-life = 1 week
  • At stability=5: half-life = 5 weeks
  • At stability=20: half-life = 140 days
```

**Audit deviations and fixes:**

1. **Missing "Importance" factor** — Code computes `Relevance × Recency × Confidence` only.
   - Fix: Add `IMPORTANCE_WEIGHTS` dict in `retriever.py`: `{constraint: 1.0, behavioral_pattern: 0.9, preference: 0.8, temporal_event: 0.8, goal: 0.7, fact: 0.6, feedback: 0.5}`, multiply into score

2. **No automatic contradiction detection** — `supersede_memory()` exists but only manual.
   - Fix: In `extractor.py`, after extracting new memories, compare against existing memories of same type. If semantic similarity > 0.85 but content contradicts → `supersede_memory(old_id, user_id, new_content)`

3. **Memory extraction not triggering PEARL** — Spec: `Response → Extract Memories → PEARL Detection → Store`.
   - Fix: Chain: `safe_extract_memories()` → `detect_patterns()` in same fire-and-forget task

4. **No lifecycle state tracking** — Memories don't track their state (Active/Decaying/Archived/Superseded).
   - Fix: Add `state` field to user_memories: `'active' | 'decaying' | 'archived' | 'superseded'`
   - Active queries filter: `state IN ('active', 'decaying')` AND `strength > 0.1`
   - Reinforcement resets state to `'active'` and strength to 1.0
   - Background job or on-read check: if `strength < 0.1` → set state to `'archived'`

5. **No memory archival pruning** — Low-strength memories should be excluded from active queries but kept.
   - Fix: `get_active_memories()` filters by `state != 'archived'` AND `state != 'superseded'`

### 1.8 Recall Memory Tier (Session Management)

**MISSING from current fix spec — Critical.** The 3-tier memory model requires Recall Memory:

**Recall Memory lives in Supabase `conversation_sessions` + `conversation_messages` tables:**
- Each conversation session stores: session_id, user_id, started_at, ended_at, summary (LLM-generated 2-3 sentences), goals_discussed, mood_signals
- Messages stored in `conversation_messages` with session_id FK
- Searchable by similarity + date range
- 30-minute inactivity timeout → auto-close session → generate summary via LLM

**Session lifecycle:**
```
get_or_create_session(user_id):
  1. Check for active session (no ended_at, last_message < 30min ago)
  2. If found → reuse session_id
  3. If not → close old session (generate summary) → create new session

close_session(session_id):
  1. Generate 2-3 sentence summary via Qwen-4B
  2. Extract goals discussed
  3. Store summary in conversation_sessions
  4. Set ended_at
```

**Integration:** Every `/chat` request calls `get_or_create_session()` before memory retrieval. Recall memories (session summaries) are injected into Working Memory alongside archival memories.

**Files to modify:**
- `app/services/chat_history.py` — Implement session lifecycle (get_or_create, close, summary generation)
- `app/api/v1/endpoints/chat.py` — Call session management at request start

### 1.9 Draft Persistence Model

**Drafts are stored in Supabase `draft_schedules` table, NOT in-memory:**

```sql
draft_schedules (
  id UUID PRIMARY KEY,
  user_id TEXT NOT NULL,
  goal_id TEXT,
  tasks JSONB NOT NULL,           -- Array of DraftTask objects
  horizon_start TIMESTAMPTZ,
  status TEXT DEFAULT 'pending',  -- 'pending' | 'accepted' | 'rejected' | 'modified' | 'expired'
  rejection_reason TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  expires_at TIMESTAMPTZ          -- 24h TTL from creation
)
```

**Pydantic schemas (from architecture spec):**
- `DraftSchedule`: id, user_id, goal_id, tasks (List[DraftTask]), horizon_start, status, rejection_reason, expires_at
- `DraftTask`: task_id, title, duration_minutes, difficulty_weight, completion_criteria, implementation_intention, dependencies, start_min, end_min
- `DraftAction`: action ('accept'|'reject'|'edit'|'rearrange'|'chat'), target_task_id, modifications, reason

**Draft lifecycle:** pending → accepted | rejected | modified | expired (24h TTL auto-expiry)

**Files to modify:**
- `app/services/draft_store.py` — Ensure all methods use Supabase (remove legacy in-memory no-op aliases)
- `app/schemas/context.py` — Add/verify DraftSchedule, DraftTask, DraftAction schemas

### 1.10 `trigger_replan` Definition

**Referenced by:** EDIT_TASK, REARRANGE, ADD_CONSTRAINT handlers, document pipeline (modifies_tasks), draft accept

**Implementation (background async task):**
```python
async def trigger_replan(user_id: str):
    """Background replan: fetch current state → re-solve → persist."""
    # 1. Fetch all pending tasks from user_tasks
    pending_tasks = await get_all_pending_tasks(user_id)
    if not pending_tasks:
        return

    # 2. Fetch behavioral constraints + habit translations
    habits = await get_behavioral_context_for_calendar(user_id)
    slots = await translate_habits_to_slots(habits)  # Gemini Flash primary

    # 3. Memory → Constraint Bridge
    memory_constraints = await memories_to_constraints(user_id, memory_store)

    # 4. Merge habit slots + memory constraints
    all_constraints = slots + memory_constraints

    # 5. Re-run OR-Tools with updated constraints
    schedule = await run_schedule(pending_tasks, all_constraints)

    # 6. Persist updated schedule
    await _persist_fused_tasks(user_id, schedule, pending_tasks)
```

**Always called via `asyncio.create_task(trigger_replan(user_id))` — never blocks the response.**

### 1.11 `behavioral_constraints` → `user_memories` Migration

**Phased coexistence strategy (from architecture spec):**

Phase 1 (Current): behavioral_constraints table is the source of truth for habits. user_memories stores PEARL-detected behavioral patterns.

Phase 2 (Migration): New habits stored as user_memories with type='constraint'. Old behavioral_constraints read-only.

Phase 3 (Complete): behavioral_constraints table deprecated. All constraints live in user_memories.

**For this fix:** Phase 1 — no migration needed yet. Both tables coexist. Plan Day Pipeline reads from behavioral_constraints for explicit habits AND from user_memories for PEARL patterns (via constraint bridge). Document this coexistence in code comments.

### 1.12 BaseRegistry Enforcement for All 4 Registries

**All 4 registries MUST instantiate `BaseRegistry` from `app/core/registry.py`:**

| Registry | Instance Variable | File |
|----------|------------------|------|
| Intent Registry | `intent_registry` | `app/services/intent_registry.py` (exists, needs handler wiring) |
| Document Type Registry | `document_registry` | `app/services/extraction/orchestrator.py` (verify or create) |
| Memory Type Registry | `memory_type_registry` | `app/services/memory/store.py` (create if missing) |
| PEARL Pattern Registry | `pearl_registry` | `app/services/memory/pearl.py` (exists, verify BaseRegistry usage) |

Each registry uses `register()`, `get()`, `get_or_fallback()`, `classification_prompt()` from BaseRegistry.

### 1.13 Streaming SSE Events (Backend → Frontend)

**Note:** SSE events are not explicitly defined in the architecture spec but are derived from the pipeline stages to support the frontend PhaseProgress component.

The backend must emit these SSE events in order:

```
event: phase
data: {"phase": "brain_dump_extraction", "model": "gemini-2.5-flash"}

event: phase
data: {"phase": "intent_classified", "intent": "PLAN_DAY"}

event: phase
data: {"phase": "decomposing", "goal": "Study DSA", "model": "gemini-2.5-flash"}

event: phase
data: {"phase": "scheduling"}

event: thinking
data: {"token": "Let me break this down..."}

event: message
data: {"token": "I've organized your day..."}

event: complete
data: {full ChatResponse JSON}
```

**Fix:** Ensure `control_policy.py` emits `phase` events at each pipeline stage so the frontend PhaseProgress component can display them with fun names and timing.

---

## Workstream 2: Chat UI (Frontend)

### 2.1 ThinkingProcess: Use Fun Phase Names

**Problem:** `ThinkingProcess.tsx` hardcodes "Thinking" and "Thought for Xs".

**Fix:**
- Import `getPhaseDisplayName` from `lib/constants`
- When streaming: show the current phase's display name from the latest phase in `phaseHistory`
- When complete: show "Thought for Xs" (keep as-is)
- If phase is "reasoning", show "Putting on my thinking cap..."
- If phase is "connecting", show "Brewing your plan..."

**Phase names (from constants.ts):**
```
connecting           → "Brewing your plan..."
brain_dump_extraction → "Digesting your brain dump..."
intent_classified    → "Aha, figuring out what you need..."
decomposing          → "Breaking it into bite-sized pieces..."
translating          → "Reading your habits..."
scheduling           → "Crunching the numbers..."
reasoning            → "Putting on my thinking cap..."
responding           → "Crafting your response..."
synthesizing         → "Adding the finishing touches..."
complete             → "Voila!"
```

### 2.2 Wire Calendar Approval Handlers

**Problem:** `JarvisResponse` accepts `onCalendarApproved`/`onCalendarRejected` props but chat page never passes them.

**Fix:** In `chat/page.tsx`:
```typescript
onCalendarApproved={(entryId) => approveCalendarEntry(entryId)}
onCalendarRejected={(entryId) => rejectCalendarEntry(entryId)}
```
Wire to `POST /api/v1/ingestion/pending-calendar/{id}/approve` and `reject`.

### 2.3 Persist Model Mode to localStorage

**Fix:** In `useJarvisChat.ts`:
- Initialize: `useState<ModelMode>(() => localStorage.getItem('jarvis-model-mode') as ModelMode ?? 'auto')`
- Persist: `useEffect(() => localStorage.setItem('jarvis-model-mode', modelMode), [modelMode])`

### 2.4 Send Model Mode in confirmScheduleStream

**Fix:** Add `model_mode: modelMode` to the confirm schedule request body.

### 2.5 Math Rendering

**Status:** KaTeX is correctly configured (remark-math + rehype-katex + CSS import). If math still doesn't render:
- Check if backend LLM output uses `\\frac` (escaped) vs `\frac` (raw) — KaTeX needs raw `\frac`
- Check if SSE streaming breaks `$...$` delimiters across chunks
- Verify `$` is not being consumed by markdown processing before KaTeX

### 2.6 Draft Review UI in Chat

The chat must support the full draft negotiation loop inline:
- When `draft_id` is in response, show DraftReview component
- Accept All → `POST /drafts/{draft_id}/accept`
- Edit Task → inline edit → `PATCH /drafts/{draft_id}/tasks/{task_id}` → re-solve shows updated draft
- Reject → `POST /drafts/{draft_id}/reject` with reason
- Chat to Modify → user types modification → sends to `/drafts/{draft_id}/chat`
- Rearrange → drag or button swap → `POST /drafts/{draft_id}/rearrange`

---

## Workstream 3: Calendar/Schedule (Frontend)

### 3.1 Build Time Grid Layout (Day View)

**Problem:** Tasks render as flat card list, not positioned on time grid.

**Fix:** Replace with CSS grid:
- Left column: hour labels (8 AM through 11 PM)
- Right column: relative-positioned container
- Each task: `top = (start_min - dayStartMin) * pxPerMin`, `height = duration_minutes * pxPerMin`
- NOW indicator: horizontal red line at current time position
- Color-coded by goal (using `colorForGoal(goalId)`)

### 3.2 Add Week/Month View Toggles

**Fix:**
- `viewMode: 'day' | 'week' | 'month'` state with 3-button toggle in header
- **Day:** Time grid (3.1)
- **Week:** 7-column grid, each column a mini day view. Fetch tasks for the week.
- **Month:** Calendar grid (7 cols x 5-6 rows), each cell shows task count + color dots. Click → day view.

### 3.3 Render Blocked Windows

**Fix:**
- Blocked windows (sleep, class, meetings) render as gray hatched blocks on the time grid
- Use `constraint_applied` field or fetch behavioral_constraints separately
- CSS: diagonal stripe pattern background
- Label: constraint name
- Non-interactive

### 3.4 Add Demo Schedule Data

**Fix:** In `demoData.ts`, add sample schedule tasks:
- 6-8 tasks (9 AM to 6 PM), mix of completed/in_progress/pending
- 2-3 blocked windows (sleep 12AM-8AM, lunch 12-1PM)
- goal_id for color coding
- horizon_start for time computation
- Return from `listTasks()` when `IS_DEMO_MODE`

### 3.5 Complete/Skip Action Buttons

**Fix:**
- Checkmark icon (complete) and skip icon on each pending/in_progress task
- Complete → SM-2 quality rating dialog (0-5) → `completeTask(taskId, userId, quality)`
- Skip → `skipTask(taskId, userId)` → task grays out
- Both trigger PEARL detection on backend (fire-and-forget)
- Re-fetch tasks after mutation

### 3.6 Workspace Navigation from Schedule

Click any task on schedule → navigate to `/workspace/{taskId}`. The workspace page shows:
- Task title, duration, difficulty, progress
- Completion criteria (checkboxes)
- Practice problems (Show Solution / Mark Complete / Skip)
- Study materials (lecture notes, YouTube, articles)
- Implementation Intention (WOOP: If [obstacle] → Then [response])
- Task-scoped mini-chat with RAG context
- Progress bar: X/Y criteria complete

---

## Implementation Architecture

### Execution Order

```
Phase A (Parallel — 3 independent workstreams):
  ├── WS1: Backend model routing + intent registry + draft endpoints + PEARL
  ├── WS2: Frontend chat UI fixes (thinking names, calendar handlers, model mode)
  └── WS3: Frontend schedule rebuild (time grid, views, blocked windows, actions)

Phase B (Sequential — depends on Phase A):
  ├── Integration: send "hi" → verify Gemini Flash brain dump + 4B classify + 4B VoJ → <2s
  ├── Integration: send "plan my day" → verify full pipeline with Gemini Flash routing
  ├── Integration: schedule renders with time grid from API tasks
  └── Integration: draft review loop works (accept/edit/reject/chat/rearrange)
```

### Files to Modify

**Backend (Workstream 1):**
- `app/models/brain/litellm_conf.py` — Add `gemini_primary_route()` and `local_primary_route()` helpers
- `app/services/analytical/control_policy.py` — Use registry dispatch, fix brain dump to Gemini Flash primary, emit phase SSE events
- `app/services/intent_registry.py` — Wire real async handlers, add embedding-based classification, remove stubs
- `app/services/intent_discovery.py` — **NEW**: frequency counter, semantic clustering, IntentBlueprint generation, DYNAMIC_INTENT execution
- `app/schemas/context.py` — Add IntentBlueprint schema, memory state enum
- `app/services/analytical/habit_translator.py` — Switch to Gemini Flash primary
- `app/api/v1/endpoints/drafts.py` — Add `/rearrange` and `/chat` endpoints, fix `/reject` to build memory
- `app/api/v1/endpoints/tasks.py` — Add PEARL detection calls after complete/skip
- `app/services/memory/pearl.py` — Add `deadline_buffer` detector, rename `completion_time_preference` → `duration_preference`
- `app/services/memory/retriever.py` — Add importance factor to scoring, filter by lifecycle state
- `app/services/memory/extractor.py` — Chain PEARL detection after extraction, add contradiction detection
- `app/services/memory/store.py` — Add lifecycle state field, reinforcement resets state to active
- `app/services/chat_history.py` — Session lifecycle (get_or_create, close, summary generation)
- `app/services/draft_store.py` — Ensure Supabase-backed, remove legacy no-op aliases
- `app/services/intent_discovery.py` — **NEW**: frequency counter, clustering, IntentBlueprint generation

**Frontend Chat (Workstream 2):**
- `components/app/ThinkingProcess.tsx` — Use fun phase names from constants
- `app/(app)/chat/page.tsx` — Wire calendar approval handlers, draft review loop handlers
- `lib/hooks/useJarvisChat.ts` — Persist model mode, add to confirmScheduleStream

**Frontend Schedule (Workstream 3):**
- `app/(app)/schedule/page.tsx` — Rebuild: time grid, view toggles, blocked windows, action buttons
- `lib/demoData.ts` — Add demo schedule tasks with blocked windows
- `lib/api.ts` — Return demo tasks in demo mode

---

## Success Criteria

1. **Core loop matches flow diagram exactly:** Memory Retrieval → Brain Dump (Gemini Flash) → Intent Classify (embedding + 4B) → Registry Dispatch → Handler → Voice of Jarvis (4B) → Memory Extraction + PEARL
2. **"hi" response in <2 seconds** — Gemini Flash brain dump + 4B classify + 4B VoJ (no 27B anywhere)
3. **"plan my day" triggers full sequential pipeline** — habits → translate (Gemini) → decompose (Gemini) → fusion → OR-Tools → DRAFT
4. **Intent classification uses embedding cosine similarity** — match against registered intent descriptions, fallback to CHAT at < 0.65
5. **Intent Discovery Engine** — unmatched inputs logged, frequency counter increments, clustering runs async, IntentBlueprint generated at threshold
6. **Intent registry is single source of truth** — no if/elif cascade, adding new intent = handler + register()
7. **All 4 registries use BaseRegistry pattern** — Intent, Document Type, Memory Type, PEARL Pattern
8. **Draft negotiation loop works** — accept/edit/reject/chat/rearrange all functional, loop back to review
9. **PEARL detects all 3 patterns** — skip_time_window, duration_preference, deadline_buffer — triggered by user behavior signals
10. **Memory lifecycle follows state machine** — NewMemory → Active → Reinforced/Decaying/Archived/Superseded with exact transition rules
11. **Memory scoring includes Importance** — Score = Relevance x Recency x Importance x Confidence
12. **Contradiction detection automatic** — new memory with similarity > 0.85 but conflicting content → supersede old
13. **Chat shows fun phase names** — "Brewing your plan...", "Digesting your brain dump...", etc.
14. **Math renders** via KaTeX (inline + block)
15. **Schedule day view** with time-positioned blocks on grid
16. **Week/month views** functional with task indicators
17. **Blocked windows** render as gray hatched zones
18. **Complete/skip from schedule** with SM-2 quality rating
19. **Multi-goal fusion works smoothly** — new tasks merge with existing pending tasks atomically

---

## What's NOT In Scope

- Landing page redesign (Phase 3 stretch goal)
- Future document types (meeting_transcript, email_thread, etc. — registry is ready, handlers not needed yet)
- DKT/RL/SARIMAX (Phase 2 — requires behavioral data from Phase 1)
- L8 PII Filter (planned, not blocking)
- Mobile responsive layout
- Onboarding flow
- Analytics page
