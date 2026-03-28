# Jarvis Engine — Project Status

**Last Updated:** 2026-03-29
**Author:** Architecture Review (Claude + Madhav)

---

## Current State: Architecture Reset Complete

After a full architecture reset (Phase 1A–1D), the Jarvis Engine has a clean, extensible foundation. The system now uses a BaseRegistry framework for intents, document types, and behavioral patterns. LLM routing defaults to Gemini 2.5 Flash for schema-critical tasks. Drafts persist in Supabase (surviving server restarts). A 3-tier memory system with SM-2 decay tracks user preferences and facts. PEARL behavioral pattern detection identifies skip/completion trends. A document intelligence pipeline classifies and processes uploaded materials. 113 tests pass across 14 test files.

---

## What Works End-to-End

- **Brain dump → schedule**: Brain dump → intent extraction → plan-day pipeline → OR-Tools CP-SAT schedule → Voice of Jarvis response
- **Multi-goal fusion**: Pending tasks + new goal merged into a single schedule with TMT priority
- **Habit translation**: Natural language → semantic time slots → solver constraints (hard/soft blocks)
- **Document ingestion**: PDF → Docling → ChromaDB chunks → task-material linking (cosine similarity ≥ 0.65)
- **Task lifecycle**: Complete, skip, update, delete → triggers background replan
- **SSE streaming**: Phase updates, thinking tokens, message tokens streamed to frontend
- **Draft review**: Accept / reject / edit flow with Supabase persistence

---

## New in Architecture Reset (Phase 1A–1D)

| Component | What It Does |
|-----------|-------------|
| **BaseRegistry Framework** | Extensible registry pattern for intents, document types, and PEARL patterns. New types added by registering, not by editing routing logic. |
| **Intent Registry** | 9 registered intents with handler lookup (plan_day, greeting, behavioral, calendar_sync, knowledge_ingestion, action_item, task_update, task_query, general_chat) |
| **Document Type Registry** | 5 document types: practice_problems, lecture_notes, syllabus, assignment, reference |
| **Document Intelligence Pipeline** | Classify → dispatch to type-specific handler → store memory and link to tasks |
| **3-Tier Memory System** | Working memory (session context), recall memory (LLM-generated summaries), archival memory (persistent user_memories with SM-2 decay) |
| **SM-2 Memory Decay** | Memories fade if not reinforced. Strength score determines retrieval priority. Reinforcement on access. |
| **Memory Extraction** | LLM extracts facts and preferences from conversations (fire-and-forget background task) |
| **Contradiction Detection** | New memories that contradict old ones supersede them (old marked superseded, not deleted) |
| **Memory → Constraint Bridge** | Memories change OR-Tools math — user preferences become solver constraints. This is the moat. |
| **PEARL Behavioral Intelligence** | Detects skip/completion patterns across tasks. Generates behavioral insights and recommendations. |
| **Gemini-Primary LLM Routing** | Gemini 2.5 Flash as primary for schema-critical structured output. Reliable JSON parsing. |
| **DraftStore Supabase Migration** | Drafts stored in Supabase instead of in-memory dict. Survive server restarts. |

---

## What's Deferred (See FUTURE_ARCHITECTURE.md)

| Component | Why It's Deferred |
|-----------|------------------|
| **DKT (Deep Knowledge Tracing)** | LSTM needs 100+ task completion events per user to train meaningfully |
| **RL (Reinforcement Learning / DQN)** | Requires DKT mastery scores as state input — can't build without DKT |
| **SARIMAX (Cognitive Energy Forecasting)** | Needs 4+ weeks of continuous usage data for seasonal decomposition |
| **L8 PII Filter** | Needed when sending significant user content to cloud LLMs — not yet the case |
| **L1 Evaluation** | Needs stable core loop before feedback signals are meaningful |
| **Signals API** | Depends on DKT + RL being implemented to consume the signals |

Full specifications preserved in [FUTURE_ARCHITECTURE.md](FUTURE_ARCHITECTURE.md).

---

## Known Issues

1. **OR-Tools / Python 3.13 compatibility** — `ortools` can abort on import in some test/runtime contexts under Python 3.13. Workaround: use Python 3.11 or 3.12.
2. **DraftStore backward-compat aliases** — Legacy callers still use old method names. Aliases exist but callers have not been migrated to the new API surface.
3. **Memory store methods are synchronous** — MemoryStore read/write operations block the event loop. Need `asyncio.to_thread` wrapping on hot paths.
4. **control_policy.py not wired to intent registry** — Control policy still uses hardcoded intent routing. The IntentRegistry exists and is tested but not yet called from the live request path.

---

## Test Coverage

- **113 tests** across **14 test files**
- All pass in < 1 second (no network calls, no real LLMs)
- Coverage areas:
  - `test_registry.py` — BaseRegistry framework
  - `test_intent_routing.py` — Intent registry and handler dispatch
  - `test_draft_store.py` — DraftStore CRUD and Supabase persistence
  - `test_core_pipeline.py` — Core pipeline integration
  - `test_memory_store.py` — MemoryStore CRUD, SM-2 scoring
  - `test_memory_retriever.py` — Memory retrieval and ranking
  - `test_memory_extractor.py` — LLM-based memory extraction
  - `test_memory_constraint_bridge.py` — Memory → OR-Tools constraint bridge
  - `test_memory_integration.py` — End-to-end memory flow
  - `test_document_registry.py` — Document type registry
  - `test_document_pipeline.py` — Document classification and processing
  - `test_document_integration.py` — Document intelligence end-to-end
  - `test_pearl.py` — PEARL pattern detection
  - `test_pearl_integration.py` — PEARL behavioral intelligence integration
