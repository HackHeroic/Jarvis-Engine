# Architecture Reset Phase 1E: Stabilize & Document

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update all project documentation to honestly reflect the current state after the architecture reset — rewrite POLICY_ENGINE_ARCHITECTURE.md, create FUTURE_ARCHITECTURE.md (preserving DKT/RL/SARIMAX specs), update PROJECT_STATUS.md, and update CLAUDE.md. No false claims, no stale references.

**Architecture:** Documentation-only phase. No new code. All 113 tests must continue to pass. The key principle: documentation must match reality. Every "Implemented" claim must have corresponding code. Every "Planned" feature must have a clear location in FUTURE_ARCHITECTURE.md.

**Tech Stack:** Markdown, Mermaid diagrams

**Spec:** `docs/superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md` (sections: Documentation Updates, FUTURE_ARCHITECTURE.md Structure, What's Cut)

**Prerequisite:** Phase 1A-1D complete (all implementation done)

**Produces:** Honest, accurate documentation that matches the codebase. A VC or engineer can read these docs and understand exactly what works, what's planned, and where to find everything.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `docs/FUTURE_ARCHITECTURE.md` | Preserved specs for DKT, RL, SARIMAX, L8 PII, L1 Eval, Signals API |
| Rewrite | `docs/PROJECT_STATUS.md` | Honest status: what works, what's broken, what's cut, what's next |
| Update | `docs/INDEX.md` | Add FUTURE_ARCHITECTURE.md link, verify all references |
| Run | Full test suite | Verify 113 tests still pass (sanity check) |

---

### Task 1: Create FUTURE_ARCHITECTURE.md

**Files:**
- Create: `docs/FUTURE_ARCHITECTURE.md`

This document preserves the full specifications for components that were designed during the architecture phase but deferred until user behavioral data exists.

- [ ] **Step 1: Create FUTURE_ARCHITECTURE.md**

Write the file at `docs/FUTURE_ARCHITECTURE.md`. The content must include:

1. **Phase 2 Architecture Diagram** — the full Mermaid diagram from the spec showing DKT → RL → CP-SAT pipeline, L8 PII gateway, L1 Evaluation feedback loop, Signals API. Copy from the spec section "FUTURE_ARCHITECTURE.md — Phase 2 Diagram".

2. **DKT (Deep Knowledge Tracing)** — LSTM math `y_t = σ(W_yh·h_t + b_y)`, input format `x_t = {q_t, a_t}`, KC mastery probability, training data schema, integration point (feeds difficulty_weight to TaskChunk). Prerequisites: 100+ task completion events per user.

3. **RL (Reinforcement Learning / DQN)** — State space (chapters_remaining, time_until_deadline, energy_cycle), reward function (+1 completion, -100 burnout), policy `π(a|s)`, integration point (replaces TMT priority in CP-SAT). Prerequisites: DKT producing reliable mastery scores.

4. **SARIMAX (Cognitive Energy Forecasting)** — Seasonality params (S=24 hourly, S=7 weekly, S=365 annual), exogenous variables (time-tracking, completion rates, mood), integration point (feeds compute_adaptive_daily_cap). Prerequisites: 4+ weeks continuous usage data.

5. **L8 PII Filter** — Anonymization strategy, consistent placeholders per PII type, Guardrails AI or regex-based, gateway before cloud LLM calls. Prerequisites: when sending significant user content to cloud.

6. **L1 Evaluation** — Feedback signal design, user rates quality 0-5, completion/skip/modify events as reward signals, Ragas/DeepEval metrics. Prerequisites: core loop stable.

7. **Signals API** — `POST /api/v1/telemetry/signal`, time/focus/mood inputs, integration with RL reward + DKT user profile. Prerequisites: DKT + RL implemented.

For each section, include:
- What it does (1-2 sentences)
- The math / technical specification
- Integration points (which existing module it connects to)
- Prerequisites (when to build it — specific data requirements)
- The stub file location in the codebase (if applicable)

Read the full spec (particularly lines 35-53 for the "What's Cut" table, and lines 2196-2249 for the structure outline) and the research PDFs referenced in the spec to get the math details. Also read the existing stub files:
- `app/models/analytical/dkt_lstm.py`
- `app/models/analytical/dqn_rl.py`
- `app/models/forecast/capacity_ts.py`

And the CLAUDE.md sections on "Analytical Engine Design (Planned)" for the full DKT/RL/SARIMAX specifications.

- [ ] **Step 2: Verify links work**

Run: `ls docs/FUTURE_ARCHITECTURE.md` — file exists

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add docs/FUTURE_ARCHITECTURE.md
git commit -m "docs: create FUTURE_ARCHITECTURE.md — preserved DKT/RL/SARIMAX/L8/L1/Signals specs"
```

---

### Task 2: Rewrite PROJECT_STATUS.md

**Files:**
- Rewrite: `docs/PROJECT_STATUS.md`

The current PROJECT_STATUS.md (last updated 2026-03-17) says "Users cannot mark tasks as completed (no endpoint)" — but task completion was added. It's stale. Rewrite it honestly.

- [ ] **Step 1: Read the current PROJECT_STATUS.md**

Read the full file to understand the current structure and claims.

- [ ] **Step 2: Rewrite PROJECT_STATUS.md**

The new version must include:

**Section 1: Current State** — One paragraph summary. After the architecture reset, the system has: BaseRegistry framework, Gemini-primary LLM routing, Supabase-backed drafts, 3-tier memory system (SM-2 decay), PEARL behavioral pattern detection, document intelligence pipeline, and intent registry. 113 tests pass.

**Section 2: What Works End-to-End**
- Brain dump → intent extraction → plan-day pipeline → OR-Tools schedule → Voice of Jarvis
- Multi-goal fusion (pending tasks + new goal merged)
- Habit translation (natural language → semantic time slots → solver constraints)
- Document ingestion (PDF → Docling → ChromaDB chunks → task-material linking)
- Task lifecycle (complete, skip, update, delete → triggers background replan)
- SSE streaming to frontend
- Draft review (accept/reject/edit)

**Section 3: New in Architecture Reset (Phase 1A-1D)**
- BaseRegistry framework — extensible intents, documents, PEARL patterns
- Intent Registry — 9 registered intents with handler lookup
- Document Type Registry — 5 types (practice_problems, lecture_notes, syllabus, assignment, reference)
- Document Intelligence Pipeline — classify → dispatch → store memory
- 3-tier Memory System — working (session), recall (summaries), archival (user_memories)
- SM-2 Memory Decay — memories fade if not reinforced
- Memory Extraction — LLM extracts facts/preferences from conversations (fire-and-forget)
- Contradiction Detection — old memories superseded, not deleted
- Memory → Constraint Bridge — memories change OR-Tools math (the moat)
- PEARL Behavioral Intelligence — detects skip/completion patterns, generates insights
- Gemini-primary LLM routing — reliable JSON for schema-critical tasks
- DraftStore Supabase migration — drafts survive server restarts

**Section 4: What's Deferred (See FUTURE_ARCHITECTURE.md)**
- DKT LSTM — needs 100+ completions per user
- RL DQN — needs DKT mastery scores
- SARIMAX — needs 4+ weeks usage data
- L8 PII Filter — needs significant cloud usage
- L1 Evaluation — needs stable core loop
- Signals API — needs DKT + RL

**Section 5: Known Issues**
- OR-Tools / Python 3.13 compatibility issue (abort on import in some test contexts)
- DraftStore backward-compat aliases (legacy callers not yet migrated to new API)
- Memory store methods are synchronous (need asyncio.to_thread wrapping on hot path)
- control_policy.py not yet wired to intent registry (still uses hardcoded routing)

**Section 6: Test Coverage**
- 113 tests across 14 test files
- All pass in < 1 second
- Coverage: registry, intents, drafts, memory store, SM-2 scoring, extraction, constraint bridge, document registry, document pipeline, PEARL patterns

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add docs/PROJECT_STATUS.md
git commit -m "docs: rewrite PROJECT_STATUS.md — honest post-reset status"
```

---

### Task 3: Update INDEX.md

**Files:**
- Modify: `docs/INDEX.md`

- [ ] **Step 1: Read current INDEX.md**

Verify all links point to existing files.

- [ ] **Step 2: Update INDEX.md**

Add the new FUTURE_ARCHITECTURE.md to the Architecture & Vision section. Change it from a backtick reference (planned) to a proper link (now exists). Also add the Phase 1D plan to the implementation plans section.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add docs/INDEX.md
git commit -m "docs: update INDEX.md — add FUTURE_ARCHITECTURE.md link and Phase 1D/1E plans"
```

---

### Task 4: Final Validation — Full Test Suite

**Files:** None (verification only)

- [ ] **Step 1: Run the complete test suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_registry.py tests/test_intent_routing.py tests/test_draft_store.py tests/test_core_pipeline.py tests/test_memory_store.py tests/test_memory_retriever.py tests/test_memory_extractor.py tests/test_memory_constraint_bridge.py tests/test_memory_integration.py tests/test_document_registry.py tests/test_document_pipeline.py tests/test_document_integration.py tests/test_pearl.py tests/test_pearl_integration.py -v`

Expected: 113 passed

- [ ] **Step 2: Verify all doc files exist**

Run: `ls docs/FUTURE_ARCHITECTURE.md docs/PROJECT_STATUS.md docs/INDEX.md docs/POLICY_ENGINE_ARCHITECTURE.md docs/PITCH_ARCHITECTURE.md`

Expected: All 5 files listed, no errors.

- [ ] **Step 3: Final commit log**

Run: `git log --oneline -40`

Verify the full sequence of commits from Phase 1A through 1E is present and clean.

---

## Phase 1E Complete Checklist

After completing all 4 tasks, verify:

- [ ] `docs/FUTURE_ARCHITECTURE.md` exists with full DKT/RL/SARIMAX/L8/L1/Signals specs
- [ ] `docs/PROJECT_STATUS.md` is accurate and honest — no false claims
- [ ] `docs/INDEX.md` links to FUTURE_ARCHITECTURE.md (not just backtick reference)
- [ ] All 113 tests pass
- [ ] All documentation files exist and are up to date

**Architecture Reset is COMPLETE after Phase 1E.**
