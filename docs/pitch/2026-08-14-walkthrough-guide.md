# Walkthrough Guide — What to Open, In What Order, And What to Say

**Companion to `2026-08-14-sanas-interview-pack.md` (the demo script) and `preflight-checklist.md` (setup).
This doc answers: which files go on screen, which diagrams are current, and the exact narration per stop.**

---

## 0. The one-line map of your own docs

| Doc | Verdict |
|---|---|
| `docs/PROJECT_STATUS.md` | ✅ **The only current architecture doc.** 3 code-verified diagrams. Keep it open the whole time. |
| `docs/superpowers/specs/2026-08-08-one-brain-...-design.md` | ✅ Current spec. Target-architecture diagram (§3) + D1–D8 decision log. |
| `docs/superpowers/specs/2026-04-12-jarvis-architecture-v2-design.md` | ✅ The vision diagram (line 50, User Model at center). Header still says "Draft" — don't scroll to it, or say "index marks it implemented." |
| `docs/superpowers/specs/2026-08-13-ask-back-clarification-design.md` | ✅ "What's next" spec (solver-asks). Not built yet — say so. |
| `docs/JARVIS_TECHNICAL_BRIEF.md` | ⚠️ May snapshot. §3.1 topology + §7 psychology diagrams are good; **§9 status diagram says the orchestrator migration is pending — it shipped.** Don't show §9 or §4.2 (hooks/tiers). |
| `docs/PITCH_ARCHITECTURE.md` | ⚠️ Dated 2026-03-28, pre-LangGraph. Diagrams 3 & 9 (constraint bridge, PEARL) still accurate; Diagram 5 contradicts the real negotiation state machine. |
| `docs/POLICY_ENGINE_ARCHITECTURE.md` | 🚫 Deprecated v1. ~35 diagrams that will contradict you. Open only to say "this is the before picture." |
| `docs/FUTURE_ARCHITECTURE.md` | 🔮 Deliberately future (DKT/RL/SARIMAX). Open only for "why isn't the ML built" — answer: data-starved, not undesigned. |

---

## 1. Screen-share sequence (docs → demo → code)

### Stop 1 — `docs/PROJECT_STATUS.md`, Diagram 1 (line 44) — "Unified v2 Request Flow"
VS Code markdown preview. **Say:** "This is drawn from the compiled graph, not from intention — real node names, the SQLite checkpointer, the SSE streaming machinery. One entry point, `/api/v1/chat/v2/stream`; the old v1 routes are the dotted deprecated node."

### Stop 2 — same file, Diagram 2 (line 125) — "Planning Module (real compiled shape)"
**Say:** "Goal validation gates the pipeline; habit translation and memory-derived constraints run in parallel into an AND-join barrier; an LLM decomposes into ≤25-min micro-tasks; then CP-SAT — not an LLM — solves the schedule. INFEASIBLE walks a horizon ladder, 3→5→7→14→30 days, and exhaustion returns an anti-guilt message, never a 500."

### Stop 3 — same file, Diagram 3 (line 188) — negotiation state machine
**Say:** "Draft review is a state machine persisted in the checkpointer — NONE, REVIEWING, EDITING, ACCEPTED. Nothing writes to the task table until explicit accept, and accept re-verifies its own write by plan_id."

### Stop 4 — live demo
Run the 5-minute script in the interview pack (`hi` → 11am constraint → plan prompt → accept → schedule page → optional restart-resume wow moment).

### Stop 5 — code walk (only if they want code; 30–60s per file)

| File | Show | Say |
|---|---|---|
| `app/orchestrator/graph.py:582` | `build_jarvis_graph()` | "The whole spine in one screen — every node and edge, including the regex fast path so 'hi' never costs an LLM call." |
| `app/modules/planning_graph.py:278` | `solve_schedule` | "**The money line** — LLM output is coerced into a validated ExecutionGraph and handed to the deterministic solver off-thread. A solver 422 becomes the INFEASIBLE retry path, not an error." |
| `app/core/or_tools/solver.py:47` | `JarvisScheduler` | "Modern CP-SAT API: AddNoOverlap single-tasking, TMT-weighted objective. The docstring says it: this prevents the LLM from hallucinating overlapping tasks." |
| `app/services/memory/constraint_bridge.py` | docstring | "Read it verbatim: *ChatGPT memory affects what the LLM says; Jarvis memory affects the mathematical constraints in OR-Tools.* That's the moat in one sentence." |
| `app/core/user_model.py` | whole file (199 lines) | "The Soul of Jarvis — one lazy facade every module reads and writes, hydrated per-invocation from user_id. State is data; facades are per-invocation." |
| `app/core/observation.py:112` | `run_observation_loop` | "After every turn: extract memories, detect PEARL patterns (pure SQL, no LLM), update cognitive state, bridge patterns to constraints — with SSE events so the UI visibly shows learning." |

---

## 2. The supervision-layer overlay (if the conversation goes to AI agents / oversight)

Frame Jarvis in the field's three supervision layers — 60 seconds:

1. **Architectural supervision** — "The LangGraph orchestrator is a supervisor pattern: 13 nodes, hub-and-spoke routing to 5 cognitive modules, single point of state control in a checkpointer."
2. **Human supervision (HITL)** — "Draft negotiation is a human-in-the-loop gate placed exactly where the field says it belongs: at the irreversible action. Nothing writes to the database until explicit accept — and the accept matcher is full-coverage, adversarially tested with ~103 utterances, because a keyword regex would have let 'confirm my exam registration' trigger a destructive write."
3. **AI-on-AI supervision** — "Most verification layers are an LLM judging an LLM — probabilistic checking probabilistic. Mine is a **formal verifier**: CP-SAT returns OPTIMAL or INFEASIBLE with proofs. LLM proposes, solver disposes. And the next spec (ask-back) extends it: the solver's infeasibility conflict core becomes grounded clarifying questions — non-LLM, provably-grounded ask moments generic agents don't have."

Closer: "Everyone has an LLM. Nobody has memories that change the math."

---

## 3. Numbers — say these, not those

| ✅ Say | 🚫 Don't say | Why |
|---|---|---|
| **480 tests passing, 1 documented xfail, ~7s, fully offline (socket-guarded)** | 473, or the 113 in SELLING_POINTS | 480 is the latest verified run (preflight doc). Pick one number and stick to it. |
| Horizon ladder **3d → 5d → 7d → 14d → 30d** | "48h → 72h → 5d" | Old docs have the stale ladder. |
| "Hooks: 7 events registered, **2 wired** — I cut the tier system rather than ship consent theater" | The tier table in TECHNICAL_BRIEF §4.2 | Honesty here is a flex; the stale diagram is a trap. |
| 13-node orchestrator, 10-step planning sub-graph, ~$0.02 total live E2E spend | The girlfriend-texts-dinner story as if live | Ambient Life Intelligence is a planned feature (SELLING_POINTS #10 self-labels it). |

---

## 4. Known landmines

- **Load a model in LM Studio before starting the backend** — otherwise startup flips to Gemini cloud and "local-first" is a claim, not a demo. (Fallback still works; just narrate it as graceful degradation.)
- Never run `npm run build` while `npm run dev` is up (corrupts `.next`; fix: kill, `rm -rf .next`, restart).
- `POST /drafts/{id}/chat` returns **501** — don't demo chat-modify of a draft; use "accept" or a re-plan message.
- Document intelligence (calendar extraction → task linking) is **stub-quality** — don't demo a PDF upload promising task linking.
- If asked "weakest part," use the prepared honest answer (research module stubs, Known Issue 11) — it lands better than deflection.
