# Post-Spine Roadmap — what's left after the May 1 60-min spine session

**Date:** 2026-05-01
**Author:** Madhav + Claude (Opus 4.7, 1M)
**Context:** Reference document from the 60-minute "spine" session.
**Status:** Ready to schedule

---

## What shipped in the 60-min spine (2026-05-01)

| # | Change | File(s) |
|---|---|---|
| 1 | Smart model auto-detect (prefers 26B for primary, E4B for fast; falls back to single-model) | `app/core/config.py`, `app/main.py` |
| 2 | Greeting / trivial-input fast path — no LLM, routes straight to CHAT | `app/orchestrator/graph.py` |
| 3 | All `"Done."` literals replaced with `"Standing by, sir."` (Jarvis voice) | `voice_of_jarvis.py`, `chat.py` |
| 4 | UserModel.`build_chat_context()` — concrete method, injects memories + tasks + goals + draft + energy | `app/core/user_model.py` |
| 5 | Conversation module auto-builds context from UserModel when state didn't pre-populate | `app/modules/conversation.py` |
| 6 | Memory edit endpoint (`PATCH /api/v1/memories/{id}`) — content/type/confidence | `app/api/v1/endpoints/memories.py` |
| 7 | Observation Loop hardened: `phase_start`/`phase_end` SSE, memory extraction in background task, hard 500ms cap with `asyncio.shield`, skip on trivial inputs | `app/core/observation.py` |
| 8 | Workspace persistence — `task_workspaces` table, 24h TTL, `?refresh=true`, cache invalidates on new doc-link | `workspace_builder.py`, `workspace.py`, `migrations/2026-05-01_task_workspaces.sql` |
| 9 | Retroactive doc→task auto-linking already worked (linker scans 500 most recent tasks); now busts workspace cache for matched tasks | `task_material_linker.py` |
| 10 | Anti-Guilt auto-reschedule — `detect_and_mark_missed()` runs at PLAN_DAY entry, surfaces blame-free framing | `app/services/analytical/missed_deadlines.py`, `control_policy.py`, `voice_of_jarvis.py` |
| 11 | Qwen references purged from comments | `litellm_conf.py` |

**Smoke test status:** all 13 touched files compile cleanly. Three modules show pre-existing `supabase` import errors that predate this session (environment-level, unrelated to spine work).

---

## What's still pending (priority-ordered)

### P0 — needed for "true Jarvis" core

1. **PEARL → OR-Tools constraint bridge** (4–6h)
   PEARL detectors emit patterns to memory store, but the bridge into solver constraints isn't wired. When a high-confidence pattern like "skips morning tasks 70%" is detected, it should produce a `BehavioralConstraint` row that the next `_run_plan_day_flow` reads and feeds to CP-SAT. This is the *moat* feature in the v2 spec — memories changing the math.
   - Files: new `app/services/analytical/memory_to_constraint_bridge.py`, hook into `_run_plan_day_flow`

2. **Sub-agent / sub-graph framework** (6–10h)
   Convert Research and Knowledge modules from function-call-style to proper `StateGraph` sub-graphs (Planning is already partly there). Enables real autonomous iteration (research can re-search, knowledge can re-classify on disagreement).
   - Files: `app/modules/research_graph.py`, `app/modules/knowledge_graph.py` (currently function imports — promote to LangGraph compiled apps)

3. **Action Hooks tier system** (3–5h)
   `app/orchestrator/hooks.py` exists as a stub. Implement Cautious → Balanced → Autonomous tiers. Wire 7 events: PreModuleExecution, PostModuleExecution, PreScheduleModify, PreCloudLLM (PII filter), PreMemoryWrite, CostThreshold, ProactiveSuggestion.
   - Frontend dependency: a "consent_request" SSE event needs UI surface.

### P1 — psychology framework completion

4. **WOOP** in `TaskChunk.implementation_intention` — already declared in schema; flesh out generation in Socratic chunker. Currently set inconsistently. (2h)
5. **CLT** — intrinsic_load is set, germane_load is set, but extraneous_load isn't pushed back into solver soft penalties. (2h)
6. **TMT** — currently used as integer priority; tie it to a softer weighted-objective rather than hard ordering. (2h)
7. **Mastery Orientation** — coach module should phrase progress feedback in mastery (self-referenced) terms, not performance (peer-referenced). Prompt change + a memory tag to track if user prefers mastery framing. (1h)

### P1 — "feels alive" features

8. **Background memory persistence audit** — verify the new background memory extraction actually writes to `user_memories`. Add a smoke test that does a full chat turn and confirms a row appears. (1h)
9. **Auto-replan on PEARL signal** — if PEARL detects a pattern with confidence ≥0.85 mid-day, surface a *proactive suggestion* event (gated by tier system). (3h)
10. **Frontend memory page** — backend CRUD is done; needs `app/(app)/memories/page.tsx` to wire to GET/PATCH/DELETE. Lets user see/edit/forget. (3h)

### P2 — Claude Code feature parity (further out)

11. **MCP server adapter** (8–12h) — Jarvis as MCP server (exposing tasks/schedules/memories) and as MCP client (consuming external tools).
12. **Skills system** (4–6h) — user-definable IntentBlueprints loaded from `~/.jarvis/skills/`.
13. **Slash commands** (2–3h) — `/plan`, `/focus`, `/review`, `/ingest`, `/research` as first-class entry points.
14. **Output styles** (3–4h) — context-adaptive tone variants beyond "Jarvis voice" (e.g., concise mode, study mode).
15. **Hybrid graph-RAG layer** (4–6h) — Supabase `knowledge_triples` + ChromaDB hybrid retrieval. **Defer until concrete multi-hop retrieval failures observed.**

### P3 — analytics

16. DKT LSTM (mastery tracking) — stub at `app/models/analytical/dkt_lstm.py`. Needs 100+ completions/user before training; collect data first.
17. RL DQN (optimal task ordering) — depends on DKT.
18. SARIMAX (energy forecasting) — replaces heuristic daily cap; needs 4+ weeks of completion data per user.

---

## Sequencing recommendation

The tightest next session is **#1 (memory→constraint bridge)** because it converts the spine from "system that works" into "system that gets smarter." After that, **#2 (sub-graph framework)** unlocks autonomous research properly. Then #3 (hooks) becomes the foundation for proactive suggestions.

Each of these is a clean 1-session block. Don't try to bundle them — same trap as today.

---

## Smoke test scenarios for the next session

These should pass before declaring the system "truly Jarvis":

1. **Greeting**: "hi" → real Jarvis reply, intent=CHAT, learning phase <500ms, no "Done."
2. **Cross-chat memory**: Tell Jarvis "I prefer studying after 2 PM" → restart server → in next chat, memory shows up in `build_chat_context()` injection.
3. **Memory CRUD**: `GET /api/v1/memories?user_id=X` → list. `PATCH` one → content updates. `DELETE` → archived.
4. **Math test scenario**:
   - Day 1: "I have a math test Friday — help me prep" → tasks created, scheduled.
   - Day 3: Upload a DPP PDF → ingestion runs → `link_document_to_tasks` matches by cosine ≥0.65 → workspace cache invalidates.
   - Day 3: `GET /api/v1/tasks/{math_task_id}/workspace` → returns workspace WITH new DPP linked. `?refresh=false` (default) returns cached after first build.
5. **Anti-Guilt**: skip a task past its deadline → next "plan today" → response leads with anti-guilt framing, not shame.
6. **Single-model resilience**: stop LM Studio → server still boots, falls through to Gemini (if key set). Restart LM Studio with only one Gemma loaded → next request uses it for both PRIMARY + FAST.

---

**End of roadmap.**
