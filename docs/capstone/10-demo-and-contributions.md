# 10 — Demo and contributions

This document is the team's playbook for the evaluation. Jarvis is a personal intelligence for one person: it keeps their work ready before they need it, learns from what actually happened (an append-only outcome history becomes patterns and a focus profile that change the solver's math), remembers what they say and forgets on request for good, and makes the first move each morning only with consent. Section 1 is the demo-day setup, with exact commands, the environment it needs and what to do when the cloud model or the network fails. Section 2 is a ten-minute demo script, minute by minute, with the exact text to type, what appears on screen (quoted from the seeded `demo` user as read from the running backend on 2026-11-14), the point to make and the rubric criterion it shows, plus a three-minute cut. Section 3 is a bank of viva questions with honest answers and the file to open. Section 4 maps every subsystem to its files and documents, with what its owner must be able to explain and the assigned owners. Section 5 lists the limits we state openly. File references are `path:line` at commit `d3e9132`, now part of `main` (frontend `4fa06ef`).

**Who presents what: Murali (introduction, the Meera story, requirements, architecture, the chat turn; anticipation, memory, CP-SAT and PEARL); Amathziah (sequence and activity, class and database diagrams); Ravi (API design, database schema, error handling); Pugal (OOP, SOLID, design patterns). Each person has a plate in `team/`. Detail in §4.**

---

## Contents

1. [Demo-day setup](#1-demo-day-setup)
2. [The demo script](#2-the-demo-script)
3. [Viva question bank](#3-viva-question-bank)
4. [Contribution map](#4-contribution-map)
5. [Known limits we will state openly](#5-known-limits-we-will-state-openly)
6. [How to verify this document](#how-to-verify-this-document)

Rubric criteria used below (spec §1): **C1** requirements, **C2** class diagram, **C3** OOP, **C4** SOLID, **C5** design patterns, **C6** sequence/activity diagrams, **C7** API design, **C8** DB schema, **C9** error handling, **C10** a working end-to-end implementation that reflects the design. Each member explains their own part (section 4).

---

## 1. Demo-day setup

### 1.1 The order, and why

Stop the backend → apply migrations only if there are new ones → seed the demo user → start the backend → run the live check → start the frontend. The seed refuses to run while a backend answers, because the backend's slot sweep runs on the real clock every minute and would set half-seeded tasks aside as if the user had (`scripts/seed_demo_user.py:35-37`). Seed **on the demo day**: the history, the current plate ("due over the next two to four days, one due today") and the memories' ages are all relative to today, and a reseed starts every memory at full strength (a stated memory now takes months to fade: 8 to 16 weeks of stability by type, `initial_stability` in `app/services/memory/memory_service.py`).

### 1.2 Checklist, in order

All commands run from `Jarvis-Engine/` unless stated.

**Shortcut:** `bash scripts/demo_day.sh` does steps 3 and 4, uploads the demo syllabus (`docs/capstone/demo/DBMS-Syllabus-CS304.pdf`) and asks for the morning brief, in one command. Run it on the morning of each demo day; the backend on this machine and every teammate's backend must be stopped first, because they share one database.

| # | Do | Command | Expect |
|---|---|---|---|
| 1 | Stop the backend | Ctrl-C in its terminal. If you cannot find it: `lsof -ti tcp:8000` then `kill <pid>` (a plain `kill` lets the lifespan stop the scheduler and drain the usage ledger, `app/main.py:173-182`). | `curl -s localhost:8000/health` fails to connect. |
| 2 | Check for new migrations | `supabase migration list` (the CLI is linked to the project) | Every local migration has a remote entry. The last one applied live is `20261115000007_user_tasks_not_before.sql`. |
| 2a | Only if a local migration is missing remotely | `supabase db push --include-all` | The CLI applies it. The memory migration prints a `SET LOCAL` warning; it was verified harmless when applied (ledger, plan 2). |
| 3 | Seed the demo user | `.venv/bin/python scripts/seed_demo_user.py --reset` | About 35 s (ledger). A report of roughly 21 days and 90 tasks, then the operator note. Exit code 0. |
| 4 | Start the backend, cloud model first | `CLAUDE_PRIMARY=true .venv/bin/uvicorn app.main:app --port 8000` | Startup lines (from `app/main.py:51-169`): `✅ Database connection successful: Supabase connected.`, `✅ Repositories: Supabase`, `CLAUDE  → claude-sonnet-5-5`, `✅ LLM providers live, in order: claude → …`, `CLAUDE_PRIMARY on`, `✅ Anticipation scheduler started: a tick every 60s`. |
| 5 | Run the live check | `.venv/bin/python scripts/live_e2e.py` | A pass/fail/skip table of steps 1–15 and the last line `ALL PASSED` (exit 0). It costs about ten chat turns (roughly 25–30 model calls) and runs as a throwaway `e2e-…` user whose rows it deletes; it never touches `demo` (`scripts/live_e2e.py:1-48`). Keep the output on screen for minute 9. |
| 6 | Start the frontend | `cd ../jarvis-frontend && npm run dev` | http://localhost:3000. `.env.local` must hold `NEXT_PUBLIC_API_URL=http://localhost:8000`, `NEXT_PUBLIC_USER_ID=demo`, `NEXT_PUBLIC_DEMO_MODE=false`. |
| 7 | Wait for the first tick | at least 60 s after step 4 | The first tick runs the sweeps and the triggers (`app/services/anticipation/scheduler.py:121-127`). The pattern-rule proposal ("A pattern around 9 AM") appears. The morning brief fires on a tick only between the wake time (07:00) and 13:00 on the demo user's clock (`triggers.py:120-125`; `config.py:196`); outside that window press **Check now** on the dashboard, which runs it on demand (`WhileYouWereAway.tsx:145`). |
| 8 | Pre-flight reads | `curl -s localhost:8000/health` · `curl -s "localhost:8000/api/v1/anticipation/suggestions?user_id=demo"` · `curl -s "localhost:8000/api/v1/you?user_id=demo"` | `{"status":"healthy","database":"connected"}`; two pending suggestions (brief, 9 AM rule); four patterns and `"source": "learned"` focus. |

What the seed does (`scripts/seed_demo_user.py:1-48`): every row is written by a live service under a simulated clock — each past day's plan is a draft accepted by `DraftService.accept` (which writes `user_tasks` and the `scheduled` events), outcomes go through `TaskService.complete` / `skip` just after each slot, the slot sweep sets aside what was left at 23:59, three things the user said are remembered by `MemoryService.remember` on their own dates, the evening before the demo the user accepts the current plate, and at the end PEARL looks at the history once. No model is called. It refuses any user but `demo`; it warms the embedder and probes the schema before deleting anything; `--reset` deletes only rows scoped to `demo`; a self-check confirms the history is the seed's alone. `--dry-run` seeds an in-memory store with no network.

Seed exit codes: **0** seeded, or already seeded (a no-op without `--reset`); **1** a partial seed, a failed self-check, or a reset that left rows; **2** a refused user, an unmigrated database or no embedder (nothing deleted); **3** a backend is running (`scripts/seed_demo_user.py:44-47`).

### 1.3 Required environment (names only; never print values)

List the names without values with `grep -o '^[A-Z_]*=' .env`.

| Where | Variable | Needed for |
|---|---|---|
| `Jarvis-Engine/.env` | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Every persisted row. The key must be the **service_role** key: with the publishable key, row-level security returns nothing (ledger, plan 1). |
| | `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) | Claude, the cloud tier. Without it Claude is left out of the chain (`app/infrastructure/llm/factory.py:52-53`). |
| | `CLAUDE_MODEL` (optional) | Default `claude-sonnet-5-5` (`app/core/config.py:51`). |
| | `GOOGLE_API_KEY` or `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini, the last-resort provider and the fallback for web search (Claude's search tool is asked first). |
| | `LOCAL_LLM_URL`, `GEMMA_PRIMARY_MODEL`, `GEMMA_FAST_MODEL` (pins optional) | The local tier (LM Studio). |
| | `CHROMA_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE` | Document search (not needed for the demo path). |
| | `JARVIS_DEFAULT_TIMEZONE` (default `Asia/Kolkata`), `JARVIS_ANTICIPATION` (default on), `JARVIS_ENERGY_OBJECTIVE` (default on), `JARVIS_SOLVER_MAX_TIME_SECONDS` (default 10), `JARVIS_CHECKPOINT_DB` | Behaviour switches (`config.py:164-233`). |
| Shell, at start | `CLAUDE_PRIMARY=true` | Every call skips the local model and goes to Claude first (`config.py:46-49`; `local.py:41-46`). It is not in `.env`. |
| `jarvis-frontend/.env.local` | `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_USER_ID`, `NEXT_PUBLIC_DEMO_MODE` | Where the backend is; the user (`demo` by default, `lib/types.ts:14`); demo mode off. |

### 1.4 Fallback plan

| What fails | What still works | What to do |
|---|---|---|
| **Claude** (credentials, credits, Anthropic unreachable) | The chain falls over by error type to the next provider: Gemini, then nothing (`app/infrastructure/llm/chain.py:87-95`). Gemini's prepaid credits were depleted during plan 1 (HTTP 402, ledger); its state since was not re-checked, so do not count on it. The solver, accept, task outcomes, PEARL, the You page, the consent chain and the morning brief keep working (the brief's replan calls a model only to translate a free-text habit that the deterministic parser cannot read). Chat extraction, decomposition and replies need a model; without one they degrade to an honest reply, never a 500. | Switch to the **local tier**: start LM Studio with a Gemma pair loaded, restart the backend **without** `CLAUDE_PRIMARY`. The lifespan detects the models (`app/main.py:94-108`) and every call goes local first, with Claude as fallback. Caveat: local PRIMARY latency against the 60 s call timeout was never measured (ledger, plan 1), and every recorded live run used `CLAUDE_PRIMARY=true`. |
| **The network** (Supabase is a cloud database) | The backend starts on in-memory repositories and says so; every data route answers 503 `persistence_unavailable` rather than showing empty or fake data (`app/api/deps.py:31-37`; `app/container.py:131-139`). Offline: the whole test suite (it blocks every non-localhost socket, `tests/conftest.py`), the solver through the tests, the layering and design-conformance checks, `scripts/seed_demo_user.py --dry-run` (prints the seeded history's report with no network), and the diagrams in `06`. | Present from the docs and the tests (section 2, minute 9), and from a screen recording of the morning's dry run. Record that recording during setup. |
| **Anticipation misbehaves** (a trigger failing every tick, a duplicate card) | With `JARVIS_ANTICIPATION=false` no tick runs: no slot sweep, no automatic brief or proposal (`app/main.py:162-169`). Suggestions already stored still list and can be accepted; **Check now** still runs the brief, because `POST /anticipation/run` calls the service directly (`app/api/v1/endpoints/anticipation.py:219-229`); a new pattern still produces a rule proposal through the event listener (`app/container.py:217-218`). | Restart the backend with `JARVIS_ANTICIPATION=false`. |
| **The energy term makes a plan look odd** | Plans are made on TMT alone. | Restart with `JARVIS_ENERGY_OBJECTIVE=false` (`config.py:204`; `app/services/energy.py:147-148`). |
| **A slow solve** | The best schedule found so far is kept at the cap. | Lower `JARVIS_SOLVER_MAX_TIME_SECONDS` (default 10). |
| **The live check fails a step** | Its one-line detail says why; `SKIP` is not a failure (`scripts/live_e2e.py:31-33`). | Fix or explain; re-run after a backend restart. |

---

## 2. The demo script

### 2.1 What the seeded user looks like (read live, 2026-11-14, about 21:30 IST)

Read with `GET /api/v1/you?user_id=demo`, `GET /api/v1/anticipation/suggestions?user_id=demo` and `GET /api/v1/tasks/?user_id=demo&status=pending`. The brief's wording depends on the hour it runs: the one below was made at 21:22 IST, so its hardest task lands in the evening. On demo day, after a fresh seed and a morning tick, the times will differ; the structure will not.

| Item | Live value |
|---|---|
| Profile | "Demo", Asia/Kolkata, wake 07:00, autonomy `ask_first`; streak 12 days |
| Brief card | Title "Today is ready". Body: "6 tasks are planned for today; the hardest, 'Revise Operating Systems lecture notes', is at 21:50. / 3 from yesterday found a new slot. / 4 from earlier today found a new slot. / I noticed you tend to set work aside around 9 AM (14 of the last 15 times)." Consent: ask, `mode_decision`, "This changes your schedule, so I'm asking first." |
| Rule proposal card | Title "A pattern around 9 AM". Body: "You've set aside 14 of the last 15 tasks around 9 AM. Want me to keep that hour clear?" Consent: ask, `mode_decision`. |
| Patterns (4) | "You tend to set work aside around 9 AM" (confidence 0.93, 15 outcomes; evidence "set aside Fri 13 Nov 09:00 · DSA practice: dynamic programming", "set aside Thu 12 Nov 09:00 · DSA practice: graphs", …) · "Your evenings around 7 PM go well" (0.93, 30; "done Fri 13 Nov 19:00 · Course project: fix the open bugs", …) · "You tend to finish close to deadlines" (1.0, 9; "done Fri 13 Nov 08:00 · OS assignment, part 1: process scheduling", …) · "You tend to set work aside around 11 AM" (0.71, 7) |
| Focus profile | Learned from 98 outcomes. Peak 19:00 (score 0.90: 28 done, 2 set aside), then 20:00 (0.88: 14 done, 1 set aside). Lowest observed hour 09:00 (0.21: 1 done, 14 set aside). |
| Memories (3, all "you said") | "I think most clearly in the evenings, after about 7 PM." (preference) · "My OS assignment is due every Friday." (fact) · "I'm preparing for placement interviews, so DSA matters most this semester." (goal) |
| Rules | none yet |
| Privacy receipt | 0 on-device, 0 cloud calls, 0 redactions over the last 30 days (the demo user has not chatted yet) |
| Pending tasks | 4, e.g. "Mock interview: graph traversal problems" (difficulty 0.9), "OS assignment: page replacement simulator" |

### 2.2 Ten minutes

The demo order matters in two places: accept the brief **before** the rule proposal (accepting a plan supersedes every other open draft, so the rule's re-plan draft must come last), and show forget-and-say-again with a **stated fact**, the safer default: it reads the same whatever was planned before. A stated rule works too (L11, resolved): forget the 5:30 rule's memory on You → Memories (the rule goes with it), then say the rule again in a new chat; the reply is "Understood, sir.", no learning note appears and You → Rules stays empty. Use the rule variant only after its re-plan has been shown.

| Min | Do (exact clicks and text) | What appears | The point, in one sentence | Criterion |
|---|---|---|---|---|
| 0:00 | Open http://localhost:3000/dashboard. | "While you were away" with the **Today is ready** card (body as in 2.1), its consent line "This changes your schedule, so I'm asking first.", and the **A pattern around 9 AM** card. Stat tiles: streak "12 days", Patterns 4. | Jarvis made the first move while the user was away, from facts and with no model call, but anything that changes the schedule waits for consent. | C10, C5 (Template Method, Chain of Responsibility) |
| 1:00 | Click **Accept** on "Today is ready". | Toast "N tasks are on today's plan." (`lib/you.ts:175-181`); Today's Schedule fills. If a slot had already begun, the backend drafted the open tasks again from now and accepted that plan instead (its result message: "I refreshed the plan first — it's in place now."). | Accepting checks the plan is still current, then writes it in one database transaction; a second accept is a 409. | C9, C8 |
| 1:45 | Click **Accept** on "A pattern around 9 AM"; then **Lock it in** on the banner. | "Saved. I've drafted a plan that keeps 9 AM clear — accept it when it suits you." The banner shows the new draft's times; Lock it in writes it. | A pattern PEARL found in the outcome history became a rule only after the user said yes, and the rule is linked to the pattern it came from. | C5 (Observer, Command), C10 |
| 2:30 | Open **You** (/you). Point at each section. | **Patterns** with evidence (2.1). **Focus through the day**: learned, peak at 19:00. **Rules**: "No work between 09:00 and 10:00". **Memories**: the three "you said" items with freshness. **How Jarvis works with you**: ask first, wake 07:00. **Privacy receipt**: "0 on this device · 0 sent to the cloud · 0 redactions" before any chat; revisit after minute 5 to show the cloud calls counted. | Seeded history, live inference: every number on this page was computed just now from the append-only outcome history, and the focus profile is also a term in the solver's objective. | C10, C8, C1 |
| 3:45 | Open **Chat**, **New Chat**, type: `I have a data structures exam on Friday. Plan my revision: graphs, dynamic programming and trees, with some mock problems.` | Phase steps (loading context, brain-dump extraction, intent PLAN_DAY, the planning steps from validate_goal to create_draft), the streamed reply, a schedule preview with **Accept All**. 09:00–10:00 stays empty. | One fast extraction call, one heavy decomposition, then CP-SAT; nothing is written to the task list until the user accepts. | C6, C7 (SSE contract), C10 |
| 5:00 | Type: `looks good` — then type `looks good` again. (Stay in chat: the card's **Accept All** also accepts, but then moves the page to Schedule.) | First: "Locked in, sir — N tasks are on your schedule." Second: "That plan is already locked in, sir." | The second accept wrote nothing: the database function, not the UI, guarantees it (06 S2a). | C9 |
| 5:30 | **New Chat**, type: `From now on never schedule any study before 11 AM, I'm useless in the mornings.` | "Noted and locked in, sir: “…”. Every schedule I draft from here on will respect it." and a learning note under the reply (the `memory_extracted` frame). | The rule is stored as a rule and remembered as something the user said, the two linked so that forgetting one forgets both. | C10, C5 |
| 6:15 | **New Chat**, type the plan prompt from 3:45 again. | A new draft with no task between 08:00 and 11:00 on any day. | A taught rule is a hard block for the solver on every day of the horizon. | C10, C6 |
| 7:00 | Type: `Something about me: I'm a night owl and do my best work after 9 pm.` Then **You → Memories →** Forget on that memory (two clicks). Then **Chat → New Chat**, type the same sentence again. | A learning note appears the first time. Toast "Forgotten. Jarvis won't store it again." The second time: no learning note, and the memory is not listed on You. | Forget is a tombstone: the words are erased but the embedding stays, so the same fact said again is recognised and not stored. | C9 (state machine), C8 |
| 8:15 | Open **Schedule** (/schedule). **Complete** one task (rate it), **Skip** another. | The skip note: "Life happened — '…' is off the list for now…". | Each outcome is one append-only event; PEARL re-reads the history in the background and a changed pattern arrives as a learning moment on the next chat turn. | C8, C5 (Observer) |
| 8:45 | In a terminal: `.venv/bin/python -m pytest -q tests/test_draft_service.py::test_accept__concurrent_double_click__exactly_one_succeeds tests/test_migrations.py::test_migrations__accept_draft__locks_the_user_before_the_draft tests/test_layering.py tests/test_design_conformance.py`. Show the live-check output from setup step 5. | 28 tests pass in about 5 s (run while writing this document). The live table shows "3 double accept -> 409 … 409, user_tasks unchanged" and ends `ALL PASSED`. | The design is checkable: the double click is proven against the real accept path, the class diagrams and the layering rule are tests, and the whole loop passed against the real database and model this morning. | C2, C4, C9, C10 |

### 2.3 Three-minute cut

| Min | Do | Point | Criterion |
|---|---|---|---|
| 0:00 | Dashboard: read the brief card and its consent line aloud; **Accept**. | First move, under consent; atomic accept with a freshness check. | C10, C5, C9 |
| 0:45 | You: one pattern with its evidence, the focus chart's 19:00 peak, the privacy receipt. | Learned from what happened; seeded history, live inference. | C10, C8 |
| 1:30 | Chat (text pre-typed): the "never before 11 AM" rule, then the plan prompt; show nothing before 11:00. | Memory changes the math. | C10, C6 |
| 2:30 | Terminal: the live-check table ending `ALL PASSED`, and `tests/test_layering.py tests/test_design_conformance.py` passing. | The design is checked, not claimed. | C2, C4, C10 |

---

## 3. Viva question bank

Answers are short on purpose; open the file named to go deeper.

### 3.1 Design and architecture

| # | Question | Answer | Open |
|---|---|---|---|
| 1 | Why are the ports ABCs and not `typing.Protocol`? | So that `<<interface>>` and "implements" in the class diagram are literally true: an adapter subclasses the port, an incomplete one cannot be instantiated, and `tests/test_design_conformance.py` checks that every `<<interface>>` in the class diagrams is an `abc.ABC` and every realization arrow a real subclass (ADR-2). A Protocol is structural, so nothing in the code would say a class implements it. One honest exception: the redaction port `Redactor` is a small Protocol inside the LLM decorators. | `app/domain/ports.py`; `app/infrastructure/llm/decorators.py:46-53` |
| 2 | How do you keep the domain clean of frameworks? | `app/domain/` may import only the standard library and pydantic; a test parses every import and fails on anything else, in any spelling. | `tests/test_layering.py:74-126` |
| 3 | Where are concrete classes chosen? | Only in the composition root, `AppContainer.build`: the repository family, the event bus and its listeners, the model chain, every service. A test fails if anything else constructs a repository. | `app/container.py:109-265`; `tests/test_layering.py::test_layering__only_container_builds_repositories` |
| 4 | Why one chat endpoint? | Two architectures in one codebase was the biggest design debt; the old v1 flow was retired (ADR-9) and `/api/v1/chat/stream` is the only way in. The router is routing only; the turn is `ChatTurnService`. The old `/v2/stream` path is gone (404): nothing calls it. | `app/api/v1/endpoints/chat.py`; `app/services/chat_turn/service.py` |
| 5 | How would you add a new trigger? | Subclass `Trigger`, implement `should_fire`, `gather` and `build_suggestion` (the template's steps), and register it in `AppContainer.build`; a new action kind goes into `build_action_factory`. The API lists and runs it with no edit. | `app/domain/anticipation.py:342-439`; `app/container.py:208-216`; `tests/test_anticipation_api.py::test_anticipation_api__run_a_newly_registered_trigger__no_api_edit_needed` |
| 6 | How would you add a new pattern detector? | Subclass `PatternDetector` (its name, `detect`, how planning applies it, whether it may become a rule, its words) and add it to `default_pattern_detectors`; no other code changes. | `app/services/memory/patterns.py:148-185`; `app/container.py:70-77`; `tests/test_patterns.py::test_detector__new_strategy__brings_its_own_insight_and_planning_use` |
| 7 | How would you add a new model provider? | Implement the `LLMProvider` ABC and pass it to `LLMProviderFactory.build`; the factory wraps every cloud provider in redaction, retry, metering and timeout, so the new one inherits them. | `app/infrastructure/llm/factory.py:21-56` |

### 3.2 Planning and the solver

| # | Question | Answer | Open |
|---|---|---|---|
| 8 | Why CP-SAT instead of letting the model schedule? | A model hallucinates overlaps and ignores constraints; CP-SAT gives a schedule that satisfies every hard constraint (no overlap, dependencies, blocked hours, the night block, the daily cap) or proves there is none. It is capped at 10 s and keeps the best schedule found. | `app/core/or_tools/solver.py:1-10, 251-330`; `app/services/planning/scheduling.py:174-351` |
| 9 | How does the focus profile change the math? | A soft objective term: for each task not due within 24 h, `900 × difficulty × penalty(hour)`, where the penalty is `10 × (1 − focus score)`. Hard work leans toward the learned peak; urgency (TMT) and hard blocks always win; `JARVIS_ENERGY_OBJECTIVE=false` turns it off; the prior never steers the solver. | `scheduling.py:95-111, 274-276, 312`; `app/services/energy.py:137-154` |
| 10 | What happens when nothing fits? | INFEASIBLE is a scope problem, not the user's failure: the horizon widens 3 → 5 → 7 → 14 → 30 days; if nothing fits, the reply is the scope message verbatim and nothing is proposed. A solve that times out says so honestly. | `app/modules/planning_graph.py:779-821` |
| 11 | Why can there never be two heavy local model calls at once? | Structurally (the decomposition is an AND-join that fires once) and at run time (a PRIMARY lock around any PRIMARY call that may run locally; a second call inside the same hold is refused, never deadlocked). | `app/core/model_router.py:195-308` |
| 12 | How is the user's timezone handled? | Minute 0 of every plan is 08:00 on the user's clock (profile timezone, else Asia/Kolkata); "never before 11 AM" is minutes 0–180; before 08:00 the logical day is yesterday, and from 22:00 a new plan starts tomorrow. | `app/services/planning/local_time.py`; `tests/behaviour/test_local_day.py` |

### 3.3 Accept, concurrency and data integrity

| # | Question | Answer | Open |
|---|---|---|---|
| 13 | How is accept atomic? | One call to the Postgres function `accept_draft()`, which runs as one transaction: check the draft is open, delete the pending tasks, insert the new rows, mark the draft accepted, supersede the user's other open drafts. They land together or not at all. | `supabase/migrations/20261115000001_accept_draft_reslots_missed.sql`; `app/services/draft_service.py:185-226` |
| 14 | What happens on a double click? | Both requests pass the fail-fast check in Python; in the database the first locks the user's profile row then the draft row, the second waits, then reads `accepted` and raises `DRAFT_NOT_OPEN`, which becomes `InvalidStateTransition`, a 409 envelope. Tasks are written once. | `06-sequence-and-activity.md` S2a; `tests/test_draft_service.py::test_accept__concurrent_double_click__exactly_one_succeeds` |
| 15 | Why lock the profile row before the draft? | The function also updates the user's other drafts, so locking the draft first lets two accepts deadlock. `FOR NO KEY UPDATE` does not block inserts that reference the profile; a row lock cannot collide across users the way a hashed advisory lock can. | `…user_tasks_not_before.sql:42-62` |
| 16 | What if the draft changed after the user looked at it? | The review banner sends the draft's `version` (a hash of its tasks and slots); a changed draft is 409 `stale_version` with the latest draft in the details. The chat card sends the version the `complete` frame carried (`draft_version`). | `app/domain/entities.py:359-368`; `app/api/v1/endpoints/drafts.py:114-119` |
| 17 | What if the user accepts hours late? | Slots that have begun are re-solved from now with every rule and the learned focus, then accepted (`moved: true`). If nothing fits: 422 and the draft stays open. Accepting stale slots would let the sweep "set aside" tasks the user never missed, poisoning what PEARL learns. | `app/services/draft_actions.py:61-99` |
| 18 | How do you prevent cross-user access? | Every repository method takes `user_id` and filters on it; another user's draft or memory reads as absent (404, never 403). `accept_draft` forces the caller's id onto every row. Checkpoint threads are `user_id:session_id`. Row-level security with owner policies is on every user table, but the backend uses the service role, which bypasses it, and there is no real authentication: `user_id` is a query parameter (L4). | `app/infrastructure/persistence/supabase_repos.py`; `app/orchestrator/checkpoint.py:292`; `tests/behaviour/test_chat_turns.py::test_chat__two_users__neither_sees_nor_accepts_the_others_draft` |
| 19 | How do two browser tabs not corrupt one conversation? | A per-conversation lock is held for the whole turn; history, memory and the checkpoint are read under it; a turn queued more than 120 s gets a conflict frame; cleanup is shielded so a disconnect cannot release the lock early. | `app/services/turn_locks.py`; `service.py:224-237` |
| 20 | What is process-local, and what breaks with two workers? | The turn locks, the PRIMARY lock, the anticipation per-user lock with its day memo, backoff and cooldown, the single-flight PEARL and proposal runs, the learning moments held for the next turn, the compaction lock, the habit-translation cache, the document-read lock, the web-search cooldown, the scheduler loop itself and the SQLite checkpoint file. With two workers: turns could interleave, two morning briefs could be made, two heavy model loads could run, a learning moment could be held by the wrong worker. Everything that would corrupt data is guarded in the database (the accept transaction, status compare-and-set, unique keys), so the damage is duplication, not loss. | `09-error-handling.md` §4.7 |

### 3.4 Memory, learning and consent

| # | Question | Answer | Open |
|---|---|---|---|
| 21 | Why is seeded history honest? | Every seeded row is written by the live services under a simulated clock, and nothing the app infers is seeded: patterns, the focus profile and the brief are computed live from that history. "Seeded history, live inference." A test checks every row shape is one the live app writes. | `scripts/seed_demo_user.py:1-27`; `tests/test_seed_demo_user.py::test_seed__every_row_shape_is_a_live_shape` |
| 22 | How is forget permanent? | The memory becomes a tombstone: status `forgotten`, words erased, embedding and pattern key kept. Every new memory is first compared with the let-go ones, of any type; a close match is suppressed. FORGOTTEN has no exits in the transition table, and the write is a compare-and-set, so a forget landing mid-write still wins. Rules linked to it are deleted, and a rule said again is not stored again (`MemoryService.drop_let_go`, checked by `store_constraint`). | `app/services/memory/memory_service.py:197-229, 304-320, 451-461`; `app/services/memory/stated_rules.py:80-97`; `app/orchestrator/graph.py:469-489` |
| 23 | What does PEARL actually detect? | Four strategies over the last 28 days of outcomes (`FOCUS_WINDOW_DAYS`), one outcome per slot: an hour set aside 70%+ of 3+ times, the single best hour (80%+ done), finishing close to deadlines, and one task skipped repeatedly. Each becomes an inferred memory keyed by pattern; confidence is capped at 0.9 because an inference is never certain. | `app/services/memory/patterns.py:68-76, 148-352`; `app/core/config.py` (`FOCUS_WINDOW_DAYS`) |
| 24 | How does a learning moment reach the screen before `complete`? | The observation loop runs inside the graph: it emits `memory_extracted` and `pattern_detected` frames onto the turn's queue before the graph ends, and drains moments PEARL learned in the background since the last turn. | `app/core/observation.py:437-503` |
| 25 | What does the consent chain do? | Chain of Responsibility, first decisive rule wins: dismissed before → deny; read-only → allow; already covered by a rule the user stated → deny; otherwise the autonomy mode decides (suggest only and ask first ask; auto low-risk allows only low-risk actions). Each decision carries a typed reason the card shows. An allowed change runs at once as a claimed suggestion. | `app/domain/anticipation.py:115-241, 353-421` |
| 26 | Why does the brief never blame the user? | Its words are put together from facts with no model call: a passed slot is "set aside" and "found a new slot"; "your strongest hour" is said only when the learned profile peaks there. A test greps the copy for blame. | `app/services/anticipation/triggers.py:227-300`; `tests/test_triggers.py::test_brief__copy__no_blame_phrases_and_peak_claim_only_when_learned` |
| 27 | How is "missed" decided without asking? | Infer, never ask: a sweep every minute marks missed any pending task whose slot ended more than 30 minutes ago with no outcome since it began, as a compare-and-set held to that slot. | `app/services/task_service.py:170-217` |
| 28 | Why an append-only `task_events` table as well as a status? | The status is only the current state. Re-slotting a missed task deletes its row and re-inserts it under the same `task_id` inside `accept_draft()` (one transaction), so the row's history is gone; without the events the evidence PEARL learns from would be erased (ADR-11). | `08-database-schema.md` §3.4 |

### 3.5 Models, privacy and cost

| # | Question | Answer | Open |
|---|---|---|---|
| 29 | How is PII handled before the cloud? | Every cloud provider is wrapped, outermost, by `RedactingProvider`: the `PreCloudLLM` hook replaces emails and US-style phone numbers with `[EMAIL]` / `[PHONE]` in the prompt, the system prompt and every history message, and counts redactions for the privacy receipt. A hook that denies stops the send. Honestly: it is regex only; names and addresses are not caught (L7). | `app/infrastructure/llm/decorators.py:79-129`; `app/orchestrator/hooks.py:59-87` |
| 30 | How does prompt caching work here? | The system prompt goes out as one block marked `cache_control: ephemeral`, and is kept byte-identical; everything that changes per turn (memories, plate, "since we last spoke", the time) travels as one context note folded into the last user message. So the system prompt and the earlier turns form a stable prefix the provider can read back from its cache instead of billing it at the full input rate. Cache reads are metered (`cache_read_tokens`); a live run recorded 1,398 cached tokens on a second reply. Shorter prompts than the model's minimum cacheable length silently do not cache, which is why the chat prompts are kept long. | `app/infrastructure/llm/claude.py:165-180, 230-238`; `app/infrastructure/llm/messages.py:12-32`; `tests/test_prompt_layout.py` |
| 31 | What happens when the model returns bad JSON? | Claude uses structured outputs, so the reply fits the schema by construction; a reply that still fails is `LLMOutputParseError` and the chain moves to the next provider (never retried); extraction that fails leaves the turn as chat, never a 500. | `claude.py:115-145`; `app/infrastructure/llm/chain.py:87-95` |
| 32 | How do you know which model answered? | The metering decorator records provider and model for each answered call (including Anthropic's server-side fallback model when it served); the reply is credited to the model that wrote it, and the ledger keeps every call, billed failures included. | `decorators.py:216-306` |

### 3.6 Errors and where it falls short

| # | Question | Answer | Open |
|---|---|---|---|
| 33 | Why one error envelope? | Every error is a `JarvisError` with a stable code and HTTP status, rendered as `{"error": {code, message, details, request_id}}` by global handlers, and the same shape inside the stream's `error` frame; exception text never reaches a client. | `app/domain/errors.py`; `app/api/error_handlers.py`; `tests/test_api_contract.py::test_endpoints__raise_no_http_exception` |
| 34 | What do the offline tests prove, and what does the live script add? | The offline suite (2361 passed, 2 xfailed, 2 deselected at `fe74172`) runs the real services, the real CP-SAT and both repository families over fakes and a scripted model, with network blocked. The live script drives the running backend against real Supabase and Claude as a throwaway user. | `tests/`; `scripts/live_e2e.py` |
| 35 | Where does the design fall short? | Section 5: ask-back, PreTaskPrep and DeadlineRisk not built; process-local guards; no authentication; regex PII; six legacy raw-table leftovers; two orchestrator branches never taken. | Section 5 |
| 36 | If you had another week? | From the ledgers' parked items: repositories for sessions and calendar rows (removing the raw-table allow-list), a `local_day` unique index for the once-a-day brief, a version on the chat accept, real authentication, and a real PII gateway. | `.superpowers/sdd/…/progress.md` |

---

## 4. Contribution map

Two views of the same team. **4.1** says who presents which part of the evaluation. **4.2** says who knows which part of the code.

### 4.1 Who presents what

| Owner | Presents | Documents | Slides | Own design patterns | Plate |
|---|---|---|---|---|---|
| **Murali** | The introduction and the Meera story; requirements; architecture; the chat turn; **anticipation, memory management, the CP-SAT solver and PEARL** | guide §1–4; `01`; `02`; `06` S1; the table below | 1–6, 11, 12, 13 | Strategy, State | `team/murali/` |
| **Amathziah** | Sequence and activity diagrams; the class diagrams; the database diagrams (ER diagram and the state machines) | `06`; `03`; `08` (diagrams) | 6, 7, 15 | Observer, Template Method | `team/amathziah/` |
| **Ravi** | API design; the database schema (tables, keys, constraints, `accept_draft`); errors, edge cases and concurrency | `07`; `08`; `09` | 14, 15, 16 | Repository, Adapter | `team/ravi/` |
| **Pugal** | OOP; SOLID; the design patterns overview | `04`; `05` | 8, 9, 10 | Singleton, Factory | `team/pugal/` |

Each person presents their own two patterns in depth; Pugal gives the overview of all of them on slide 10. Decorator, Chain of Responsibility, Facade and Command are not assigned: anyone answers them from `05` §0. "Singleton" here means one instance built once at the composition root (`AppContainer`, `get_hooks()`, `get_chain()`), not a class with `getInstance()`; `05` §13 says why.

`team/where-everything-is.md` is the one-page map of the code for every OOP pillar, SOLID principle and pattern, and says which pattern supports which SOLID principle.

Slides 17 to 22 (models, working software, proof, decisions) are shared: whoever built the part speaks to it. The deck has no team slide; the team's names are on the title slide and this section is the record.

**Planning, CP-SAT and PEARL (Murali): where each is written up.**

| Topic | Read | Code |
|---|---|---|
| Planning: a brain dump becomes a draft | guide §3 step 6; `01` §4.1 and UC1; `06` S1 and A2 (the planning sub-graph) | `app/modules/planning_graph.py` |
| The CP-SAT solver: hard and soft blocks, TMT priority, the energy term, the horizon ladder | guide D20; `01` FR-4 and NFR-1; `09` §3.3; viva answers in §3.2 of this document; slide 13 | `app/core/or_tools/solver.py`, `app/services/planning/scheduling.py` |
| The user's clock: 08:00 day start, 22:00 cutoff | guide D21 | `app/services/planning/local_time.py` |
| A rule taught in chat, then a re-plan | `01` §4.3 and UC3; `06` S3 | `app/services/memory/stated_rules.py` |
| PEARL: the four pattern detectors and their thresholds | guide D16; `01` FR-25 and UC7; `03` §8.2; `05` §3 (Strategy); `06` S4 and S7 | `app/services/memory/patterns.py` |
| The focus profile, and how it reaches the solver | guide D20 (the energy term); `01` FR-30 and FR-31; `03` §8.2 | `app/services/energy.py` |
| Rule check-ins (spaced repetition) | guide D33 | `app/services/analytical/rule_check_ins.py` |

### 4.2 Who knows which code

Four team members share thirteen subsystems.

| Subsystem | Key files | Covered in | The owner must be able to explain | Owner |
|---|---|---|---|---|
| **Domain and ports** | `app/domain/entities.py`, `ports.py`, `events.py`, `errors.py`, `anticipation.py`, `clock.py` | 03, 04, 05, 09 | • Why ports are ABCs, and which class implements each. • The draft, memory and suggestion transition tables (State) and `DraftSchedule.transition`. • The `EventBus` (Observer) and the `JarvisError` hierarchy (code → HTTP status). | Pugal |
| **Persistence and schema** | `app/infrastructure/persistence/{supabase_repos,in_memory,_mapping,factory}.py`, `supabase/migrations/*`, `tests/test_repository_contracts.py`, `tests/test_migrations.py` | 08, 09 §4, 05 §2 | • `accept_draft()`: one transaction, lock order, what each error maps to. • Two repository families, one contract suite (LSP), and who chooses the family. • Keys, FKs, RLS, `user_id` on every query, bounded reads under the 1000-row cap. | Pugal |
| **LLM tier** | `app/core/model_router.py`, `app/infrastructure/llm/*`, `app/orchestrator/hooks.py` (PreCloudLLM), `app/services/usage_ledger.py` | 06 S8, 05, 09 §3.1 | • Task → role → chain; fall-over by error type; `CLAUDE_PRIMARY`. • The decorator order and why (redact once, retry transient only, metering outside the timeout). • Prompt-cache layout, the PII gate and its limits, the PRIMARY lock. | Amathziah |
| **Chat turn and orchestration** | `app/api/v1/endpoints/chat.py`, `app/services/chat_turn/*`, `app/services/turn_locks.py`, `app/orchestrator/{graph,routing,state,checkpoint,module_wrapper}.py`, `app/core/module_framework.py`, `app/schemas/chat_stream.py` | 06 S1, A1; 07 §6; 09 §4.3 | • A turn from open to `complete`: lock, conversation read, graph pump, frames, shielded cleanup. • The graph's nodes and conditional edges, the negotiation shortcut, draft verbs by regex. • The SSE contract (golden tests) and the user-scoped checkpoint with transient keys. | Amathziah |
| **Planning and solver** | `app/modules/planning_graph.py`, `app/modules/planning_replan.py`, `app/services/planning/*`, `app/core/or_tools/*`, `app/services/analytical/habit_translator.py`, `schedule_modifier.py` | 06 S1, S2b, S3, A2; 01 UC1, UC3 | • The sub-graph steps, the AND-join, the horizon ladder. • What `run_schedule` enforces: hard and soft blocks, TMT, the adaptive cap, the night block, the energy term below TMT. • The user's clock: minute 0, the evening cutoff, "never before 11 AM" as minutes 0–180. | Murali |
| **Tasks and outcome history** | `app/services/task_service.py`, `draft_service.py`, `draft_actions.py`, `app/services/planning/task_rows.py`, `app/api/v1/endpoints/{tasks,drafts}.py` | 06 S2a, S2b, S4; 08 §3.3–3.5; 09 §4.1–4.2 | • Accept as the only write path, and the double click. • One append-only event per outcome, compare-and-set on status, the slot sweep. • Late accept re-solve and `expected_version`. | Murali |
| **Memory** | `app/services/memory/{memory_service,conversation_memory,context,stated_rules,constraint_bridge,since_last_spoke,summary_text}.py`, `app/core/observation.py` (memory half), `app/api/v1/endpoints/memories.py`, `app/infrastructure/embedding.py` | 06 S3, S5; 08 §3.8; 01 UC5 | • `remember`: suppress, supersede, reinforce, create. • Forget as a tombstone, and the rule ↔ memory link. • Recall within token budgets and the context note's cache-stable layout. | Amathziah |
| **PEARL and focus profile** | `app/services/memory/patterns.py`, `app/services/energy.py`, `app/core/observation.py` (PEARL half), `app/services/user_model_service.py` | 06 S4; 05 (Strategy, Observer) | • The detectors, one outcome per slot, the 3 / 0.7 thresholds, confidence capped at 0.9. • The background observer (single flight) and moments held for the next turn. • The focus profile: prior vs learned, and how it enters the solver. | Murali |
| **Anticipation and consent** | `app/services/anticipation/*`, `app/domain/anticipation.py`, `app/api/v1/endpoints/anticipation.py` | 06 S6, S7, A3; 05 (Template Method, Chain of Responsibility, Command) | • The scheduler tick, the per-user lock, the day memo and backoff. • `Trigger.run` and the two triggers built. • The consent chain, claim-then-execute, and the brief's freshness check on accept. | Ravi |
| **API and errors** | `app/api/{error_handlers,deps,sse}.py`, `app/api/v1/endpoints/*`, `app/schemas/*`, `app/main.py` | 07, 09 | • One envelope with a request id; no exception text on the wire. • A typed response model on every route; the OpenAPI document. • The `CurrentUser` seam and `require_supabase` (503, never fake data). | Pugal |
| **Frontend: dashboard, You, chat** | `jarvis-frontend/app/(app)/{dashboard,you,chat,schedule}/page.tsx`, `components/app/{WhileYouWereAway,PendingDraftBanner,LearningMoments,SchedulePreview}.tsx`, `components/app/you/*`, `lib/api.ts`, `lib/hooks/useJarvisChat.ts` | 07 §7; 10 | • How the chat page consumes the SSE frames and validates them. • 409 handling, single-flight buttons, `expected_version` on Lock it in. • Honest empty vs error states; the consent reason on each card. | Ravi |
| **Tests, live checks and seed** | `tests/conftest.py`, `tests/fakes.py`, `tests/behaviour/`, `tests/golden/`, `tests/test_layering.py`, `tests/test_design_conformance.py`, `scripts/live_e2e.py`, `scripts/seed_demo_user.py` | 10; every "How to verify" | • The offline suite: socket guard, fakes, scripted model, real CP-SAT, strict xfails. • The seed: live services under a simulated clock, exit codes, why the backend must be stopped. • The live script's steps and its cleanup of the throwaway user. | Ravi |
| **Docs** | `docs/capstone/*`, `docs/PROJECT_STATUS.md`, `docs/superpowers/specs/2026-11-14-capstone-lld-and-anticipation-design.md` | all | • Traceability: requirement → code → test. • How the diagrams are checked against the code. • The known limits and where the spec was amended. | All four |

Code areas: Pugal has Domain, Persistence, API and errors; Amathziah has the LLM tier, Chat turn and Memory; Murali has Planning, Tasks and PEARL; Ravi has Anticipation, Frontend, Tests and seed. Docs are shared: each owner keeps their sections current.

---

## 5. Known limits we will state openly

| # | Limit | Source |
|---|---|---|
| L1 | **In-process guards.** The turn locks, PRIMARY lock, anticipation lock with its day memo, backoff and cooldown, single-flight runs, held learning moments, compaction lock and the scheduler loop are per process. Jarvis runs as one process on one machine; a second worker would duplicate work and could make a second morning brief (the fix noted: a `local_day` column with a unique index). Data-corrupting cases are guarded in the database. | Ledger plan 2 (once-a-day guard "process-local"); `09-error-handling.md` §4.7 |
| L2 | **Legacy leftovers on the layering allow-list.** Six files still make raw table calls with stated reasons: `workspace_builder.py`, `user_preferences.py`, `chat_history.py`, the sessions and ingestion endpoints, and `extraction/orchestrator.py` (Plan 3 cut the sessions and calendar repositories; documents and their task links have `DocumentRepository`). `core/model_router.py` is the one allowed import of infrastructure. | `tests/test_layering.py:146-151, 514-531`; ledger plan 3 |
| L3 | **Not built:** ask-back (the deterministic clarify gate); the PreTaskPrep and DeadlineRisk triggers; the research module's summary (it reports a result count, `app/modules/research_graph.py:68-70`); the planned `CognitiveModule` registry (Plan 3 Task 9 dropped). | Spec §7, §8.3 (amended); ledger plan 3 |
| L4 | **No authentication.** The caller is a `user_id` query parameter (the `CurrentUser` seam); RLS owner policies exist but the backend's service role bypasses them. | `app/api/deps.py:16-28`; spec non-goal |
| L5 | **The version check has a window:** a draft edit landing between `DraftService` reading a draft and the accept call is not detected (both the review banner and the chat card send `expected_version`). | Ledger plan 3 (parked); `09-error-handling.md` §4.1 |
| L6 | **Hands-on browser pass never done.** The browser extension did not respond; pages were checked with headless renders and API reads, and "Check now" was never clicked live. | Ledger plan 2 |
| L7 | **PII gate is regex only** (emails, US-style phone numbers). | `app/orchestrator/hooks.py:59-87` |
| L8 | **Hooks:** seven handlers are registered; `PreCloudLLM` and `PreModuleExecution` are the ones called on the live path. | `app/orchestrator/hooks.py:130-137`; `app/core/model_router.py:84`; `app/orchestrator/module_wrapper.py:23` |
| L9 | **Model availability:** Gemini's prepaid credits were depleted during plan 1 (402) and not re-checked; local PRIMARY latency against the 60 s timeout was never measured; every recorded live run used `CLAUDE_PRIMARY=true`. | Ledger plan 1 |
| L10 | **Two orchestrator branches are never taken:** the `observation_loop → classify_intent` follow-up edge (nothing sets `needs_followup`), and `route_to_module`'s NEGOTIATION and planning-error rules (no writer / always before any module). | `06` A1; `app/core/observation.py:454, 460, 494`; `app/orchestrator/routing.py:37-51` |
| L11 | **Resolved since this list was written:** a rule said again after its memory was forgotten is no longer stored again; `store_constraint` checks the tombstones first (`MemoryService.drop_let_go`). Found while writing `06`. | `app/orchestrator/graph.py:469-547`; `tests/behaviour/test_learning.py::test_forgotten_rule__said_again_alone__nothing_stored_and_no_failure_claimed` |
| L12 | **Rearranging a draft does not re-solve it** (`needs_resolve: true` is returned), and draft-edit re-solve paths build their constraints from habit text only. | `app/api/v1/endpoints/drafts.py:252-259`; ledger plan 2 |
| L13 | **Evening plans start tomorrow.** From 22:00 local a new plan starts at 08:00 the next day; a late-evening demo shows tomorrow's times. | `app/core/config.py:173-175`; ledger plan 1 ruling |
| L14 | **Resolved: Jarvis checks in on every rule it learned (SM-2).** Each rule has one tracker (`RuleCheckIns`). A rule stated in chat, given through ingestion or adopted from a pattern starts its check-ins at once (best effort: the rule is stored either way), first due the next day; a rule older than its tracker gets one when the Habits page is read, counting from when it was learned, so it is due already. Each answer to "Still how you work?" (0–5) spaces the next check-in out (1 day, 6 days, then the interval times the easiness); an answer under 3 brings it back the next day. Deleting or forgetting a rule deletes its tracker first, and a tracker whose rule is gone is never listed. The Habits page shows the due rules as cards and the rest under "Not due yet"; the demo seed leaves two due and one for tomorrow. What remains: the database does not enforce one tracker per rule (no unique key, and no DDL access), so the repository reads before it inserts and a per-user lock serialises it within the one server process; two processes racing could leave a second tracker, which is never listed twice. A low answer offers to forget the rule, never to reword it. A rule past its validity window keeps its tracker, unlisted. | `app/services/analytical/rule_check_ins.py`; `app/domain/ports.py` (`HabitTrackerRepository.add_for_constraint`, `delete_for_constraint`); `tests/behaviour/test_rule_check_ins.py`; `07-api-design.md` §5 (habits) |
| L15 | **Resolved: the uploaded file is kept and opens.** Ingestion keeps it in a private Supabase Storage bucket (`jarvis-documents`, at `{user_id}/{source_id}`, created on first use, 10 MB limit), and the Documents page opens it from `GET /documents/{source_id}/file`; each document says `has_file` and names its linked tasks. What remains: keeping the file is best effort, so a document uploaded while the store could not be reached, and every document uploaded before this change, has no file to open (`has_file: false`); and linking documents to tasks by meaning is a measured threshold, not a guarantee (on 192 calibration pairs it missed one true link and made one wrong one, see `DOCUMENT_LINK_SIMILARITY`). | `app/services/documents/{service,linker}.py`; `app/infrastructure/persistence/supabase_storage.py`; `07-api-design.md` §5.8 |
| L16 | **Workspace links need a web search that answers.** The workspace's web links (at most one search per workspace, none for an admin task) come from Claude's server-side web search (the chat key), with Gemini's grounded search as the fallback. Links the search did not return are dropped. When neither answers (no credits, no network) the workspace shows the task, its criteria, the user's documents and a notice, and does not retry for 5 minutes. A search costs about 18,000 input tokens, because the results are read by the model. | `app/models/brain/claude_web_search.py`; `app/services/analytical/workspace_builder.py` (`_search_web`) |
| L17 | **The workspace still makes one blocking read.** `build_task_workspace` reads the learning style with a synchronous Supabase call (the `user_preferences` leftover, L2); the task and its documents go through repositories since 2026-11-16; hands-on testing measured about 1.5 s per workspace. | `app/services/analytical/workspace_builder.py`; `tests/test_layering.py` (`_RAW_TABLE_CALLS_ALLOWED`) |
| L18 | **A slow upload in chat ends at the step's limit.** Documents are read off the event loop with a 120 s limit (503 `document_read_timeout` on the REST routes), but in a chat turn the knowledge module's `ingest_document` step stops at 60 s first. | `app/utils/docling_helper.py`; `app/modules/knowledge_graph.py` |

---

## How to verify this document

```bash
cd Jarvis-Engine
git log -1 --oneline                                   # d3e9132
git -C ../jarvis-frontend log -1 --oneline             # 4fa06ef

# Setup commands exist and say what section 1 says.
sed -n 1,48p scripts/seed_demo_user.py                 # what the seed does, refusal, exit codes
sed -n 1,48p scripts/live_e2e.py                       # the steps, cost, cleanup
sed -n 44,168p app/main.py                             # startup lines, CLAUDE_PRIMARY, JARVIS_ANTICIPATION
grep -o '^[A-Z_]*=' .env | sort -u                     # variable names only, never values
.venv/bin/python scripts/seed_demo_user.py --dry-run   # the seeded history's report, no network

# The demo content in 2.1 (read-only; the backend running on the seeded user).
# Note: GET /you also runs PEARL's look, which may write pattern memories.
curl -s "localhost:8000/api/v1/you?user_id=demo"
curl -s "localhost:8000/api/v1/anticipation/suggestions?user_id=demo"
curl -s "localhost:8000/api/v1/tasks/?user_id=demo&status=pending"

# The viva answers' code and tests.
.venv/bin/python -m pytest -q tests/test_layering.py tests/test_design_conformance.py tests/test_draft_service.py tests/test_migrations.py \
  tests/test_triggers.py tests/test_consent.py tests/test_memory_service.py tests/test_patterns.py \
  tests/test_claude_provider.py tests/test_prompt_layout.py tests/test_seed_demo_user.py
.venv/bin/python -m pytest -q tests/                   # 2361 passed, 2 xfailed, 2 deselected at fe74172
(cd ../jarvis-frontend && npm test)                    # the frontend suite (vitest)

# The whole loop, live (costs model calls; throwaway e2e- user, rows deleted).
.venv/bin/python scripts/live_e2e.py                   # last line: ALL PASSED
```

The ledger figures quoted here (seed time, suite counts, the 402, the cached-token count, the parked items) are in `.superpowers/sdd/2026-11-14-capstone-plan-{1,2,3}-*/progress.md`.
