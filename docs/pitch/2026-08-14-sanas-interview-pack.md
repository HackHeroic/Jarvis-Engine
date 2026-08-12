# 🎯 Sanas Interview Pack — Jarvis Demo & Talking Points

**Interview:** Aug 14 · **Everything below verified working Aug 13.**

---

## 1. Tonight / 30 minutes before

| ✔ | Step | Command / action |
|---|------|------------------|
| ☐ | **Load a model in LM Studio** (server is running but empty!) | Open LM Studio → load a Gemma-family model (12B-class if RAM allows, E4B otherwise). Detection auto-picks it at backend startup. This makes "local-first" REAL in the demo. |
| ☐ | Start backend | `cd ~/Jarvis-cursor/Jarvis-Engine && .venv/bin/uvicorn app.main:app --port 8000` |
| ☐ | Check the startup line | Should name your loaded Gemma as PRIMARY. If it says "Gemini cloud" — model isn't loaded; demo still works via fallback. |
| ☐ | Start frontend | `cd ~/Jarvis-cursor/jarvis-frontend && npm run dev` → http://localhost:3000 |
| ☐ | One warm-up run | Type `hi`, then one planning prompt. First LLM call is the slowest — don't let it be during the interview. |
| ☐ | Have http://localhost:8000/docs open in a tab | Auto-generated API explorer — looks professional, shows the deprecated-v1 discipline. |
| ☐ | Have `docs/PROJECT_STATUS.md` open in your editor | The three diagrams render in VS Code's markdown preview. |

**If something dies mid-demo, that's a feature:** kill the DB or the LLM and chat still answers (say: "every dependency is allowed to fail — stores degrade to empty context, a dead local model falls back to cloud, solver failures return an anti-guilt message, never a stack trace").

---

## 2. The 5-minute demo script (exact prompts, in one conversation)

**Narrate the left-side trace as you go** — the phase lines with cycling verbs ARE the architecture demo: each line is a LangGraph node executing.

1. **`hi`** → instant reply.
   *Say:* "Trivial inputs never touch an LLM — a regex fast path answers in milliseconds. Latency budget matters; you don't spend a model call on 'hi'."

2. **`I never want to study before 11am`** → "Noted and locked in" + green **Constraint saved** card.
   *Say:* "This is the core thesis: **memories change the math**. That sentence just became a hard constraint row that feeds the constraint solver on every future plan — not a prompt hint, an actual blocked region in the schedule optimizer."

3. **`prepare a 20-minute technical presentation on my AI scheduling project by Friday`** → watch phases stream: extraction → intent → planning module → decomposition → solving → draft.
   *Say, pointing at the trace:* "A LangGraph orchestrator routes to a planning sub-graph: goal validation gates the pipeline, habit translation and memory-constraints run in parallel into an AND-join barrier, an LLM decomposes the goal into ≤25-minute micro-tasks, and then — deliberately NOT an LLM — **Google OR-Tools CP-SAT** solves the actual schedule with deadline-priority weighting, adaptive daily caps, and sleep blocks. LLMs propose; the solver disposes. Notice nothing is scheduled before 11am."

4. **`accept`** → "Locked in, sir — N tasks."
   *Say:* "Draft-review loop: nothing writes to the database until explicit acceptance, and the accept verifies rows actually landed before claiming success — it re-queries by the write's plan_id. Honest failure over fake success."

5. Open the **Schedule** page → tasks laid out; complete one → background replan triggers.
   Open the **Habits** page → "Active constraints" shows your 11am rule.

6. **(The wow, if time)** Ctrl-C the backend mid-conversation, restart it, type `accept` again in the same chat → "no draft awaiting review."
   *Say:* "Conversation and negotiation state live in a SQLite checkpointer with user-scoped threads — the process is disposable, the negotiation isn't. Uploaded documents and live clients are scrubbed before serialization."

---

## 3. The 90-second architecture answer (memorize this)

> "Jarvis is a local-first AI chief-of-staff. The interesting part is it's a **hybrid neuro-symbolic system**: LLMs handle language — extracting goals, habits, and constraints from a brain dump — but scheduling is a **CP-SAT constraint solver**, because 'no study before 11am, sleep is blocked, deadline Friday, max 3 hours of deep work a day' is a constrained-optimization problem, and language models are provably bad at those. The spine is a LangGraph state machine — 13 orchestrator nodes routing to cognitive sub-graph modules — with a SQLite checkpointer so multi-turn negotiation survives restarts, real token streaming over SSE, and a two-tier model router: a small local model for classification, a big local model for reasoning, cloud fallback only when local is down — with a PII gate in front of any cloud call. And when the solver says a week is infeasible, the product says 'this is a scope problem, not a you problem' — the psychology is designed in, not bolted on."

---

## 4. War stories (they WILL ask "hardest bug" / "tell me about a technical challenge")

**Pick 1–2. These are genuinely good.**

1. **The pipeline that never ran.** "My module framework compiled LangGraph sub-graphs from declarative step definitions. Two edge-wiring defects meant the planning pipeline silently truncated after the third node — everything downstream was orphaned dead code, while the system *looked* fine because a fallback path answered. Found it by dumping the compiled graph's edges and comparing to the declaration. The naive fix double-fired the LLM decomposition node — LangGraph treats multiple plain edges as independent triggers, not a barrier — so I implemented proper AND-join semantics with the list-form edge API and added a build-time orphan check so the whole bug class is now unrepresentable."

2. **The test suite that lied.** "460 offline tests, all green — and live testing found decomposition producing zero tasks. The transport returned structured responses as dicts; the router only normalized strings; the parse failure was swallowed and the schedule got built from stale data while looking successful. The test fake returned a typed object production never produces. Lesson I now design by: **fakes must return the production shape, and the seam you mock must be the real transport boundary** — and offline suites need at least one paid end-to-end run before you trust them."

3. **The regex that would have deleted user data.** "Draft acceptance was gated by a keyword regex. Adversarial review showed 'I need to *confirm* my exam registration' would trigger a destructive accept — delete-and-replace of every pending task. I rebuilt it as full-coverage matching — every word of the message must be consumed by acceptance phrases — and probed it with ~103 adversarial utterances. Zero leaks, all 14 legitimate accepts still pass. On the same path I made persistence verification plan_id-scoped so 'success' means *this write's* rows exist, not just any rows."

---

## 5. Q&A they might throw at you

- **"Why not let the LLM do the scheduling?"** → Constrained optimization with hard invariants (no-overlap, dependencies, caps) needs a solver with proofs. CP-SAT returns OPTIMAL or INFEASIBLE — an LLM returns plausible. INFEASIBLE is also product signal: it drives the anti-guilt recalibration flow.
- **"How do you test something built on LLMs?"** → Three layers: 480 offline tests with a **socket guard** that hard-fails any network egress (LLM transports faked at the boundary, returning production shapes); grammar/schema-constrained outputs so invalid responses are unrepresentable; and paid live E2E runs as the final gate — which caught what mocks couldn't.
- **"Why local-first?"** → Privacy (your calendar/habits are intimate data), latency (fast-path + small-model classification), cost, and honesty about degradation: local → cloud fallback with a regex PII gate before anything leaves the machine.
- **"What's the weakest part?"** (be honest — it lands well) → "The research module's cognition is stubbed, hooks/consent tiers aren't wired, and there's a truthfulness bug where a failed decomposition can present a stale-task schedule as success — it's Known Issue 11 in my status doc with a fix design. I keep a verified known-issues list instead of pretending it's finished."
- **"What's next?"** → Generative UI (approved 3-tier spec: typed result cards → grammar-constrained JSON component catalog that even a small local model can fill reliably → sandboxed free-form widgets), then DKT mastery tracking feeding task difficulty.

**Bridge to Sanas (use once, don't force):** "A lot of this maps to what you do: real-time streaming pipelines with strict latency budgets, small-model/big-model routing, on-device inference with graceful cloud fallback, and shipping ML systems where the failure modes are handled honestly instead of hidden."

---

## 6. Doc reading order (open these tonight)

| # | Doc | What it is / what to say about it |
|---|-----|-----------------------------------|
| 1 | `docs/PROJECT_STATUS.md` | **The master doc.** Three code-verified Mermaid diagrams (request flow, planning pipeline, negotiation state machine), component inventory, storage map, 11 verified known issues. If they ask anything architectural, the answer is a diagram here. |
| 2 | `docs/JARVIS_TECHNICAL_BRIEF.md` | Narrative technical overview — good pre-read to refresh the story arc. |
| 3 | `docs/superpowers/specs/2026-08-08-one-brain-stabilization-unification-design.md` | Spec with an **8-entry decision log** (D1–D8: why keep Supabase, why SQLite checkpointer, why unify onto v2…). Perfect for "walk me through a technical decision you made and the alternatives you rejected." |
| 4 | `docs/superpowers/specs/2026-08-08-generative-ui-design.md` | The roadmap spec — 3-tier generative UI with its own 9-entry ADR log. Answers "what's next" with receipts. |
| 5 | `docs/SELLING_POINTS_AND_COMPETITIVE_ANALYSIS.md` | Product positioning if the conversation goes product-side. |
| 6 | `docs/PITCH_ARCHITECTURE.md` + `docs/pitch/Jarvis_VC_Pitch_v5.pptx` | Only if you want pitch-deck visuals; note PITCH_ARCHITECTURE describes the deprecated v1 path (banner says so). |
| 7 | `.claude/CLAUDE.md` | Repo map — skim so you can navigate live if they ask to see code. Best files to show: `app/orchestrator/graph.py` (the state machine), `app/core/module_framework.py` (the barrier-semantics fix + orphan guard), `app/core/or_tools/solver.py` (CP-SAT). |

---

## 7. Numbers to have on the tip of your tongue

- **480 tests passing** (1 documented xfail), fully offline, ~6s, socket-guarded — across 39 test files
- **13-node** LangGraph orchestrator; **10-step** planning sub-graph with parallel barrier joins
- **9-layer** architecture (L0 Apple Silicon/MLX → L9 orchestration)
- Two-model local routing (heavy reasoning + fast classification) + cloud fallback + PII gate
- Multi-turn negotiation state survives process restarts (SQLite checkpointer, user-scoped threads)
- Whole system verified end-to-end live, twice, for ~**$0.02** of cloud spend
- Psychology designed in: TMT deadline motivation → solver weights, CLT ≤25-min micro-tasks, anti-guilt INFEASIBLE handling, WOOP implementation intentions
