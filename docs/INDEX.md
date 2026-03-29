# Jarvis Engine — Document Index

**Last updated:** 2026-03-28

Quick reference to all important documents. Start here.

---

## Architecture & Vision

| Document | What It Is | When To Read |
|----------|-----------|-------------|
| [PITCH_ARCHITECTURE.md](PITCH_ARCHITECTURE.md) | 10 Mermaid diagrams for pitch + moat comparison | VC pitch, demos, explaining the system |
| [POLICY_ENGINE_ARCHITECTURE.md](POLICY_ENGINE_ARCHITECTURE.md) | Full current architecture with Mermaid diagrams | Understanding the system in depth |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | Honest status: what works, what's broken | Before pitching or planning |
| [FUTURE_ARCHITECTURE.md](FUTURE_ARCHITECTURE.md) | Preserved Phase 2 specs (DKT, RL, SARIMAX, L8, L1) | When ready to build advanced features |
| [NST-Startup-Foundry-2026-Application.md](NST-Startup-Foundry-2026-Application.md) | Startup foundry application | Reference |

## Design Specs

| Spec | What It Covers | Status |
|------|---------------|--------|
| [Architecture Reset](superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md) | **THE master spec.** Core loop, 3-tier memory (SM-2 decay), registry framework, document intelligence, draft negotiation UX, PEARL behavioral inference, LLM routing, session management, migration strategy. All other work flows from this. | Active |

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

### Phase 6: Architecture Reset (March 28 — Current)

| Plan | What It Covers | Status |
|------|---------------|--------|
| [Architecture Reset Spec](superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md) | Master spec — see Design Specs above | Active |
| [Phase 1A: Foundation](superpowers/plans/2026-03-29-architecture-reset-phase-1a-foundation.md) | BaseRegistry, IntentRegistry, DraftStore migration | Done |
| [Phase 1B: Memory & Context](superpowers/plans/2026-03-29-architecture-reset-phase-1b-memory-context.md) | 3-tier memory, SM-2 decay, Memory → Constraint Bridge | Done |
| [Phase 1C: Document Intelligence](superpowers/plans/2026-03-29-architecture-reset-phase-1c-document-intelligence.md) | Document type registry, classification pipeline | Done |
| [Phase 1D: Behavioral Intelligence](superpowers/plans/2026-03-29-architecture-reset-phase-1d-behavioral-intelligence.md) | PEARL pattern detection, behavioral insights | Done |
| [Phase 1E: Stabilize & Document](superpowers/plans/2026-03-29-architecture-reset-phase-1e-stabilize-document.md) | FUTURE_ARCHITECTURE.md, PROJECT_STATUS.md rewrite, INDEX.md | Done |

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

## For VC Pitch (Wednesday April 1, 2026)

**Use:** [PITCH_ARCHITECTURE.md](PITCH_ARCHITECTURE.md) — 10 diagrams, all in `stateDiagram-v2` / flowchart style, with talking points and moat comparison.

**3-slide minimum:**
1. Diagram 1: Core Loop — "What it does"
2. Diagram 3: Memory-to-Constraint Bridge — "Why it's different"
3. Diagram 10: Platform Roadmap — "Where it goes"

**7-slide version:** Add Memory Lifecycle, Draft Negotiation, Document Intelligence, Day-by-Day Scenario.

**Export:** Copy Mermaid code to [mermaid.live](https://mermaid.live), export SVG/PNG, drop into slides.
