# Jarvis Engine — Document Index

**Last updated:** 2026-03-28

Quick reference to the most important documents in this project. Start here.

---

## Architecture & Vision

| Document | What It Is | When To Read |
|----------|-----------|-------------|
| [POLICY_ENGINE_ARCHITECTURE.md](POLICY_ENGINE_ARCHITECTURE.md) | Full architecture with Mermaid diagrams | Understanding the system |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | Honest status: what works, what's broken | Before pitching or planning |
| [FUTURE_ARCHITECTURE.md](FUTURE_ARCHITECTURE.md) | Preserved Phase 2 specs (DKT, RL, SARIMAX, L8, L1) | When ready to build advanced features |

## Design Specs (Most Important First)

| Spec | What It Covers | Status |
|------|---------------|--------|
| [Architecture Reset](superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md) | **THE master spec.** Core loop, memory system, registry framework, document intelligence, draft UX, LLM routing, PEARL. Everything flows from here. | Active |

## Implementation Plans (Chronological)

### Foundation (March 4-5)
| Plan | What It Built |
|------|--------------|
| [Backend Setup](superpowers/plans/2026-03-04-jarvis-backend-setup.md) | FastAPI scaffolding, project structure |
| [LiteLLM Router](superpowers/plans/2026-03-04-litellm-hybrid-router.md) | Local/cloud LLM routing |
| [Brain Dump Extraction](superpowers/plans/2026-03-05-brain-dump-multi-intent-extraction.md) | Multi-intent extraction from natural language |
| [Control Policy](superpowers/plans/2026-03-05-control-policy-implementation.md) | Unified /chat endpoint, 5-way routing |
| [Socratic Chunker](superpowers/plans/2026-03-05-socratic-task-chunker.md) | Goal → micro-task decomposition |
| [Deterministic Scheduler](superpowers/plans/2026-03-05-phase-2-deterministic-scheduler.md) | OR-Tools CP-SAT solver |

### Features (March 5)
| Plan | What It Built |
|------|--------------|
| [Multi-Goal Fusion](superpowers/plans/2026-03-05-global-recalibration-multi-goal-fusion.md) | Multiple goals in one schedule |
| [Context Ingestion](superpowers/plans/2026-03-05-phase-3-context-ingestion.md) | Docling PDF → ChromaDB pipeline |
| [Proactive Workspace](superpowers/plans/2026-03-05-proactive-task-workspace.md) | RAG + web search + practice assets |
| [Habits & SM-2](superpowers/plans/2026-03-05-recurring-habits-temporal-intelligence.md) | Spaced repetition for habits |
| [Adaptive Pacing](superpowers/plans/2026-03-05-adaptive-pacing-intelligence.md) | Anti-guilt daily cap computation |
| [Multi-Day Safeguards](superpowers/plans/2026-03-05-multi-day-safeguards-and-thinking-process.md) | Late-night fix, biological fallback |

### UI & Polish (March 15-19)
| Plan | What It Built |
|------|--------------|
| [Demo Response UI](superpowers/plans/2026-03-15-jarvis-demo-response-layers-ui.md) | Frontend response layers |
| [Draft Review UX](superpowers/plans/2026-03-19-agentic-chat-ux-and-smart-task-management.md) | Draft accept/edit/reject flow |
| [Performance](superpowers/plans/2026-03-19-progressive-draft-review-and-performance.md) | Progressive rendering + perf |

## Research

| Document | Location |
|----------|----------|
| Jarvis AI Blueprint (Original Vision) | `Jarvis-Docs/Research /Jarvis AI Blueprint.pdf` |
| Architecture Diagram Corrections | `Jarvis-Docs/Research /Jarvis Architecture diagram correction.pdf` |

## For VC Pitch (Wednesday April 1, 2026)

**Start with:** [Architecture Reset Spec](superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md) — it has all the Mermaid diagrams.

**Key diagrams in the spec:**
1. Phase 1 Architecture (full system flow)
2. Three-Tier Memory Model (with SM-2 scoring)
3. Memory Lifecycle State Machine
4. PEARL Behavioral Pattern Detection
5. Draft Negotiation Loop
6. Document Intelligence Pipeline
7. Registry Framework
8. Phase 2 Future Architecture (DKT/RL/SARIMAX)
9. Document-Task Integration Day-by-Day Scenario
10. Framework-Based Pipeline Flow

**Pitch narrative:** See "Painkiller Thesis" section in the spec.
