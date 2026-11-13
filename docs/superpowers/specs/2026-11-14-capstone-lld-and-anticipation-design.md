# Capstone LLD Restructure + Anticipation Engine — Design

**Date:** 2026-11-14
**Status:** Draft, awaiting Madhav's approval
**Branch:** `capstone-lld` (cut from `spine-may1-wip` in Jarvis-Engine; matching branch to be cut in jarvis-frontend)
**Team:** Madhav (HackHeroic), RAVIYADAV6522, pugazhjs9, amathziah. The work split will be decided later by the team, so every deliverable below has an **owner slot**.
**Evidence base:** four read-only audits run 2026-11-14 covering the backend LLD, the DB/errors/API, features and demo readiness, and Claude Code patterns. Their findings are summarised in §2, with file:line citations kept in the audit outputs.

---

## 1. Intent: why we're doing this

Jarvis is a **hyper-personalized personal intelligence**, not a chat assistant and not a calendar. It builds a living model of one person: when they focus, what drains them, what they avoid and why, and how they respond to failure. It uses that model to **have things ready before they're asked**. The scheduler is a muscle; the User Model plus the observation loop is the brain. (Madhav, 2026-11-14: "keeping things ready even before you need and doing it efficiently, understanding a human both psychologically and mentally.")

The same codebase is the final-year capstone for System Design + Software Engineering. The evaluation is days away and grades 10 Low-Level Design criteria:

1. requirements
2. class diagram
3. OOP
4. SOLID
5. design patterns
6. sequence/activity diagrams
7. API design
8. DB schema
9. error handling
10. a working end-to-end implementation that **reflects the design exactly**

Each member must be able to explain their own part.

So this work has two jobs that pull in the same direction:

1. **Make the design real.** Restructure the code so the class diagram, patterns and SOLID claims are true in the code and an examiner can open any file and find what the diagram says.
2. **Make the vision visible.** Today Jarvis never makes the first move and what it learns stays invisible. The demo must show it anticipating.

## 2. Where we are (audit summary)

**Works end-to-end today:**
- brain dump → plan → CP-SAT schedule as a draft
- negotiate the draft (accept or reject), then persist it
- teach a rule ("no work before 11") → re-plan → the schedule moves
- memory review and forget
- missed-task recovery without guilt (message text only)
- the mastery coach
- resuming a conversation across a restart (SQLite checkpointer)

509 offline tests pass.

**Rubric gaps:**

| # | Criterion | Gap |
|---|---|---|
| 1 | Requirements | Nothing written. There is no Time/Clock actor, which is why nothing is proactive. |
| 2 | Class diagram | Tasks, goals, drafts, habits and memories are `dict` rows. There are about 21 behavioural classes against 339 top-level functions. |
| 3 | OOP | Zero ABCs and one private Protocol. Behavioural inheritance appears once (`ScrubbingSqliteSaver`). |
| 4 | SOLID | God-files: `control_policy.py` (1939 lines), `chat.py` (1618, with a 445-line handler), `graph.py` (658). Adding an intent means editing 3 files. `UserModel._db` is reached into from 5 call sites. 36 collaborators are typed `Any`. |
| 5 | Patterns | Real today: Facade (UserModel), Registry, Chain of Responsibility (hooks), Builder (JarvisScheduler, build_module_graph), function-style Strategy and Decorator. Missing: Repository, State, Observer, Command, and Strategy behind real interfaces. `registry.py` claims inheritance that doesn't exist. |
| 6 | Sequence/activity | Flowcharts exist in PROJECT_STATUS.md, but there are no sequence diagrams. |
| 7 | API | 11 of 50 routes have `response_model`. There are 7 error shapes. `str(exc)` leaks internals. There is no auth seam. Services import domain types from endpoint files (11 upward imports). |
| 8 | DB | 4 FKs in total. No Goal or User table. No RLS. Code writes columns and status values the schema doesn't allow. The health check queries a table no migration creates. 6 tables are dead. |
| 9 | Errors | No custom exceptions and 176 `except Exception` blocks. **Three real bugs:** an IDOR on pending-calendar approve/list, accepting a draft twice duplicates tasks, and a non-atomic delete-then-insert can lose tasks. LLM calls have no timeout. |
| 10 | Demo | Core loop works. Proactive suggestions are stubbed, PEARL has no data within a 10-minute demo, research is a stub, the dashboard shows fake `DEMO_TASKS`, focus windows are a hardcoded hour table, and learning events go out after the stream closes. |

## 3. Goals, non-goals, anti-goals

**Goals**
- G1. Every one of the 10 rubric criteria is demonstrably met, with code as the evidence and docs that point at it.
- G2. A layered architecture: controllers → application services → domain (entities + interfaces) → infrastructure (repositories, LLM providers). There are no upward imports.
- G3. Jarvis visibly anticipates (§7): it acts on a schedule without being asked, within a consent policy the user controls.
- G4. Jarvis visibly understands: a "What Jarvis knows about you" page where every pattern, rule and memory shows its evidence, and a focus profile learned from history instead of hardcoded.
- G5. The three real bugs are fixed, along with every schema mismatch.
- G6. Capstone docs with Mermaid diagrams generated from the finished code, plus an automated **design-conformance test** proving that the diagram matches the code.

**Non-goals (this push)**
- Real authentication or JWT. We add an auth *seam* (a `CurrentUser` dependency) and are honest that it is a stub.
- The Generative UI spec (2026-08-08), MCP, skills, slash commands, DKT, RL, SARIMAX.
- Cloud deployment. The demo runs locally (frontend on localhost, backend on localhost, Supabase cloud).
- The full ask-back spec. Only its deterministic-gate tier is in scope (§8.3).
- Multi-user scale or performance work.

**Anti-goals (failure even if it "works")**
- A diagram that disagrees with the code.
- **Faked intelligence:** hardcoded insights shown as learned. Demo history may be seeded (§9), but every pattern on screen must be computed live by real code from that history.
- Breaking the core loop that works today. The 509 tests stay green throughout.
- Copying Claude Code source. We adopt architectural *ideas* only. Leaked proprietary code in a graded project is an academic-integrity and IP risk.
- Guilt language, or framing Jarvis as a calendar or planner.
- PII sent to the cloud without going through the redaction path.

## 4. Constraints

- **Time:** days. Every phase in §12 ends in a demoable state so we can stop at any phase boundary.
- **Stack stays the same:** Python, FastAPI, LangGraph, OR-Tools, LiteLLM, Supabase (cloud) and ChromaDB on the backend; Next.js 14 and React 18 on the frontend.
- **LLM routing changes (ADR-10, proposed, awaiting Madhav's approval):**
  - Local-first stays: LM Studio Gemma is used whenever it is running.
  - **Claude (Anthropic API, paid from Madhav's existing Console credits) replaces Gemini as the cloud tier.** Gemini stays only as a last resort, so the fallback order is Local → Claude → Gemini.
  - A `CLAUDE_PRIMARY` switch makes Claude first for the demo. It works like today's `GEMINI_PRIMARY`.
  - Claude is called through the official `anthropic` Python SDK (`AsyncAnthropic`), not through LiteLLM.
  - Model `claude-sonnet-5-5` for every cloud call (Madhav's choice, 2026-11-14: Opus is more than this project needs; set by `CLAUDE_MODEL` in config so it can change in one place), using effort to separate the roles: `low` for FAST-role tasks, `medium`/`high` for PRIMARY-role tasks.
  - Structured outputs via `output_config.format` / `messages.parse()` for every schema'd call.
  - Prompt caching on the stable system prompts.
  - Server-side refusal fallback on by default (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`).
  - Estimated spend: roughly half of the Opus estimate: about $0.05–0.10 per turn, and $5–15 for all development and demo use.
- **Supabase is currently paused.** Madhav must restore it from the dashboard. Code work continues offline because tests never touch the network.
- Tests must stay offline (the conftest socket guard).
- Docs use Mermaid plus ADR-style decision logs (Madhav's standing preference).
- Agents: the main session orchestrates and implementation and review subagents run on Opus. Fan out only for genuinely independent work.

## 5. Target architecture (HLD)

```mermaid
flowchart TB
    subgraph FE["jarvis-frontend (Next.js)"]
        PAGES["Dashboard · Chat · Schedule · Habits · Documents · Workspace · You (new)"]
    end
    subgraph API["Controller layer: app/api/v1 (thin routers)"]
        R["Pydantic request/response models · CurrentUser dependency · global error handlers"]
    end
    subgraph APP["Application services: app/services"]
        CTS["ChatTurnService"]
        DS["DraftService"]
        TS["TaskService"]
        MS["MemoryService"]
        AS["AnticipationService"]
        CS["ConsentService"]
        UMS["UserModelService"]
    end
    subgraph ORCH["Orchestrator: app/orchestrator + app/modules"]
        G["LangGraph turn graph"]
        MODS["CognitiveModule implementations<br/>Planning · Research · Knowledge · Coach · Conversation"]
    end
    subgraph DOMAIN["Domain: app/domain (no framework imports)"]
        ENT["Entities: Goal, Task, DraftSchedule, Constraint, Memory,<br/>Pattern, Suggestion, UserProfile, LLMUsageEvent"]
        PORTS["Ports (ABCs): *Repository, LLMProvider, EnergyEstimator,<br/>Trigger, AutonomyPolicy, RetryPolicy, EventListener"]
        EVT["Domain events + EventBus"]
    end
    subgraph INFRA["Infrastructure: app/infrastructure"]
        SB["Supabase*Repository"]
        MEMR["InMemory*Repository (tests)"]
        LLM["LocalProvider · GeminiProvider ·<br/>RetryingProvider · RedactingProvider"]
        CH["ChromaArchive"]
        SOLV["JarvisScheduler (CP-SAT)"]
    end
    FE --> API --> APP
    APP --> ORCH
    APP --> DOMAIN
    ORCH --> DOMAIN
    INFRA -.implements.-> PORTS
    CLOCK(["Clock actor<br/>AnticipationScheduler"]) --> AS
```

**The dependency rule:**
- `domain` imports nothing from `api`, `services`, `orchestrator` or `infrastructure`.
- `services` and `orchestrator` depend on domain *ports*, never on Supabase or LiteLLM directly.
- The composition root (FastAPI lifespan → `AppContainer`) is the only place that picks concrete implementations.

A test enforces the rule by scanning imports.

## 6. Domain model and design (LLD)

### 6.1 Entities (become real classes; the class diagram is drawn from these)

| Entity | Key attributes | Relationships |
|---|---|---|
| `UserProfile` | user_id, display_name, timezone, wake_time, autonomy_mode | 1 → * of everything below |
| `Goal` | id, title, deadline, status | 1 Goal *composes* 1..* Task |
| `Task` | id, task_id, title, duration_min, difficulty, status, scheduled_start/end, deadline, dependencies | * Task ↔ * Task (depends_on) |
| `DraftSchedule` | id, goal_id, tasks[], status (State), horizon_start, expires_at, version | 0..1 per active plan; on accept *produces* Tasks |
| `BehavioralConstraint` | id, kind (rule/habit), time window, recurrence, source (said / inferred) | *aggregated* by UserModel; 0..1 HabitTracker |
| `Memory` | id, type, content, confidence, strength, superseded_by | self-association (supersedes) |
| `Pattern` | detector, description, occurrences, confidence, evidence[] | *derived from* Tasks; may *propose* a Constraint |
| `Suggestion` | id, trigger, kind, title, body, action (Command), status, consent_reason | produced by a Trigger; *holds* 1 ProactiveAction |
| `LLMUsageEvent` | role, model, location (local/cloud), tokens, latency, redactions | recorded by UsageLedger |
| `UserModel` | Facade aggregating Profile, Constraints, Memories, Patterns and EnergyProfile | the centre; every module reads it |

### 6.2 Ports (ABCs)

**Decision (ADR-2):** ports are ABCs, not structural Protocols, so the class diagram's `<<interface>>` and "implements" arrows are literally true and enforced at instantiation.

| Port | Implementations | Pattern |
|---|---|---|
| `TaskRepository`, `GoalRepository`, `DraftRepository`, `ConstraintRepository`, `MemoryRepository`, `SuggestionRepository`, `UsageRepository`, `ProfileRepository` | `Supabase*` and `InMemory*` | Repository; Liskov demonstrated by running the same contract tests against both |
| `LLMProvider` (`complete`, `stream`, `structured`) | `LocalLMStudioProvider`, `ClaudeProvider` (new, official SDK), `GeminiProvider`. `RetryingProvider` and `RedactingProvider` wrap any provider. `FallbackChainProvider` tries providers in order. | Strategy + Decorator + Chain of Responsibility. `LLMProviderFactory` selects by role, availability and the `*_PRIMARY` switches. Adding `ClaudeProvider` without editing any caller is the live OCP demonstration. |
| `EnergyEstimator` | `HeuristicEnergyEstimator` (today's table, kept as prior), `HistoryEnergyEstimator` (learned from completion history, smoothed toward the prior) | Strategy |
| `Trigger` (template method `run()` → `should_fire` / `gather` / `build_suggestion`) | `MorningBriefTrigger`, `PatternRuleTrigger`, `PreTaskPrepTrigger`, `DeadlineRiskTrigger` | Template Method + Strategy |
| `AutonomyPolicy` | `SuggestOnly`, `AskFirst`, `AutoLowRisk` | Strategy, selected by `UserProfile.autonomy_mode` |
| `ConsentRule` chain | deny rules → per-action risk check → user rules → mode policy | Chain of Responsibility; returns `ConsentDecision(behavior, reason)` |
| `RetryPolicy` | `ExponentialBackoff(base, cap, jitter)`, `NoRetry` | Strategy |
| `EventListener` | `PatternObserver` (PEARL), `UsageLedger`, `AnticipationListener`, `TelemetryListener` | Observer via `EventBus` |
| `CognitiveModule` (`name`, `intents`, `build_graph()`) | Planning, Research, Knowledge, Coach, Conversation | Registry + OCP: modules declare their intents, so the router is derived and adding a module touches one file |
| `Step` (Command; `execute`, plus fail-closed `is_read_only`/`is_concurrency_safe` defaults) | `FunctionStep` adapts existing node functions | Command + Adapter; `StepExecutor.run` is the Template Method (flag → validate → consent → pre-hook → execute with timeout → post-hook) |

### 6.3 Design patterns: what goes where, and why (feeds `05-design-patterns.md`)

| Pattern | Where | Why this pattern and not another |
|---|---|---|
| **Singleton** | `AppContainer` (composition root), `EventBus`, `Settings` (one per process via the lifespan) | Exactly one wiring of concrete implementations per process. Accessed through dependency injection, not global lookups, so tests can build their own. |
| **Factory Method / Abstract Factory** | `LLMProviderFactory`, `RepositoryFactory` (supabase / in-memory family), `TriggerFactory` | Construction varies by environment; callers shouldn't know which concrete class they got. |
| **Builder** | `JarvisScheduler` (stepwise CP-SAT model), `ModuleGraphBuilder`, `MorningBriefBuilder` | Complex objects assembled in validated steps. |
| **Strategy** | LLMProvider, EnergyEstimator, AutonomyPolicy, RetryPolicy, PEARL detectors as classes, document handlers | Interchangeable algorithms chosen at runtime; new ones are added without editing callers (OCP). |
| **Observer** | `EventBus` with domain events `TaskCompleted`, `TaskSkipped`, `DraftAccepted`, `PatternDetected`, `LLMCallCompleted` | Learning, telemetry and anticipation react to what happened without the emitter knowing them. This replaces 5 never-fired hook events. |
| **Decorator** | `RetryingProvider`, `RedactingProvider`, the step timeout/hook wrapper | Adds retry and PII redaction around any provider without subclass explosion. |
| **Chain of Responsibility** | Consent rule chain, intent rules (replacing the `_classify_intent` if-ladder), ActionHooks | Ordered handlers where any one can decide. Hooks aggregate **deny > ask > allow regardless of order**, fixing today's first-match bug. |
| **State** | `DraftSchedule` lifecycle (`Pending → UnderReview → Accepted / Rejected / Expired / Superseded`) and `NegotiationPhase` as one transition table | Illegal transitions raise `InvalidStateTransition` (409). This is also how double-accept is rejected. |
| **Command** | `Step`, and `ProactiveAction` held by a `Suggestion` (`describe()`, `execute()`) | A proactive proposal is a deferred, inspectable, consent-gated action. |
| **Facade** | `UserModel` (no more `_db` reach-throughs), `ChatTurnService` | One simple face over many subsystems. |
| **Repository + Unit of Work** | all persistence; atomic accept through a Postgres function | Data access in one layer, user-scoped by construction, which fixes the IDOR class of bug. |
| **Template Method** | `Trigger.run`, `StepExecutor.run`, `BaseRepository` helpers | A fixed algorithm skeleton with variable steps. |
| **Adapter** | LiteLLM → `LLMProvider`, supabase-py → repositories, `FunctionStep` over legacy node functions | Wraps existing code instead of rewriting it. |
| **Null Object** | `NullEventBus`, and `InMemory*` repositories replace `_StubDBClient` | Removes the `hasattr`/`None` checks (the LSP violation today). |

### 6.4 SOLID: where each principle is applied (feeds `04-oop-and-solid.md`)

- **SRP:**
  - `chat.py` is split into a thin router, `ChatTurnService` and `SseEncoder`.
  - `control_policy.py` is dissolved: live helpers move into `services/planning/`, and v1 is deleted (§8.5).
  - UserModel loses prompt rendering (moved to `UserModelPromptRenderer`) and the energy heuristic (moved to `EnergyEstimator`).
- **OCP:**
  - New module = new `CognitiveModule` registration.
  - New trigger = new `Trigger` subclass.
  - New provider = new `LLMProvider`.
  - None of these edits the router or graph.
- **LSP:** `InMemory*` and `Supabase*` repositories pass the **same contract test suite**. `HeuristicEnergyEstimator` and `HistoryEnergyEstimator` are interchangeable.
- **ISP:** small role ports (`TaskReader` vs `TaskWriter` where they diverge). Module nodes receive their projected state (`state_in`/`state_out`), not all 35 JarvisState keys.
- **DIP:** services and modules depend on ports, and `AppContainer` injects concretes. No `create_client` outside infrastructure; today there are 9 sites.

## 7. Anticipation Engine: Jarvis makes the first move

This is my call, because Madhav hadn't answered the "weekday morning" question when this was written. It is built from his own words, "keeping things ready even before you need." Madhav: veto or reshape this section.

**New actor: Clock.** `AnticipationScheduler` is an in-process asyncio loop started in the FastAPI lifespan, ticking every 60s. On each tick it asks each `Trigger` whether it should fire for each active user. For the demo, `POST /api/v1/anticipation/run {trigger}` fires any trigger on demand.

| Trigger | Fires | What it has ready | Default consent |
|---|---|---|---|
| **MorningBrief** | at the user's `wake_time` | Today's plan already built. Yesterday's misses re-slotted, framed without guilt. The hardest task placed in the **learned** peak-focus window. The first task's workspace prepped. One "I noticed…" insight with its evidence. | Read-only parts auto; any schedule change → ask |
| **PatternRule** | when PEARL confirms a pattern (≥3 occurrences, ≥0.7 confidence) | "You've skipped 3 of the last 4 study blocks before 10am. Want me to stop putting study there?" Accept → constraint stored → re-plan draft. | Ask |
| **PreTaskPrep** | 30 min before a scheduled task | The task workspace (linked documents, research results, WOOP if-then plan) is built and waiting. | Auto (read-only) |
| **DeadlineRisk** | hourly | A goal's TMT risk crossed a threshold. Proposes a re-plan draft that explains what moved and why. | Ask |

```mermaid
sequenceDiagram
    participant Clock as AnticipationScheduler
    participant T as MorningBriefTrigger
    participant UM as UserModel (facade)
    participant EE as EnergyEstimator
    participant PL as PlanningService
    participant CP as ConsentPolicy
    participant SR as SuggestionRepository
    participant FE as Dashboard
    Clock->>T: tick(now, user)
    T->>T: should_fire? (wake_time reached, not already today)
    T->>UM: tasks, missed, constraints, patterns
    T->>EE: peak windows (learned)
    T->>PL: build draft for today
    PL-->>T: DraftSchedule
    T->>CP: evaluate(ProactiveAction "apply plan")
    CP-->>T: ASK (reason: schedule change)
    T->>SR: save Suggestion(brief, action, status=pending)
    FE->>SR: GET /anticipation/suggestions
    FE-->>FE: "While you were away" card
    FE->>PL: accept → DraftService.accept (atomic)
```

**Consent model** (idea adopted from Claude Code's permission design, written fresh):
- Every proactive action passes through `ConsentPolicy.evaluate(action) → ConsentDecision(allow | ask | deny, reason)`.
- The reason is typed (`RuleMatch` / `ModeDecision` / `RiskCheck` / `LearnedPreference`), so Jarvis can explain itself: "I didn't move it, because you said never before 11."
- The user sets `autonomy_mode` on the You page.

## 8. Feature work beyond anticipation

### 8.1 Visible understanding: the "You" page (`/you`)

The page shows "What Jarvis knows about you". Each item shows its evidence and source ("you said" vs "I noticed"), and each can be confirmed or forgotten (explicit beats inferred):

- learned patterns with evidence counts
- active rules
- memories with freshness ("noted 12 days ago")
- the **focus profile** (a chart of completion likelihood by hour, from `HistoryEnergyEstimator`)
- autonomy mode and wake time settings
- the **privacy receipt** from the UsageLedger (N calls on-device, M sent to Gemini, K redactions)

### 8.2 Make learning moments arrive

- Memory and learning events are emitted **before** the SSE `complete` frame instead of from a background task after the stream closes.
- Wire up the components that already exist but are never fed or mounted: `pearl_insights`, `LearningToast`, `WoopCard`, `ReplanBanner`, `InfeasibleGuidance`.
- Handle the `consent_request` SSE event in the frontend.
- Remove the dashboard's `DEMO_TASKS` fallback in favour of a real empty state.

### 8.3 Ask-back, tier 1 only

From `2026-08-13-ask-back-clarification-design.md`, take only the deterministic `clarify_gate`:
- a required slot is missing (no real goal, no deadline when one is needed) → typed `clarify_request` → the existing `ClarificationChips` → resume on the same thread.

Solver-conflict explanation (AddAssumptions) is P2.

### 8.4 Research module, minimum real version (P2)

Replace the stub with a real search-and-summarise path through `LLMProvider`, so it passes through `RedactingProvider` and the usage ledger. Use Claude's server-side web search tool when the cloud tier is Claude, and Gemini grounding otherwise. Today it bypasses both.

### 8.5 Retire v1

Delete the deprecated routes and `execute_agentic_flow`; move the live helpers (`_persist_fused_tasks`, the extraction prompt, etc.) into `services/planning/`. After that, `/api/v1/chat/stream` becomes the single chat endpoint (renamed from `/v2/stream`, frontend updated in the same change). This removes the biggest SRP violation and about 2,000 lines an examiner could ask about.

## 9. Demo data: seeded history, real computation

`scripts/seed_demo_user.py` writes about 3 weeks of realistic history for a `demo` profile into Supabase:
- tasks with scheduled and actual times
- completions and skips that contain deliberate, discoverable regularities, such as early-morning study skipped, evenings strong, and a deadline-crunch habit

PEARL, `HistoryEnergyEstimator` and the triggers then compute everything live from that history. The seed is idempotent and resettable (`--reset`). In the viva we say plainly: "seeded history, live inference."

**Honesty rule:** the seed writes exactly the rows and events the live app writes (§9.1). It never writes a shape that only the seed produces.

### 9.1 What "skipped" and "missed" mean (my call, from the builder check)

Today the signals disagree:
- The Skip button writes `pacing_pushed`.
- `missed` means the deadline passed.
- PEARL counts `status == "skipped"`, which nothing writes.

The new semantics:

- **The outcome history lives in a new append-only `task_events` table**, with event types `scheduled | completed | skipped | missed | rescheduled | deadline_missed`. `user_tasks.status` holds only the *current* state.
  - PEARL, `HistoryEnergyEstimator` and the triggers read events, never statuses.
  - Re-slotting a missed task updates the same `user_tasks` row (new times, status back to pending) and appends a `rescheduled` event. No history is lost, and nothing collides with UNIQUE `(user_id, task_id)`.
- **Skip button:** writes a `skipped` event, carrying the reason when one is given. It replaces `pacing_pushed`.
- **Missed is inferred, not asked** ("infer → confirm → adapt"). A `SlotPassedSweep` runs on the AnticipationScheduler tick. A scheduled task whose `scheduled_end` + 30-minute grace has passed, with no `completed` or `skipped` event, gets a `missed` event and the `missed` status.
- **The morning brief re-slots misses with no-guilt framing**, and the user can confirm or correct ("I actually did that").
- **Deadline-passed** becomes its own `deadline_missed` event, so it no longer overloads `missed`.

### 9.2 The learned focus profile changes the solver (my call)

- `HistoryEnergyEstimator` produces a per-hour focus score. `run_schedule` gets a **soft** objective term, difficulty × focus score, so hard tasks lean toward the user's learned peak hours. It applies to every plan, not just the brief, because "memory changes the math" is the moat.
- Its weight sits **below** the TMT deadline priority. Hard blocks and the user's own rules always win.
- It sits behind a flag (`JARVIS_ENERGY_OBJECTIVE`, default on) so it can be switched off if it ever destabilises the demo.
- Tests pin that "teach a rule → the schedule moves" still holds with the term on, and that hard blocks are never violated.
- The morning brief's draft re-solves the whole pending set, using the existing multi-goal fusion semantics. There is no new "accept only today" path.

## 10. Error handling, validation, concurrency (feeds `09-error-handling.md`)

```
JarvisError
├── DomainError
│   ├── ValidationError            → 422
│   ├── NotFoundError              → 404
│   ├── ConflictError              → 409
│   │   ├── InvalidStateTransition   (e.g. accepting an already-accepted draft)
│   │   └── StaleVersionError        (optimistic concurrency)
│   ├── ScheduleInfeasibleError    → 422 with structured guidance (not HTTPException-as-signal)
│   └── ConsentRequiredError       → 202 / SSE consent_request
└── InfrastructureError
    ├── PersistenceError           → 503
    └── LLMError
        ├── LLMTimeoutError          → retry policy, then fallback provider
        ├── LLMUnavailableError      → fallback provider, then 503
        └── LLMOutputParseError      → one repair attempt, then honest degraded reply
```

- **One error envelope everywhere:** `{"error": {"code", "message", "details?", "request_id"}}` from global handlers, with the same shape inside an SSE `error` event. No `str(exc)` reaches a client; internals are logged with the `request_id`.
- **Validation at the boundary:**
  - `Literal`, `max_length`, `UUID` and date types on every request model
  - an upper bound on `file_base64`
  - duration and `start_min` bounds on draft edits
  - `user_id` required, resolved by the `CurrentUser` dependency
- **LLM:** explicit `timeout` on every completion. `RetryingProvider` with exponential backoff and jitter on transient errors only. A mid-stream failure falls back to the next provider. Local→cloud fallback is decided by error *type*, so a local code bug no longer silently ships the prompt to the cloud.
- **Concurrency:**
  - (a) **Atomic, idempotent accept:** a Postgres function `accept_draft(draft_id, user_id)` checks `status='pending'` and expiry, then deletes and inserts the tasks in one transaction; a second call gets 409. This fixes both the double-accept bug and the delete-then-insert loss window.
  - (b) A per-thread `asyncio.Lock` serialises concurrent turns on the same session.
  - (c) Conditional updates (`.eq("status", expected)` or a `version` column) for read-modify-write paths (`reschedule_count`, SM-2).
  - (d) Every repository method requires `user_id`, which closes the IDOR class of bug.
- **Silent-success removal:**
  - The completion-signal insert is fixed (it wrote non-existent columns and swallowed the error).
  - `'missed'` and `'archived'` status values are reconciled with the CHECK constraint.
  - List endpoints no longer return `[]` on a DB failure; they return 503.

## 11. Database (feeds `08-database-schema.md`)

One migration source: `supabase/migrations/`. Numbered N001–N014 are ported in; `app/db/migrations/` is retired. Then:

- **New tables:**
  - `user_profiles` (PK `user_id`): the User entity, so every `user_id` becomes an FK
  - `goals` (backfilled from distinct `goal_id`s): `user_tasks.goal_id` and `draft_schedules.goal_id` become FKs
  - `proactive_suggestions`
  - `llm_usage_events`
- **Keys and indexes:**
  - UNIQUE `(user_id, task_id)` on `user_tasks`
  - index `(user_id, goal_id)`
  - FK `task_materials → ingested_documents ON DELETE CASCADE`, which replaces the manual cascade
- **CHECK constraints:**
  - `duration_minutes > 0`
  - `difficulty_weight` in [0,1]
  - `confidence` and `strength` in [0,1]
  - `ef ≥ 1.3`
  - `scheduled_end > scheduled_start`
  - enums for status, kind, recurrence and signal_type
  - NOT NULL `user_id` everywhere
- **RLS** enabled with owner policies on every user table. The service role bypasses RLS today; the policies document intent and protect the auth seam.
- **Cleanup:**
  - drop the 6 dead tables and the duplicate `conversation_*` tables
  - point the health check at `user_profiles`
  - make `horizon_start` a `timestamptz`

**Class → table mapping** is part of the doc: every entity in §6.1 maps to exactly one table, and every association arrow maps to an FK.

## 12. Phases, in order, each ending demoable

| Phase | Contents | Priority |
|---|---|---|
| **0. Unblock** | Madhav restores Supabase. Cut `capstone-lld` in the frontend. Baseline test run. | P0 |
| **1. Make it safe** | The three bugs (IDOR, atomic accept via `accept_draft` + State, delete-then-insert), LLM timeouts, schema mismatches, health check. | P0 |
| **2a. The spine** | `app/domain` entities and all ports. Error hierarchy, envelope, boundary validation. Repositories for tasks, drafts, constraints, suggestions, profiles, usage and task events (Supabase + InMemory, with shared contract tests). `LLMProvider` strategy, decorators and fallback chain, including `ClaudeProvider`. `EventBus`. Draft State pattern. The schema migration (§11, plus `task_events` from §9.1). | P0 |
| **3a. Make the vision visible (core)** | AnticipationScheduler + MorningBrief + PatternRule + ConsentPolicy + suggestions API; slot-passed sweep (§9.1); `HistoryEnergyEstimator` + solver energy term (§9.2); UsageLedger; seed script; `/you` page; dashboard "While you were away" panel; learning-moment fixes (§8.2). | P0 |
| **2b. Finish the restructure** | Retire v1, split `chat.py`, `CognitiveModule` registry with derived routing, `response_model` on every remaining route plus typed SSE events, remaining repositories (sessions, memories, documents, pending-calendar), UserModel encapsulation, route the remaining LLM bypass sites. | P0 |
| **3b. Vision extras** | PreTaskPrep, DeadlineRisk, ask-back tier 1, then research. | P1, with research P2 |
| **4. Prove it** | Capstone docs (§13), design-conformance and layering tests, demo script, two full dry runs on live Supabase. Then merge `capstone-lld` → `main` in both repos, with Madhav's go-ahead at that moment, so examiners and teammates who open the default branch see the finished design. | P0 |

**Why this order (ADR-1, revised after the builder check):**
- 2a builds only the parts of the restructure that the anticipation path stands on.
- 3a then builds anticipation on the new design, so nothing gets refactored twice.
- 2b finishes the restructure after that.

If time runs out at any boundary, the demo still shows both the design and the vision. Within each phase, work splits into parallel Opus subagents along layer boundaries. The implementation plan decides the exact cuts.

**Who writes the code (Madhav's instruction, 2026-11-14):**
- For now, agents implement everything on Madhav's branch.
- The team decides later who owns which part. The owner slots in §13 are where that goes.
- The plan should still keep each workstream inside one subsystem, so ownership maps cleanly afterwards.

## 13. Deliverables: `docs/capstone/`, one owner slot per file

| File | Contents |
|---|---|
| `01-requirements.md` | Functional requirements, non-functional requirements (latency, privacy/local-first, reliability, honesty, accessibility), actors (User, Clock, Local LLM, Cloud LLM, Solver, Database), use-case diagram, use-case specs for the 8 core UCs. |
| `02-architecture.md` | HLD: layers, components, deployment, the dependency rule. |
| `03-class-diagrams.md` | Overview class diagram plus one per subsystem (Domain, Persistence, LLM, Orchestration, Anticipation), with multiplicities and association, aggregation, composition and inheritance shown correctly. |
| `04-oop-and-solid.md` | The four OOP pillars and five SOLID principles, each with file:line evidence. |
| `05-design-patterns.md` | §6.3 expanded: problem → pattern → where → alternative rejected. |
| `06-sequence-and-activity.md` | Sequence diagrams for plan-from-brain-dump, accept-draft (including the concurrent double-click), teach-rule-and-replan, morning brief, pattern → rule proposal and ask-back. Activity diagrams for the turn graph and the trigger lifecycle. |
| `07-api-design.md` | Every endpoint with request/response schema, the error envelope, the SSE event catalogue, and the layering. |
| `08-database-schema.md` | ER diagram, table specs, keys, indexes, class→table mapping. |
| `09-error-handling.md` | §10 expanded with a failure-scenario table and the concurrency cases. |
| `10-demo-and-contributions.md` | Demo script (about 10 minutes), the viva Q&A bank, and a contribution map with owner slots. |

**Conformance tests:**
- **Class diagram vs code:** `tests/test_design_conformance.py` parses the Mermaid class diagrams and asserts that every class exists, every `<<interface>>` is an ABC, every "implements" arrow is a real subclass, and every listed method exists.
- **Layering:** `tests/test_layering.py` asserts there are no upward imports.

Together these make "the code reflects the design exactly" checkable, not just a claim.

## 14. Left to the builder

- Exact file layout inside each layer, and the names of private helpers.
- The goal backfill mechanics, including what to do with free-text `goal_id`s.
- Polling vs SSE for suggestion delivery. Polling every 30s is acceptable.
- Which existing tests need patch-target updates after moves. Keep re-exports where cheap.
- Chart library for the focus profile.

## 15. Decision log

| ADR | Decision | Why | Rejected alternative |
|---|---|---|---|
| 1 | **Spine first (2a), then core vision (3a), then finish the restructure (2b)** (revised after the builder check) | The code must match the diagrams, and anticipation must not be built on the old shape. If time runs out, both the design and the vision still show. | Full restructure before any vision work: risks a demo with no anticipation. Features first: fails rubric items 2–9. |
| 11 | Append-only `task_events` holds outcome history; `user_tasks.status` is current state only | Re-slotting keeps the evidence PEARL learns from; one meaning per signal; an Observer-friendly event stream | Status-only history: re-slotting erases what was learned |
| 12 | The learned focus profile is a soft solver objective for every plan, below TMT and hard rules, behind a flag | Makes "understands you" change the actual math; the flag and tests guard the working core loop | Brief-only: safer but hollow. A hard constraint: too brittle. |
| 2 | Ports are **ABCs** | `<<interface>>` and "implements" are literally true and enforced; easier to defend in a viva | `typing.Protocol`: structural, so the diagram's arrows aren't real in code |
| 3 | In-process asyncio `AnticipationScheduler` plus a manual `run` endpoint | No new dependency; a Template Method + Strategy design that is easy to explain; deterministic demo trigger | APScheduler or Celery: more moving parts, for nothing we need at this scale |
| 4 | Stay on Supabase cloud; `InMemory*` repositories for tests | Least change; the in-memory family demonstrates LSP and DIP | Local Postgres: no Docker on this machine, and supabase-py speaks PostgREST |
| 5 | Seeded history, live inference | Patterns need weeks of data; seeding is honest when inference is real | Hardcoded insights: an anti-goal |
| 6 | Consent defaults: read-only prep auto, schedule changes ask | Anticipation without loss of control; matches anti-guilt and agency | Fully autonomous: surprising and unsafe |
| 7 | `user_profiles` as the User entity, FK target for every `user_id` | Correct ER mapping without building auth | `auth.users`: no auth in scope |
| 8 | Adopt Claude Code *ideas* (consent chain with typed reasons, deny>ask>allow hook aggregation, retry with backoff, usage ledger, fail-closed step defaults), never its code | Better design and defensible patterns; avoids an integrity/IP risk | Porting TS code: an integrity risk and wrong language |
| 9 | Retire v1 and rename `/v2/stream` → `/stream` | Removes ~2k lines of dead-weight design debt | Keep deprecated: an examiner finds two architectures |
| 10 | **Claude replaces Gemini as the cloud tier; local stays first** (proposed, pending Madhav) | Guaranteed schema-valid structured output removes the biggest live-demo failure class (bad JSON → retry → degraded reply). Stronger persona and ask-back quality. Credits are already paid. About $10–30 total. Keeps the local-first privacy story. | Claude-only, dropping local: loses the privacy/local-first identity. Keeping Gemini as the cloud tier: cheaper, but weaker structured output and voice in the live demo. |
