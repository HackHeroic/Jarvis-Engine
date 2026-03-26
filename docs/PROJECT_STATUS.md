# Jarvis Engine — Project Status

**Last Updated:** 2026-03-17
**Author:** Architecture Review (Claude + Madhav)

---

## Current State: Backend Complete, Feedback Loop Missing

The Jarvis Engine backend has all core pipelines implemented and functional. The jarvis-demo frontend connects to the backend via SSE streaming. The system can take a brain dump, extract intents, decompose goals, translate habits, solve schedules via OR-Tools, ingest documents, and synthesize responses.

**What works end-to-end today:**
- Brain dump → intent extraction → plan-day pipeline → OR-Tools schedule → Voice of Jarvis response
- Multi-goal fusion across sessions (pending tasks + new goal merged)
- Habit translation (natural language → semantic time slots → solver constraints)
- Document ingestion (PDF → Docling → ChromaDB chunks → task-material linking)
- SSE streaming to frontend (phase updates, thinking tokens, message tokens)
- Confirm flow (decompose → user reviews tasks → confirm → schedule)

**What does NOT work yet:**
- Users cannot mark tasks as completed (no endpoint)
- Nothing triggers replanning autonomously (no event bus, no background tasks)
- No feedback loop exists (no completion data → no DKT → no RL → no adaptation)
- No external integrations (no Slack/email/MCP)

---

## Implementation Status Matrix

### Fully Implemented

| Component | File(s) | What It Does |
|-----------|---------|-------------|
| **FastAPI Server** | `app/main.py` | CORS, lifespan, health, async throughout |
| **Control Policy** | `app/services/analytical/control_policy.py` | Master orchestrator: brain dump → 5-way routing → plan-day or ingestion |
| **Brain Dump Extractor** | `control_policy.py` (4B SLM prompt) | Extracts: planning_goal, inline_habits, state_updates, action_items, search_queries, calendar_text, knowledge flags, deadlines |
| **LiteLLM Hybrid Router** | `app/models/brain/litellm_conf.py` | Local-first (Qwen 27B/4B), Gemini cloud for real-time research + fallback |
| **Socratic Chunker** | `control_policy.py` → `hybrid_route_query` | Goal → 5+ TaskChunks (25-min ceiling, WOOP, CLT, completion criteria) |
| **OR-Tools CP-SAT** | `app/core/or_tools/solver.py` | Hard/soft blocks, AddNoOverlap, dependencies, TMT priority, daily cap |
| **Adaptive Pacing** | `app/utils/pacing.py` | slack-ratio tiers (90/120/180/240 min/day), cognitive load adjustment |
| **Multi-Goal Fusion** | `control_policy.py` + `task_retrieval.py` | Namespace `goal_id_task_id`, merge pending + new, single schedule |
| **Habit Translator** | `app/services/analytical/habit_translator.py` | Natural language → SemanticTimeSlots (27B) |
| **Horizon Expander** | `app/services/analytical/horizon_expander.py` | Semantic slots × recurrence → concrete TimeSlots across days |
| **Biological Fallback** | `horizon_expander.py` | No sleep habit → inject Midnight–8 AM block |
| **Late-Night Fix** | `control_policy.py` | hour < DAY_START(8) → logical day = yesterday |
| **Horizon Retry** | `control_policy.py` | INFEASIBLE → retry 48h → 72h → 5d → 10d → 20d → 30d |
| **Calendar Extractor** | `app/services/extraction/calendar_extractor.py` | Timetable → TimeSlots, pending approval flow |
| **Knowledge Ingester** | `app/services/extraction/knowledge_ingester.py` | Docling → chunk → ChromaDB, proactive extraction (topics, deadlines, actions) |
| **Task-Material Linker** | `app/services/extraction/task_material_linker.py` | Embedding cosine similarity ≥ 0.65 → link doc to matching tasks |
| **Behavioral Store** | `app/services/extraction/behavioral_store.py` | CRUD for behavioral_constraints (Strategy Hub L7) |
| **Action Item Handler** | `app/services/extraction/action_item_handler.py` | Propose action items for user approval |
| **SM-2 Spaced Repetition** | `app/services/analytical/sm2_engine.py` | EF calculation, next_interval, habit_trackers table |
| **Workspace Builder** | `app/services/analytical/workspace_builder.py` | RAG chunks + web search + practice assets (asyncio.gather) |
| **Voice of Jarvis** | `app/services/analytical/voice_of_jarvis.py` | 4B synthesis, `<think>` block extraction, warm messaging |
| **SSE Streaming** | `app/api/v1/endpoints/chat.py` | `/chat/stream`, `/chat/confirm-schedule` with phase/thinking/message events |
| **Ingestion Pipeline** | `app/services/extraction/orchestrator.py` | Classify → route (calendar/knowledge/habit/action) → extract → persist |

### Stubs (Files exist, not implemented)

| Component | File | What It Should Do |
|-----------|------|-------------------|
| **DKT** | `app/models/analytical/dkt_lstm.py` | LSTM tracking P(mastery \| KC) from task completions |
| **RL** | `app/models/analytical/dqn_rl.py` | DQN optimal task ordering from DKT mastery + task graph |
| **SARIMAX** | `app/models/forecast/capacity_ts.py` | Predict cognitive energy for next horizon |
| **WOOP** | `app/core/psychology/woop.py` | Richer implementation intention generation |
| **Telemetry** | `app/api/v1/endpoints/telemetry.py` | Signal collection (completion, focus, mood) |
| **Analytical API** | `app/api/v1/endpoints/analytical.py` | DKT mastery queries, RL endpoints |

### Not Yet Built

| Component | Priority | Why It Matters |
|-----------|----------|---------------|
| **Task Completion Endpoint** | P0 | Without this, no feedback loop exists at all |
| **Task Mutation Endpoints** | P0 | Users can't edit/delete/skip tasks post-scheduling |
| **Internal Replan Trigger** | P0 | System can't autonomously reschedule |
| **In-Process Event Bus** | P1 | Decouples "something changed" from "therefore replan" |
| **SSE Notification Channel** | P1 | Server → client push for background schedule updates |
| **Signals/Telemetry API** | P1 | Input pipeline for DKT/RL, completion rate tracking |
| **Morning Auto-Plan** | P2 | Proactive daily schedule from pending tasks |
| **End-of-Day Rollover** | P2 | Incomplete tasks → tomorrow |
| **MCP Integration** | P2 | Slack/email/external calendar sync |
| **L8 PII Filter** | P2 | Privacy gateway before cloud LLM calls |
| **L1 Evaluation** | P2 | User feedback → reward signals |

---

## Architecture Verification

### What's Correct

1. **Separation of Analytical (LLM) from Deterministic (OR-Tools)** — This is textbook correct. LLMs handle semantic understanding; CP-SAT handles mathematical optimization. They don't mix.

2. **Local-first with cloud fallback** — All structured tasks go to Qwen first. Gemini only for real-time research or last-resort retry. This preserves privacy and reduces latency.

3. **Sequential 27B execution** — Never running two 27B calls concurrently prevents OOM on 24GB M4 Pro. The codebase enforces this consistently.

4. **Multi-goal fusion with namespacing** — `goal_id_task_id` prevents collisions when merging pending tasks across goals. Dependencies are also prefixed.

5. **Anti-guilt INFEASIBLE handling** — 422 returns recalibration guidance, not error. Horizon retry expands automatically. This is psychologically correct.

6. **Brain dump multi-intent extraction** — Single prompt → structured extraction of 8 fields → parallel execution. This is more efficient than multi-turn classification.

7. **Proactive document intelligence** — PDF drops → auto-extract topics/actions/deadlines → link to existing tasks. This bridges ingestion and planning.

### What Needs Architectural Improvement

1. **Request/Response Only Architecture** — Everything starts at `POST /api/v1/chat`. There is no way for the system to initiate actions. This is the single biggest architectural gap.

2. **No Task State Machine** — Tasks have a `status` field but no defined transitions (pending → scheduled → in_progress → completed/skipped/rolled_forward) and no endpoint to drive those transitions.

3. **No Event-Driven Behavior** — When a document is ingested and linked to a task, nothing happens. When a constraint changes, nothing happens. The system says "suggested_action: replan" and waits.

4. **DKT/RL are pure stubs** — TMT priority works as a stopgap, but the planned DKT → RL pipeline (mastery tracking → optimal ordering) has zero implementation.

---

## Replan Triggers (Complete List)

### Currently Functional
| # | Trigger | How It Works |
|---|---------|-------------|
| 1 | User says "plan my day" / "reschedule" | PLAN_DAY intent → full pipeline |
| 2 | User submits new goal while tasks pending | Multi-goal fusion merges |

### Partially Working (manual follow-up required)
| # | Trigger | Gap |
|---|---------|-----|
| 3 | User adds habit/constraint | Saved, but returns `suggested_action: replan` — user must manually chat again |
| 4 | User uploads PDF affecting tasks | Linked to tasks, but no auto replan of task duration/difficulty |
| 5 | User says "exam moved to March 25" | `deadline_update` persisted, but doesn't trigger schedule recalculation |
| 6 | User says "I'm exhausted" | `state_updates` captured as temporary context, not a replan trigger |

### Not Implemented At All
| # | Trigger | What's Needed |
|---|---------|--------------|
| 7 | Task completed | Task completion endpoint + replan of remaining tasks |
| 8 | Task scope change (edit/delete/skip) | Task mutation endpoints |
| 9 | Morning auto-plan | Background cron at DAY_START_HOUR |
| 10 | End-of-day rollover | Background cron; incomplete → tomorrow |
| 11 | Elapsed-time ghosting (task overdue) | Timer detects stale pending tasks → auto-shift |
| 12 | SM-2 due injection | Due habits auto-added to today's schedule |
| 13 | External message (Slack/email) | MCP integration |
| 14 | Completion rate deviation | Telemetry aggregation |
| 15 | Burnout signal (consecutive low scores) | Telemetry aggregation → lighter schedule |
| 16 | Calendar anchor resolved | `user_calendar_anchors` → horizon recomputation |
| 17 | Goal deleted/cancelled | Delete goal tasks → compress schedule |
| 18 | SARIMAX capacity shift | Energy prediction → adjust daily cap |

---

## Frontend Status (jarvis-demo)

**Framework:** Next.js 14, Tailwind CSS, TypeScript
**Communication:** SSE streaming (not WebSockets)
**State:** Functional demo with live + mock modes

### Pages Implemented
- `/` — Landing page
- `/chat` — Main chat with file upload, SSE streaming, thinking process display
- `/schedule` — FullCalendar day/week/month views
- `/workspace/[taskId]` — Task-focused RAG workspace
- `/documents` — Document management
- `/habits` — Behavioral constraints
- `/architecture` — Mermaid architecture diagrams

### Frontend Gaps
- No task completion button ("Done" / "Skip")
- No notification system for background updates
- No task editing UI post-scheduling
- No progress tracking visualization
- No SM-2 habit rating UI integrated into schedule

---

## Database Schema (Supabase)

| Table | Status | Purpose |
|-------|--------|---------|
| `user_tasks` | Active | Task decomposition (task_id, goal_id, title, status, duration, dependencies, deadline_hint) |
| `behavioral_constraints` | Active | Habits/preferences (raw_text, constraint_type, recurrence, structured_semantics JSONB) |
| `user_plan_updates` | Active | Goal-level deadlines |
| `pending_calendar_updates` | Active | Extracted timetables awaiting approval |
| `ingested_documents` | Active | Document metadata (source_id, topics, chunk_count) |
| `task_materials` | Active | Document-to-task links (source_id → ChromaDB) |
| `habit_trackers` | Active | SM-2 state (EF, repetitions, next_interval) |
| `pending_action_items` | Active | Action proposals awaiting user choice |
| `user_preferences` | Active | Learning style (watcher/reader/interactive) |
| `user_calendar_anchors` | Created, unused | Date placeholder resolution (e.g., "finals" → 2026-06-15) |

---

## Roadmap (Next 4 Weeks)

### Week 1: Close the Feedback Loop (P0)
- Build `POST /api/v1/tasks/{task_id}/complete` + PATCH + DELETE
- Build `trigger_replan(user_id, reason)` internal function
- Test: plan → complete task → verify pending state updated

### Week 2: Event Bus + Notifications (P1)
- `app/core/event_bus.py` — in-process asyncio event dispatcher
- Wire publishers: task completion, constraint change, document ingestion
- `GET /api/v1/events/stream` — SSE notification channel
- Background replan handler
- Frontend: notification toast + schedule refresh

### Week 3: Signals + Telemetry (P1)
- `POST /api/v1/telemetry/signal` — task completion, focus, mood
- KC taxonomy mapper (task title → KC_tag via embedding)
- Update `POLICY_ENGINE_ARCHITECTURE.md` with all new components

### Week 4: DKT Foundation
- LSTM model in `dkt_lstm.py`
- Training pipeline on accumulated completion data
- Wire DKT mastery → `difficulty_weight` override in plan-day flow

---

## Key Thresholds & Constants

| Constant | Value | Location |
|----------|-------|----------|
| `DAY_START_HOUR` | 8 | `app/core/config.py` |
| `DEFAULT_HORIZON_MINUTES` | 2880 (48h) | `app/core/config.py` |
| `MAX_HORIZON_MINUTES` | 43200 (30d) | `app/core/config.py` |
| `SIMILARITY_THRESHOLD` | 0.65 | `task_material_linker.py` |
| SM-2 EF floor | 1.3 | `sm2_engine.py` |
| Task duration ceiling | 25 min | Socratic Chunker prompt |
| Adaptive pacing tiers | 90/120/180/240 min/day | `pacing.py` |
| Cognitive load safety | intrinsic >= 0.8 → cap * 0.8 | `pacing.py` |

---

## Decision Log

| Decision | Rationale |
|----------|-----------|
| SSE over WebSockets | Frontend already handles SSE; WebSockets adds complexity without benefit for one-way push |
| In-process event bus over Redis | Single-machine local-first; external MQ is overkill |
| DKT before RL | RL state representation depends on DKT mastery scores |
| Keep jarvis-demo, don't restart frontend | Functional Next.js 14 app with SSE; rewriting wastes 2-4 weeks |
| Sequential 27B calls | 24GB M4 Pro OOMs with concurrent 27B; strictly enforced |
| TMT as RL stopgap | `(Expectancy * Value) / (Impulsiveness * Delay)` works until RL is ready |
