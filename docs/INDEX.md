# Jarvis Engine — Document Index

**Last updated:** 2026-08-08

Quick reference to all important documents. Start here.

---

## Architecture & Vision

| Document | What It Is | When To Read |
|----------|-----------|-------------|
| [PITCH_ARCHITECTURE.md](PITCH_ARCHITECTURE.md) | 10 Mermaid diagrams for pitch + moat comparison | VC pitch, demos, explaining the system |
| [POLICY_ENGINE_ARCHITECTURE.md](POLICY_ENGINE_ARCHITECTURE.md) | **v1 pipeline — deprecated 2026-08-08.** Kept for historical reference | Understanding how the pre-orchestrator system worked |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | **Start here.** Honest status: the live v2 architecture, current Mermaid diagrams, verified test counts, known issues, storage map | Before pitching, planning, or touching code |
| [FUTURE_ARCHITECTURE.md](FUTURE_ARCHITECTURE.md) | Preserved Phase 2 specs (DKT, RL, SARIMAX, L8, L1) | When ready to build advanced features |
| [NST-Startup-Foundry-2026-Application.md](NST-Startup-Foundry-2026-Application.md) | Startup foundry application | Reference |

## Design Specs

All specs live in `superpowers/specs/`. Newest first.

| Spec | What It Covers | Status |
|------|---------------|--------|
| [Architecture v2](superpowers/specs/2026-04-12-jarvis-architecture-v2-design.md) | **THE master spec.** LangGraph orchestrator, User Model at the centre, cognitive modules, observation loop, negotiation phases. Supersedes the 2026-03-28 reset spec. Everything current flows from this. | **Active — implemented** |
| [One Brain: Stabilization + v1/v2 Unification](superpowers/specs/2026-08-08-one-brain-stabilization-unification-design.md) | Store resilience, model detection, serializable state + checkpointer, planning→`run_schedule` delegation, ModuleStep barrier fix, draft accept/reject/edit, real token streaming, v1 deprecation. Supersedes the P0 ordering in the 05-01 roadmap. | Implemented 2026-08-08 |
| [Generative UI](superpowers/specs/2026-08-08-generative-ui-design.md) | Claude-style inline rendering, three tiers. Depends on the stabilized v2 SSE path. | Approved — not started |
| [Post-Spine Roadmap](superpowers/specs/2026-05-01-post-spine-roadmap.md) | What was left after the May 1 spine session. P0 ordering superseded by the 2026-08-08 spec. | Reference |
| [ModuleStep Framework](superpowers/specs/2026-04-13-module-step-framework-design.md) | Declarative `ModuleStep` / `ModuleDefinition` replacing hardcoded `build_*_graph()`. This is what got built instead of the 04-05 adaptation spec. | **Active — implemented** |
| [Intelligent Phase Progress](superpowers/specs/2026-04-13-intelligent-phase-progress-design.md) | SSE phase/tool_use events, spinner verbs, learning moments — making the system *feel* intelligent. | Approved — partially implemented |
| [Psychology Framework Completion](superpowers/specs/2026-04-13-psychology-framework-completion-design.md) | TMT, WOOP/MCII, CLT, self-efficacy, anti-guilt as the competitive moat. | Draft |
| [Claude Code Architecture Adaptation](superpowers/specs/2026-04-05-claude-code-architecture-adaptation-design.md) | Full Claude-Code-style tool/hook/agent architecture. | **Superseded** by the ModuleStep framework |
| [Core Loop Realignment](superpowers/specs/2026-03-31-core-loop-realignment-design.md) | Realigned 4 drift points against the reset spec. | Historical |
| [Spec Compliance Fix](superpowers/specs/2026-03-30-jarvis-spec-compliance-fix-design.md) | Backend + frontend alignment to the reset spec. | Historical |
| [Demo Frontend Update](superpowers/specs/2026-03-29-demo-frontend-update-design.md) | jarvis-demo frontend showcase of Phase 1 backend. | Historical |
| [Architecture Reset](superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md) | Core loop, 3-tier memory (SM-2 decay), registry framework, document intelligence, draft negotiation UX, PEARL, LLM routing, session management. | Superseded by Architecture v2 |

## Implementation Plans

### Phase 0: Backend Foundation (March 4)

| Plan | What It Built | Status |
|------|--------------|--------|
| [Backend Setup](superpowers/plans/2026-03-04-jarvis-backend-setup.md) | FastAPI scaffolding, project structure, Supabase connection | Done |
| [LiteLLM Hybrid Router](superpowers/plans/2026-03-04-litellm-hybrid-router.md) | Local/cloud LLM routing via LiteLLM | Done |

### Phase 1: Core Intelligence (March 5)

| Plan | What It Built | Status |
|------|--------------|--------|
| [Brain Dump Multi-Intent Extraction](superpowers/plans/2026-03-05-brain-dump-multi-intent-extraction.md) | Multi-intent extraction from natural language, Voice of Jarvis | Done |
| [Control Policy](superpowers/plans/2026-03-05-control-policy-implementation.md) | Unified /chat endpoint, 5-way intent routing | Done |
| [Socratic Task Chunker](superpowers/plans/2026-03-05-socratic-task-chunker.md) | Goal to micro-task decomposition (25-min max, WOOP) | Done |
| [Deterministic Scheduler](superpowers/plans/2026-03-05-phase-2-deterministic-scheduler.md) | OR-Tools CP-SAT solver, hard/soft blocks, dependencies | Done |

### Phase 2: Scheduling Intelligence (March 5)

| Plan | What It Built | Status |
|------|--------------|--------|
| [Global Recalibration & Multi-Goal Fusion](superpowers/plans/2026-03-05-global-recalibration-multi-goal-fusion.md) | Multiple goals in one schedule, namespace fusion | Done |
| [Global Recalibration Architecture Update](superpowers/plans/2026-03-05-global-recalibration-and-architecture-update.md) | Architecture update for global recalibration | Done |
| [Adaptive Pacing Intelligence](superpowers/plans/2026-03-05-adaptive-pacing-intelligence.md) | Anti-guilt daily cap, slack ratio computation | Done |
| [Pacing TMT Deadline Improvements](superpowers/plans/2026-03-05-pacing-tmt-deadline-improvements.md) | Temporal Motivation Theory priority weights | Done |
| [Deadline-Based Horizon](superpowers/plans/2026-03-05-deadline-based-horizon.md) | Horizon computation from task deadlines | Done |
| [Multi-Day Safeguards](superpowers/plans/2026-03-05-multi-day-safeguards-and-thinking-process.md) | Late-night fix, biological fallback, thinking_process | Done |

### Phase 3: Knowledge & Ingestion (March 5)

| Plan | What It Built | Status |
|------|--------------|--------|
| [Context Ingestion](superpowers/plans/2026-03-05-phase-3-context-ingestion.md) | Docling PDF to ChromaDB pipeline, task-material linking | Done |
| [Autonomous Extraction Pipeline](superpowers/plans/2026-03-05-autonomous-extraction-pipeline.md) | Autonomous content extraction | Done |
| [Multi-Source Deadlines & Ingestion Fusion](superpowers/plans/2026-03-05-multi-source-deadlines-and-ingestion-fusion.md) | Multi-source deadline handling | Done |
| [Proactive Task Workspace](superpowers/plans/2026-03-05-proactive-task-workspace.md) | RAG + web search + practice assets | Done |

### Phase 4: Habits & Behavioral (March 5)

| Plan | What It Built | Status |
|------|--------------|--------|
| [Recurring Habits & Temporal Intelligence](superpowers/plans/2026-03-05-recurring-habits-temporal-intelligence.md) | Spaced repetition (SM-2), habit scheduling | Done |
| [Habit Deletion & Provenance](superpowers/plans/2026-03-05-habit-deletion-and-provenance.md) | Habit lifecycle management | Done |
| [Habit Application & Intent Collision Fix](superpowers/plans/2026-03-05-habit-application-and-intent-collision-fix.md) | Fix intent collisions in habit flow | Done |
| [SARIMAX Engine Response Fix](superpowers/plans/2026-03-05-sarimax-engine-response-fix.md) | SARIMAX model response handling | Done |

### Phase 5: UI & UX (March 15-19)

| Plan | What It Built | Status |
|------|--------------|--------|
| [Demo Response Layers UI](superpowers/plans/2026-03-15-jarvis-demo-response-layers-ui.md) | Frontend response layers | Done |
| [Thinking Display & LM Studio Fix](superpowers/plans/2026-03-15-thinking-display-and-lmstudio-fix.md) | Thinking process display, LM Studio integration fix | Done |
| [March 17 Status Summary](superpowers/plans/2026-03-17-march17-so-far.md) | Status checkpoint | Reference |
| [Agentic Chat UX & Smart Task Management](superpowers/plans/2026-03-19-agentic-chat-ux-and-smart-task-management.md) | Agentic chat, smart task management | Done |
| [Progressive Draft Review & Performance](superpowers/plans/2026-03-19-progressive-draft-review-and-performance.md) | Draft accept/edit/reject, SSE streaming, performance | Done |

### Phase 6: Architecture Reset (March 28–29)

| Plan | What It Covers | Status |
|------|---------------|--------|
| [Architecture Reset Spec](superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md) | Superseded by Architecture v2 — see Design Specs above | Historical |
| [Phase 1A: Foundation](superpowers/plans/2026-03-29-architecture-reset-phase-1a-foundation.md) | BaseRegistry, IntentRegistry, DraftStore migration | Done |
| [Phase 1B: Memory & Context](superpowers/plans/2026-03-29-architecture-reset-phase-1b-memory-context.md) | 3-tier memory, SM-2 decay, Memory → Constraint Bridge | Done |
| [Phase 1C: Document Intelligence](superpowers/plans/2026-03-29-architecture-reset-phase-1c-document-intelligence.md) | Document type registry, classification pipeline | Done |
| [Phase 1D: Behavioral Intelligence](superpowers/plans/2026-03-29-architecture-reset-phase-1d-behavioral-intelligence.md) | PEARL pattern detection, behavioral insights | Done |
| [Phase 1E: Stabilize & Document](superpowers/plans/2026-03-29-architecture-reset-phase-1e-stabilize-document.md) | FUTURE_ARCHITECTURE.md, PROJECT_STATUS.md rewrite, INDEX.md | Done |

### Phase 7: v2 Orchestrator (April 12 — August 8, Current)

| Plan | What It Built | Status |
|------|--------------|--------|
| [Architecture v2](superpowers/plans/2026-04-12-jarvis-architecture-v2.md) | LangGraph orchestrator, JarvisState, module sub-graphs, hooks, observation loop | Done |
| [v2 Production Wiring](superpowers/plans/2026-04-12-v2-production-wiring.md) | `/chat/v2/stream`, SSE contract, module registry at lifespan startup | Done |
| [ModuleStep Framework](superpowers/plans/2026-04-13-module-step-framework.md) | Declarative steps → compiled sub-graphs, replacing `build_*_graph()` | Done |
| [Intelligent Phase Progress](superpowers/plans/2026-04-13-intelligent-phase-progress.md) | `tool_use` / phase SSE events, spinner verbs | Partial |
| [Psychology Framework Completion](superpowers/plans/2026-04-13-psychology-framework-completion.md) | WOOP/MCII, Bandura coaching, anti-guilt language | Partial |
| [One Brain: Stabilization + Unification](superpowers/plans/2026-08-08-one-brain-stabilization-unification.md) | Store resilience, model detection, green offline suite, Supabase + migrations, checkpointer, planning delegation + drafts, ModuleStep barrier fix, draft actions, real token streaming, v1 deprecation, this doc pass | Done |

## Research Documents

| Document | Location | Content |
|----------|----------|---------|
| Jarvis AI Blueprint (Original Vision) | `Jarvis-Docs/Research /Jarvis AI Blueprint.pdf` | Technical + psychological architecture, 9-level Agentic RAG stack |
| Architecture Diagram Corrections | `Jarvis-Docs/Research /Jarvis Architecture diagram correction.pdf` | Corrections to original architecture |
| Building Jarvis AI Backend | `Jarvis-Docs/Research /Building Jarvis AI Productivity Backend.pdf` | Detailed backend design |
| Jarvis AI Day 1 Behavior Architecture | `Jarvis-Docs/Research /Jarvis AI Day 1 Behavior Architecture.pdf` | Cold start, onboarding |
| AI Productivity Engine Specifications | `Jarvis-Docs/Research /AI Productivity Engine Specifications.pdf` | Full product specifications |
| Jarvis AI Business Plan | `Jarvis-Docs/Research /Jarvis AI Business Plan.pdf` | Business model, freemium pricing |
| Recurring Habits Scheduling Logic | `Jarvis-Docs/Research /Recurring Habits Scheduling Logic.pdf` | SM-2 integration design |
| Jarvis Google Gemini Reference | `Jarvis-Docs/Research /Jarvis Google Gemini - LLM Reference.pdf` | LLM routing reference |

## For Pitching

**Use:** [PITCH_ARCHITECTURE.md](PITCH_ARCHITECTURE.md) — 10 diagrams, all in `stateDiagram-v2` / flowchart style, with talking points and moat comparison. Note that it predates the v2 orchestrator; cross-check any architectural claim against [PROJECT_STATUS.md](PROJECT_STATUS.md), which carries the current diagrams.

**3-slide minimum:**
1. Diagram 1: Core Loop — "What it does"
2. Diagram 3: Memory-to-Constraint Bridge — "Why it's different"
3. Diagram 10: Platform Roadmap — "Where it goes"

**7-slide version:** Add Memory Lifecycle, Draft Negotiation, Document Intelligence, Day-by-Day Scenario.

**Export:** Copy Mermaid code to [mermaid.live](https://mermaid.live), export SVG/PNG, drop into slides.
