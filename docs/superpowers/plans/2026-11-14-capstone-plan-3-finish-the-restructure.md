# Capstone Plan 3: Finish the Restructure (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An examiner who opens the repo finds one architecture: one chat endpoint, a thin router over a `ChatTurnService`, one persistence path per entity behind repository ports, collaborators passed in rather than looked up, a typed response on every route, and one error envelope. The live demo keeps working after every task.

**Architecture:**
- **Retire v1.** The v1 routes and `execute_agentic_flow` are deleted, with everything only they reach. The live helpers (the three goal-id functions and the brain-dump extraction prompt) move to `app/services/planning/`. `/api/v1/chat/v2/stream` becomes `/api/v1/chat/stream`.
- **Split `chat.py`.** It becomes routing plus parsing. The turn moves to `app/services/chat_turn/`, and the SSE frames become typed models with one encoder in `app/schemas/chat_stream.py`.
- **One persistence path per entity.** `DraftStore`, `MemoryStore`, `chat_history` and every raw `supabase.table(...)` in `app/api` and `app/services` give way to repository ports, except two allow-listed leftovers.
- **Injection, not lookup.** The composition root reaches graph nodes through the turn's state (`container`, and `planning` for the planning sub-graph) and reaches services through constructors. `app/core/runtime.py` is deleted.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, LangGraph, supabase-py, OR-Tools CP-SAT, pytest + pytest-asyncio. Frontend: Next.js 14, React 18, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-14-capstone-lld-and-anticipation-design.md`: §5, §6.2–6.4, §8.5, §10, §12 phase 2b, ADR-9. This is plan 3 of 4. Its inputs are the Plan 2 ledger `.superpowers/sdd/2026-11-14-capstone-plan-2-vision-core/progress.md`, in particular final review B (I2 two persistence paths, I3 global lookups, god files, M3 API naming, M4 skip reason) and the "Carried to Plan 3/4" line, plus the Plan 1 ledger's Plan-2b/Plan-3 items (G1 slow-client lock, `str(exc)` in v1 streams, TaskChunk in the API layer).

**Code base this plan was verified against:**
- Jarvis-Engine `capstone-lld` at `dca181d`. The suite stands at 1850 passed, 3 xfailed, 2 deselected.
- jarvis-frontend `capstone-lld` at `a2db469`.

## Global Constraints

**Branch, commits, tests**
- Work on branch `capstone-lld` in both repos. Commit after every task (one commit per repo it touches), ending the message with the session's Co-Authored-By trailer.
- Never `git stash`, never check out another branch, never rewrite history.
- The full suite passes after every task: `.venv/bin/python -m pytest tests/ -q`. The baseline is 1850 passed, 3 xfailed, 2 deselected. Task 1 removes the strict xfail `test_trigger_replan__pending_tasks__rewrites_them` along with its code, which leaves 2 xfailed. Each task's report states the new counts.
- Tests never touch the network (conftest socket guard). They use the InMemory family, `FakeSupabase`, a scripted chain, and the real CP-SAT solver with the `deterministic_solver` fixture. Names follow `test_{thing}__{condition}__{expected}`.
- Frontend: after every frontend commit, `npm test` and `npx tsc --noEmit` are clean, and `npm run lint` reports no new errors.

**Behaviour preservation**
- A test is deleted only together with the code it tests, and only when the task names it. A moved behaviour keeps every test that pins it. Such tests are re-pointed (import path or call signature), never weakened. An assertion changes only where the task says the behaviour changes.
- Moved names are not re-exported from their old home, except the `planning_graph` package facade (Task 9); tests are re-pointed instead. Spec §14 permits re-exports, but an examiner reading two homes for one name is what this plan removes.
- Plan 3 changes no database schema. A task that finds it needs one stops and reports; it does not write a migration.

**The live stack**
- A backend on :8000 (no `--reload`) and a frontend dev server on :3000 (hot reload) run from these working trees against the live database and the seeded `demo` user.
- Implementers never stop or restart either server, never call a write endpoint, never run `scripts/seed_demo_user.py` or `scripts/live_e2e.py`, and never touch the live database. Only the orchestrator does those things, at the Live checkpoints below.
- A frontend edit is live the moment it is saved, so it must work against whatever backend is running. The one exception, Task 1's path switch, has its own ordered steps.

**Layering** (`tests/test_layering.py`, tightened in Task 8)
- `app/domain/**` imports only the standard library, pydantic and itself.
- `app/services/**`, `app/orchestrator/**`, `app/modules/**`, `app/core/**` and `app/api/**` never import `app.infrastructure`; only `core/model_router.py` stays allow-listed.
- Nothing below `app/api` imports `app.api`.
- Only `app/container.py` builds a repository.
- No raw `.table(` call in `app/api`, `app/services`, `app/core`, `app/modules` or `app/orchestrator` outside the Task 8 allow-list.

**Data, errors, copy, time** (unchanged from Plan 2)
- Every repository method takes `user_id` first, and every read is bounded (PostgREST caps a read at 1000 rows).
- Errors are `JarvisError` subclasses in the one envelope `{"error": {"code","message","details","request_id"}}`. No exception text reaches a client.
- Copy is anti-guilt: never "missed", "failed", "you didn't", "behind", or any phrase in `app/domain/anti_guilt.py::BLAME_PHRASES`. Never call Jarvis a calendar or a planner.
- "Today", hours and wall times are always computed in the user's zone (`app/services/planning/local_time.py`).
- Never copy code from any leaked Claude Code source.

## Decisions this plan makes where the spec is silent

1. **Which routes go.** The keep/delete table below is the record. Every deleted route either has no caller in `jarvis-frontend/lib/api.ts`, or has a caller this plan removes in the same task.
2. **The stream rename keeps a hidden alias.** `/api/v1/chat/v2/stream` stays registered with `include_in_schema=False` and `deprecated=True` until Task 11, so the running frontend and backend can be restarted in either order. Task 11 deletes it after the final live run.
3. **No accept without a draft.** `/chat/accept-schedule` is deleted. When a planning turn could not store its draft (the frame has a schedule but no `draft_id`), the frontend makes no request. It shows "I couldn't keep this plan for review. Ask me again and I'll rebuild it." Draft acceptance stays the only write path.
4. **`user_tasks` rows are written only by draft acceptance, and statuses only by `TaskService`.**
   - The unused `PATCH /tasks/{id}`, `DELETE /tasks/{id}` and `DELETE /tasks/` broke that rule and are deleted.
   - The `task_completion_signals` insert stops: the `completed` task event, carrying quality and actual duration, is the one outcome record (ADR-11). The table stays and is read by nothing.
5. **How the composition root reaches graph nodes: through the turn's state.**
   - The orchestrator's nodes read the transient key `container` (an `AppContainer`).
   - The planning sub-graph reads `planning`, a `PlanningServices` record holding only the ports it uses (ISP, spec §6.4).
   - The chat turn sets both. The replanner passes `planning` from its constructor.
   - With no container (unit tests, scripts) a feature is off and says so in the log. That is the Null Object pattern (spec §6.3), never a second persistence path. `app/core/runtime.py` is deleted.
6. **`ChatTurnService` yields encoded SSE text.**
   - The generator is today's `event_gen`, moved, so the lock, the pump and the shielded cleanup keep their exact structure. The frame encoding is `SseEncoder` (spec §6.4 SRP split).
   - The endpoint returns `ClosingStreamingResponse`, which closes the body iterator under a shield. This fixes the parked G1 slow-client path (Plan 1 ledger).
7. **Mastery reads the outcome history.**
   - `user_tasks.quality_score` is never written by the app, so the coach's quality numbers are always the neutral prior today.
   - Success ratio, average quality, quality trend and goal ids move onto `task_events` (quality from the completed event's metadata) and `TaskRepository`.
8. **`run_schedule` moves to `app/services/planning/scheduling.py` and raises `ScheduleInfeasibleError`, not `HTTPException`** (spec §10).
   - Its database read of the success ratio is dropped. v2 never passed it a database, so v2 has always used the 0.5 prior; the prior stays as a `success_rate: float = 0.5` parameter.
   - CLAUDE.md's "run_schedule is a reusable module-level function" stays true, at its new home.
9. **New ports.**
   - `GoalRepository` over `goals` and `user_plan_updates` (spec §6.2 names it, and the entity `Goal` exists).
   - `HabitTrackerRepository` over `habit_trackers`.
   - `DocumentRepository` over `ingested_documents` and `task_materials`.
   - `PendingCalendarRepository`.
   - `TaskRepository` gains `list_for_user` and `bring_back`.
   - `ConversationRepository` gains the session and message writes, and its docstring stops saying "only reads".
10. **The raw-table allow-list ends with two entries, each with its reason.**
    - `services/analytical/workspace_builder.py` keeps its `task_workspaces` reads and writes: it is a cache of a derived payload, not an entity in §6.1.
    - `services/user_preferences.py` keeps a one-column `learning_style` read.
    - Both are recorded in `docs/capstone` by Plan 4.
11. **Draft edits go through `DraftService`.**
    - A closed draft is refused by the State table (`DraftSchedule.transition`), not by a repository filter. This closes the Plan 1 sweep item where `replace_tasks` could edit an accepted draft.
    - `DELETE /drafts/{id}` becomes *discard*, which moves the draft to EXPIRED and deletes no row.
    - The 501 `POST /drafts/{id}/chat` is deleted.
12. **Status codes that change.** A malformed request is `ValidationError`, so 400 becomes 422. An unreachable classifier is `LLMUnavailableError`, so 502 becomes 503. Every error body is already the envelope, so the frontend reads them unchanged.
13. **The streak comes from the backend.** `GET /you` gains `streak_days`, computed by the function the coach uses. The dashboard stops computing it.
14. **The dashboard Patterns tile shows the number of pattern cards on the You page.** Task 10 checks the live number before changing anything.
15. **`DELETE /ingestion/behavioral-constraint/{id}` is deleted.** It duplicates `DELETE /habits/constraints/{id}`, which the frontend uses and which also lets go of the linked memory (`StatedRules`).
16. **Settings are injected for classes the container builds.**
    - A frozen settings record per service is built once by `Settings.from_config(cfg)` in the composition root.
    - Constructors default to `default_settings()`, so tests and the seed script keep working without passing settings.
17. **The `CognitiveModule` registry is in scope but droppable (Task 9).** At HEAD it is a small change: three `ModuleDefinition`s declare their intents, and routing is derived from them plus one table for the four built-in nodes.

## Route keep/delete table

The routes the frontend calls today, from every `fetch` in `jarvis-frontend/lib/api.ts`:

- **Chat:**
  - `POST /chat/v2/stream` (`chatStream`).
  - `POST /chat/confirm-schedule` (`confirmScheduleStream`, only from `confirmTasks`, which runs only when `awaiting_task_confirmation` is true, and the v2 route always sends false).
  - `POST /chat/accept-schedule` (`acceptScheduleDirect`, only when a draft-schedule frame has no `draft_id`).
- **Tasks:** `POST /tasks/{id}/complete`, `POST /tasks/{id}/skip`, `GET /tasks/`, `GET /tasks/{id}/workspace`.
- **Sessions:** `GET /sessions/`, `DELETE /sessions/`, `GET`/`PUT`/`DELETE /sessions/{id}`.
- **Documents:** `GET /documents/`, `DELETE /documents/{id}/`, `POST /ingestion/process`.
- **Drafts:** `GET /drafts/{id}`, `POST /drafts/{id}/accept`, `POST /drafts/{id}/reject`.
- **Habits:** `GET /habits/tracker/due`, `POST /habits/tracker/{id}/complete`, `GET /habits/constraints`, `DELETE /habits/constraints/{id}`.
- **Pending calendar:** `POST /ingestion/pending-calendar/{id}/approve` and `/reject`.
- **Memories:** `GET /memories/`, `DELETE /memories/{id}`, `POST /memories/{id}/confirm` and `/dismiss`.
- **You:** `GET /you`, `PATCH /you/settings`.
- **Anticipation:** `GET /anticipation/suggestions`, `.../{id}/accept`, `.../{id}/dismiss`, `POST /anticipation/run`.
- **Models:** `GET /models/status`.

Defined in `lib/api.ts` but imported by nothing: `chat()`, `updateTask`, `deleteTask`, `editDraftTask`, `getPendingCalendars`.

`scripts/live_e2e.py` calls `/chat/v2/stream`, `/drafts/{id}/accept`, `/memories/` (GET, DELETE), `/you`, `/anticipation/run`, `/anticipation/suggestions` (GET, accept) and `/tasks/{id}/complete` and `/skip`.

| Route | Called by | Plan 3 |
|---|---|---|
| `POST /api/v1/chat/` (v1) | nobody (`chat()` has no importer) | **Delete**, Task 1 |
| `POST /api/v1/chat/stream` (v1) | nobody | **Delete**, Task 1; the path is reused by the turn stream |
| `POST /api/v1/chat/confirm-schedule` | dead frontend branch | **Delete**, Task 1, with `confirmTasks` and `TaskPreview` |
| `POST /api/v1/chat/accept-schedule` | frontend no-draft fallback | **Delete**, Task 1 (decision 3) |
| `POST /api/v1/chat/modify-schedule` | nobody | **Delete**, Task 1, with `parse_modification_request`, `apply_modification`, `fuzzy_match_task` |
| `POST /api/v1/chat/v2/stream` | frontend, live script | **Rename** to `/api/v1/chat/stream`, Task 1; hidden alias until Task 11 |
| `POST /drafts/{id}/chat` (501) | nobody | **Delete**, Task 4 |
| `DELETE /drafts/{id}` | nobody | **Keep** as discard through `DraftService`, Task 4 |
| `POST /drafts/{id}/modify`, `PATCH /drafts/{id}/tasks/{task_id}`, `POST /drafts/{id}/rearrange` | nobody (`editDraftTask` unimported) | **Keep**, moved through `DraftService`, Task 4 |
| `PATCH /tasks/{id}`, `DELETE /tasks/{id}`, `DELETE /tasks/` | nobody | **Delete**, Task 5 (decision 4) |
| `DELETE /ingestion/behavioral-constraint/{id}` | nobody | **Delete**, Task 5 (decision 15) |
| every other route listed by `app.routes` at HEAD | as above | **Keep**; typed response model and one envelope by Task 8 |

## Review Focus

Each of these is a way the restructure is likely to break the live demo. Each is pinned.

1. **Frontend and backend disagree about the stream path.**
   - The risk: the hot-reloaded frontend posts to `/api/v1/chat/stream` while the old backend still serves the deprecated v1 route there, or posts to a path the new backend no longer has.
   - Expected: chat always reaches the orchestrator turn.
   - Pinned by:
     - Task 1 `test_routes__chat__stream_and_hidden_alias_are_the_one_turn_handler`.
     - Frontend `chatStream posts to /api/v1/chat/stream`.
     - Task 1's ordered restart step.
     - Live step 1 over the new path.
     - Task 11 `test_routes__chat__only_the_stream_remains`.
2. **A tab closed mid-turn leaves the conversation locked.**
   - The risk: the next message in that chat waits forever after the turn moves into `ChatTurnService`.
   - Expected: the lock is released, in order, after the graph run unwinds, with or without garbage collection.
   - Pinned by:
     - the kept `tests/test_turn_locks.py` tests: `test_chat_v2__client_disconnects_mid_turn__lock_released_once_the_run_unwound`, `..._queued_turn_starts_after_the_cleanup`, `test_chat_v2__two_tabs_one_session__turns_run_in_order`, `test_chat_v2__turn_queued_past_the_wait_limit__honest_conflict_frame`.
     - Task 3's new `test_chat_stream__client_gone_while_a_frame_waits_to_send__lock_released_without_gc`.
3. **"Looks good" or the Accept button stops finding the draft, or a double accept writes twice.**
   - The risk comes from moving from `DraftStore` to `DraftService`.
   - Pinned by:
     - Task 4 `test_planning_turn__draft_proposed__chat_accept_resolves_it_by_id`.
     - Task 4 `test_rest_accept__after_a_chat_proposal__second_accept_409_and_tasks_written_once`.
     - the kept `tests/test_accept_on_time.py`.
     - Live steps 2, 3 and 4.
4. **A node silently loses its collaborators when the container stops being global.**
   - The risk: the Null behaviour hides the loss. The taught rule stops blocking the morning, memories stop being recalled, learning moments stop.
   - Pinned by:
     - Task 7 `test_turn__container_only_in_turn_state__rule_memory_and_pattern_reach_the_turn`, a behaviour-harness turn with no process-wide registration.
     - Task 7 `test_runtime_registry__gone_and_imported_nowhere`.
     - Live steps 5, 6, 7 and 10a.
5. **A list moved behind a repository comes back with a different shape or count.**
   - The pages at risk: the dashboard's 90 seeded tasks, the Habits rules list, the chat sidebar, the documents page.
   - Pinned by:
     - Task 5 `test_list_tasks__seeded_size__every_field_the_dashboard_reads_newest_first`.
     - Task 5 `test_habits_constraints__same_rules_as_the_you_page`.
     - Task 6 `test_sessions_list__pinned_first_then_recent__archived_hidden_and_outage_503`.
     - Task 6 `test_documents_list__same_fields_as_before_and_outage_503`.
     - the repository contract suite, run against both families.
     - Live steps 11b and 14.

---

## Task map, parallelism and shared files

| # | Task | Repos | Depends on | Parallel with | Droppable? |
|---|---|---|---|---|---|
| 1 | Retire v1; one chat endpoint | both | none | 10-FE (after its FE commit) | no |
| 2 | Typed SSE events + `SseEncoder` | backend | 1 | 5 | no |
| 3 | `ChatTurnService`; thin chat router; turn state carries the container | backend | 2 | 5 | no |
| 4 | Drafts: one path (`DraftStore` retired) | backend | 3 | none | no |
| 5 | Tasks, rules, goals, habit trackers: one path | backend | 1 | 2, 3 | no |
| 6 | Sessions, messages, documents, pending calendar: one path | backend | 4, 5 | none | partly (documents and pending calendar) |
| 7 | Injection: no global lookups; settings from the composition root | backend | 6 | none | settings step only |
| 8 | API contract: typed responses, one envelope, layering at (near) empty | backend | 7 | 10-backend | no |
| 9 | Orchestration structure: `CognitiveModule` registry + `planning_graph` package | backend | 8 | 10 | **yes** |
| 10 | Carried fixes: streak, unslotted tasks, Patterns tile | both | FE: 1; backend: 7 | 8, 9 | Patterns-tile step |
| 11 | Live E2E, alias removed, docs | both | all kept | none | no |

**What the examiner still sees if a droppable task is cut:**
- **Task 9:** `INTENT_TO_MODULE` stays a hand-written table, so adding a module edits `routing.py`. Plan 4 must then soften spec §6.4's OCP claim to "the registry derives the graph's nodes". `planning_graph.py` stays one cohesive 1,000-line file of node functions.
- **Task 6, second half:** `documents.py` and the pending-calendar handlers keep raw reads in `app/api`, and Task 8's allow-list names them.
- **Task 7, settings step:** container services keep reading `config` module globals. Spec §6.3's "Settings via DI" stays unmet, and Plan 4 amends it.
- **Task 10, Patterns step:** the tile keeps its current count, which already equals the You page's card count.

**Shared files.** Merge these sequentially; each task's edits are additive:
- `app/container.py`: Tasks 3, 4, 5, 6, 7.
- `app/domain/{entities,ports}.py`, `app/infrastructure/persistence/*`, `tests/test_repository_contracts.py`, `tests/fakes.py`: Tasks 4, 5, 6.
- `app/modules/planning_graph.py`: Tasks 1, 4, 7, 8, 9.
- `app/services/chat_turn/*`: Tasks 3, 4, 6, 7.
- `app/orchestrator/{graph,state,checkpoint}.py`: Tasks 1, 3, 4, 7.
- `tests/test_layering.py`: Tasks 1, 4, 7, 8.
- Frontend `lib/api.ts`, `lib/types.ts`, `lib/contracts.ts`: Task 1, then Task 10.
- **Tasks 2 and 3 run in parallel with Task 5 only.** Task 5 touches no chat file. Both may add a helper to `tests/fakes.py`; that is an additive merge.

**Live checkpoints (orchestrator only).** After Tasks 1, 3, 4, 5, 6, 7 and 11:
1. Restart the backend from the working tree (`.venv/bin/uvicorn app.main:app --port 8000`, the same env as now, `CLAUDE_PRIMARY=true`).
2. Run `.venv/bin/python scripts/live_e2e.py`. Expected: `ALL PASSED` (18 steps), and a server log with no tracebacks.
3. After Tasks 5 and 6, also render `/dashboard`, `/habits`, `/documents` and the chat sidebar in a headless browser against the seeded `demo` user. Those are the pages whose reads moved, and the live script does not read them.
4. On a failure, stop and fix before the next task.

Between checkpoints the running backend serves the last checkpoint's code, which has passed, so the demo stays ready.

---

### Task 1: Retire v1, one chat endpoint (backend + frontend)

**Files (backend):**
- Create:
  - `app/services/planning/goal_ids.py`: `derive_goal_id`, `mint_run_token`, `namespace_chunk`, moved verbatim from `control_policy.py:299-358`.
  - `app/services/planning/brain_dump_prompt.py`: `BRAIN_DUMP_EXTRACTION_PROMPT`, moved verbatim.
  - `tests/test_routes.py`.
- Delete:
  - `app/services/analytical/control_policy.py`
  - `app/services/intent_registry.py`
  - `app/services/analytical/task_editor.py`
  - `app/services/analytical/task_rearranger.py`
  - `app/services/analytical/task_retrieval.py`
  - `app/services/memory/pearl.py`
  - `app/services/memory/extractor.py`
  - `app/services/memory/retriever.py`
  - `app/services/memory/store.py` (`MemoryStore`)
- Modify:
  - `app/api/v1/endpoints/chat.py`:
    - Delete `chat`, `chat_stream`, `_stream_direct_27b`, `ConfirmScheduleRequest`/`confirm_schedule_stream`, `_persist_failed`, `AcceptScheduleRequest`/`accept_schedule`/`_accept_as_a_draft`, `ModifyScheduleRequest`/`modify_schedule`, and their imports (`execute_agentic_flow`, `hybrid_route_query`, `log_step`, `JSONResponse`).
    - `chat_stream_v2` is registered at `"/stream"` and renamed `chat_stream`. The alias `"/v2/stream"` is the same function, registered with `include_in_schema=False, deprecated=True`.
  - `app/schemas/chat_stream.py`: drop `awaiting_task_confirmation`; docstrings name `/api/v1/chat/stream`.
  - `app/schemas/context.py`: delete `IntentContext` and `ChatResponse` (v1 only; confirm with grep).
  - `app/orchestrator/graph.py`:
    - The prompt import comes from `brain_dump_prompt`.
    - `_load_context` drops the memory store.
    - The module docstring stops naming `execute_agentic_flow`.
  - `app/modules/planning_graph.py`:
    - The helper import comes from `services/planning/goal_ids`.
    - `_memory_slots` without a container returns `[]`; the legacy-store branch goes.
  - `app/services/memory/constraint_bridge.py`: delete `memories_to_constraints`, `_parse_hour_from_pattern`, and the private aliases `_parse_time_range` and `_to_horizon`.
  - `app/services/analytical/schedule_modifier.py`: keep only `resolve_from_now`.
  - `app/services/draft_actions.py`: delete `_count_persisted`.
  - `tests/fakes.py`: add `build_test_container(supabase: FakeSupabase | None = None) -> AppContainer`. It calls `AppContainer.build` with `BagOfWordsEmbedder()` and `install_chain=False`; None means the InMemory family. Tasks 3–7 use it.
    - The name must not start with `test`: pytest would collect it in every module that imports it.
  - `app/services/documents/pipeline.py`: drop the `memory_store` parameter and its write.
  - `app/core/user_model.py`: delete the memory and semantic store accessors, `build_chat_context`, `get_active_goals`, `get/set_active_draft`, `get/set_pearl_patterns` and `upsert_behavioral_constraint`. None has a caller.
  - `app/core/runtime.py`: drop `memory_store`.
  - `app/modules/planning_replan.py`: drop `memory_provider`.
  - `app/main.py`: no `MemoryStore`, no `register_default_intents`.
  - `app/api/v1/endpoints/tasks.py`: fix the comment that names `control_policy`.
  - `scripts/live_e2e.py`: post to `/api/v1/chat/stream`.
  - `tests/test_layering.py`:
    - Drop the `control_policy` entries from `_API_IMPORTS_ALLOWED` and `_WEB_SEARCH_CALLS_ALLOWED`.
    - Empty `_LEGACY_LLM_CALLS_ALLOWED`.
    - Delete `_V1_ONLY`.

**Files (frontend):**
- `lib/api.ts`:
  - `CHAT_STREAM_PATH = '/api/v1/chat/stream'`.
  - Delete `chat`, `confirmScheduleStream`, `acceptScheduleDirect`, `updateTask`, `deleteTask` and their request types.
- `lib/hooks/useJarvisChat.ts`:
  - Delete `confirmTasks`, `pendingTasks`, `pendingGoalMetadata` and the `awaiting_task_confirmation` branch.
  - Requests stop sending `confirm_before_schedule` and `draft_schedule`.
  - `acceptDraftFn` with no draft id shows `noDraftToAccept()` and makes no request.
- `lib/learning.ts`: add `noDraftToAccept(): AcceptOutcome`, with copy from decision 3, tone `info`.
- `lib/types.ts`:
  - `ChatRequest` keeps only `user_prompt, user_id, conversation_id, file_base64, media_type, file_name, model_mode`.
  - Remove `ConfirmScheduleRequest`, the `awaiting_confirmation` phase and `ChatResponse.awaiting_task_confirmation`.
- `components/app/JarvisResponse.tsx`, `components/app/DraftReview.tsx`, `app/(app)/chat/page.tsx`: drop `onConfirmTasks` and stage 1.
- Delete `components/app/TaskPreview.tsx`; only `DraftReview` imports it.
- Tests: `lib/__tests__/sse.test.ts`, `lib/__tests__/learning.test.ts`.

**Interfaces:**
- Produces:
```python
# app/services/planning/goal_ids.py (signatures unchanged from control_policy)
def derive_goal_id(goal_metadata: Any, fallback_objective: str = "") -> str
def mint_run_token() -> str
def namespace_chunk(chunk: TaskChunk, goal_id: str, run: str) -> TaskChunk
# app/services/planning/brain_dump_prompt.py
BRAIN_DUMP_EXTRACTION_PROMPT: str
# app/api/v1/endpoints/chat.py
async def chat_stream(request: ChatRequest, http_request: Request) -> StreamingResponse   # POST /stream (+ hidden /v2/stream)
# tests/fakes.py
def build_test_container(supabase: FakeSupabase | None = None) -> AppContainer
```

**Draft-edit re-solves (carried fix F1) are settled by this deletion.** The two paths that rebuilt constraints from habit text alone were `/chat/modify-schedule` and `control_policy._run_schedule_modify_flow`. The only solve left outside a planning turn is `PlanningGraphReplanner.reslot_from_now`, which already builds its slots with `plan_time_slots`, the late-accept function. It stays pinned by `tests/test_accept_on_time.py::test_rest_accept__late__a_rule_kept_only_as_a_memory_still_holds` and the other tests in that file.

**Tests that go with the deleted code:**
- Whole files:
  - `test_chat_accept_schedule.py`
  - `test_clarification.py`
  - `test_intent_routing.py`
  - `test_memory_store.py`
  - `test_pearl.py`
  - `test_pearl_integration.py`
  - `test_memory_extractor.py`
  - `test_memory_retriever.py`
  - `test_memory_wiring.py`
  - `test_background_replan.py` (including the strict xfail)
  - `test_schedule_modifier.py`
- `test_core_pipeline.py`: the four `intent_registry` tests.
- `test_draft_flow.py`: `test_draft_accept__persists_tasks`, `test_draft_accept__empty_chunks__no_insert`.
- `test_draft_store.py`: `test_legacy_add_component_with_four_args`, `test_legacy_create_returns_draft_with_id`.
- `test_error_text_leaks.py::test_accept_schedule__invalid_tasks__fixed_message_no_validation_text`.
- `test_llm_call_sites.py`: the memory-extraction, schedule-modification, task-edit and task-rearrange tests (7).
- `test_memory_constraint_bridge.py`: the 8 tests that call `memories_to_constraints`.
- `test_memory_integration.py`: every class except `TestMemorySchemas` and the `parse_time_range` cases.
- `test_memory_service.py::test_legacy_retriever__after_the_decay_sweep__reads_the_strength_the_service_reports` and `test_legacy_reinforce__then_the_decay_sweep__memory_not_faded`.
- `test_store_resilience.py`: the two `MemoryStore` tests and `test_build_memory_context__store_raises__returns_empty_string`.
- `test_container.py::test_lifespan__composition_root_built__legacy_pearl_detectors_not_registered`.
- `test_migrations.py::test_migrations__pearl_task_fetch__selects_only_existing_user_tasks_columns`.
- `test_local_time.py::test_pearl__skips_at_nine_local__counted_as_hour_nine`.
- `test_observation.py::test_observation_loop_calls_pearl`, `test_detect_pearl_patterns__no_composition_root__nothing_and_no_legacy_detector`.
- `behaviour/test_psychology.py::test_anti_guilt__deprecated_v1_planner__every_user_facing_literal_is_blame_free`.

**Tests kept and re-pointed:**
- `test_turn_learning.py::test_brain_dump_prompt__memories_first__never_exclusive_of_inline_habits`: prompt import path.
- `test_planning_replan.py`: `_to_horizon` becomes `to_horizon`.
- `test_memory_constraint_bridge.py::TestHorizonAnchoredParsing` and `test_memory_integration.py`'s parse cases: `_parse_time_range` becomes `parse_time_range`.
- `test_planning_graph.py::test_accept_after_replan__pre_existing_pending_rows_survive_in_user_tasks` and `test_planning_subgraph__namespaced_ids__reach_the_solver_and_the_draft`: accept through `DraftService.accept` over the same `FakeSupabase` instead of `_persist_fused_tasks`.
- `test_task_lifecycle.py::test_task_endpoints__any_change__schedule_no_replan_and_claim_none`: drop its `trigger_replan` monkeypatch.
- `behaviour/test_psychology.py::test_anti_guilt__learned_skip_pattern__insight_and_note_are_blame_free`: rebuilt on `PatternService` over a seeded skip-window history. It covers `SkipTimeWindowDetector.noticed` and `insights_of`, with the same `blame_phrases_in` assertion.
- `behaviour/test_long_term_memory.py`, the five tests that used `build_memory_context` as the oracle:
  - The oracle becomes the contents of `jarvis.state.container.memory_service.list_active("alice", now=...)`.
  - `test_memory__confirmed_pattern__outlasts_an_unconfirmed_one` seeds through `memory_service.remember(..., provenance=MemorySource.INFERRED, origin="behavior")`.
- `behaviour/conftest.py`: the `Jarvis` harness drops `memory_store` and `set_shared_clients(memory_store=...)`.
- `test_orchestrator.py`, `test_checkpointer.py`, `test_error_text_leaks.py`, `test_turn_locks.py`, `behaviour/conftest.py`: `chat_stream_v2` becomes `chat_stream`.

- [ ] **Step 1: Write the failing tests.**
  - In `tests/test_routes.py`:
    - `test_routes__chat__stream_and_hidden_alias_are_the_one_turn_handler`:
      - The set of `/api/v1/chat` paths in `app.routes` is exactly `{"/api/v1/chat/stream", "/api/v1/chat/v2/stream"}`.
      - Both have `endpoint is chat.chat_stream`.
      - Only the alias has `deprecated` and `include_in_schema is False`.
    - `test_v1__retired_modules__not_importable`: parametrized over the nine deleted module paths; `importlib.util.find_spec(name) is None`.
    - `test_off_turn_resolve__only_the_replanner_calls_resolve_from_now`: AST scan of `app/`. Calls to `resolve_from_now` appear only in `app/modules/planning_replan.py`.
  - Frontend:
    - In `sse.test.ts`: `chatStream posts to /api/v1/chat/stream` (mock `fetch`, assert the URL ends with the path).
    - In `learning.test.ts`: `noDraftToAccept says the plan was not kept, without blame`.
- [ ] **Step 2: Run.** `.venv/bin/python -m pytest tests/test_routes.py -q` and `npm test`. Expected: FAIL (the v1 paths are present; `noDraftToAccept` is undefined).
- [ ] **Step 3: Implement the backend side.** Do the moves, the deletions and the re-pointed tests as listed. Run the full suite. Expected: PASS, with 2 xfailed. Commit `refactor(chat): v1 retired; /api/v1/chat/stream is the one turn endpoint`.
- [ ] **Step 4: Orchestrator: restart the backend from the tree** (Live checkpoint procedure). Check that `/api/v1/chat/stream` answers a turn (`curl -N` with a greeting) before any frontend file is saved.
- [ ] **Step 5: Implement the frontend side.** Run `npm test`, `npx tsc --noEmit`, `npm run lint`. Expected: PASS. Commit in jarvis-frontend: `refactor: chat posts to /api/v1/chat/stream; the v1 confirm and draft-less accept paths are gone`.
- [ ] **Step 6: Orchestrator: live checkpoint.** `scripts/live_e2e.py` must report ALL PASSED.

### Task 2: Typed SSE events, one catalogue, one encoder

**Files:**
- Modify:
  - `app/schemas/chat_stream.py`: add the frame models, `SseEvent`, `SSE_EVENTS` and `SseEncoder`.
  - `app/api/v1/endpoints/chat.py`: every frame is built through `SseEncoder`; `render_progress_event` and `_PROGRESS_EVENT_CHANNEL` move out.
  - `app/api/error_handlers.py`: imports `ErrorBody` and `ErrorResponse` from their new home.
- Create: `app/schemas/errors.py`; `tests/test_sse_contract.py`.
- Test:
  - `tests/test_orchestrator.py`: the five `test_render_progress_event__*` tests move to `tests/test_sse_contract.py`, re-pointed to `SseEncoder.render_progress`. Their assertions are unchanged.

**Interfaces — Produces:**
```python
class SseEvent(str, Enum): PHASE="phase"; STEP="step"; THINKING="thinking"; MESSAGE="message"; TOOL_USE="tool_use"
                           MEMORY_EXTRACTED="memory_extracted"; PATTERN_DETECTED="pattern_detected"
                           CONSENT_REQUEST="consent_request"; COMPLETE="complete"; ERROR="error"
class PhaseFrame(BaseModel):        # extra="allow": progress callbacks add their own detail keys
    phase: str; verb: str | None = None; detail: dict | None = None
class StepFrame(BaseModel):         # extra="forbid"
    intent: str; stage: Literal["intent_classified", "pipeline_done"]
    model_mode: str | None = None; synthesis_model: str | None = None; force_cloud: bool | None = None
class TokenFrame(BaseModel): token: str                                   # thinking and message
class ToolUseFrame(BaseModel): module: str; tool: str; status: Literal["started", "done", "error", "skipped"]; detail: dict | None = None
class MemoryExtractedFrame(BaseModel):
    memory_id: str; type: MemoryType; content: str; confidence: float; provenance: MemorySource
    action: Literal["created", "reinforced", "superseded"]
class PatternDetectedFrame(BaseModel):
    type: str; content: str; confidence: float; occurrence_count: int; action: Literal["created", "strengthened"]
    pattern_key: str | None; memory_id: str
class ConsentRequestFrame(BaseModel): reason: str; module: str
# ErrorBody and ErrorResponse move from app/api/error_handlers.py to app/schemas/errors.py (error_handlers imports
# them from there); the SSE error frame is ErrorResponse itself, so the HTTP handlers and SSE share one model
SSE_EVENTS: dict[SseEvent, type[BaseModel]]        # COMPLETE -> ChatCompletePayload; the catalogue docs/capstone 07 cites
class SseEncoder:
    @staticmethod
    def frame(event: SseEvent, payload: BaseModel) -> str          # "event: X\ndata: <json>\n\n"
    @staticmethod
    def render_progress(raw: Any) -> tuple[str, str, str]          # (frame, event_type, token): render_progress_event's contract
```

**Rules:**
- **Byte-compatible JSON.** `frame` encodes `json.dumps(payload.model_dump(mode="json", exclude_unset=True))`. A frame carries exactly the keys its emitter set, as today's dicts do.
- **Progress entries are validated against their channel's model.** An entry that fails validation is logged (`logger.warning`, event type only) and sent as today's raw passthrough. A frame is never dropped.
- **The handler's own frames are built from the models** (phase, step, message, thinking, consent_request, error, complete).

- [ ] **Step 1: Write failing tests** in `tests/test_sse_contract.py`:
  - `test_sse_catalogue__every_event_the_frontend_handles__has_a_model`: the keys of `SSE_EVENTS` are the 10 `SseEvent` values.
  - `test_encoder__frame__event_line_data_line_blank_line`.
  - `test_encoder__unset_optional_fields__not_in_the_json`.
  - `test_render_progress__off_contract_tool_use__passed_through_raw_and_logged` (caplog).
  - `test_chat_turn__every_frame_emitted__parses_into_its_model`: one planning turn and one chat turn through the behaviour harness. Every `(event, data)` validates as `SSE_EVENTS[event]`.
  - The five moved `test_render_progress_event__*` tests.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_sse_contract.py -q`. Expected: FAIL (`SseEncoder` undefined).
- [ ] **Step 3: Implement** the models and the encoder, and switch `chat.py` to them.
- [ ] **Step 4: Run** the new tests, then the full suite. Expected: PASS. Everything that parses frames, including `behaviour/*` and `test_turn_learning.py`, passes unchanged.
- [ ] **Step 5: Commit** `refactor(chat): every SSE frame is a typed model in one catalogue, encoded in one place`.

### Task 3: `ChatTurnService`, a thin chat router, and the container in the turn state

**Files:**
- Create:
  - `app/services/chat_turn/__init__.py`: exports `ChatTurnService` and `ChatTurn`.
  - `app/services/chat_turn/service.py`: the turn. Model mode, attribution, the lock, the pump, frames, the reply save, the complete frame, compaction scheduling.
  - `app/services/chat_turn/context.py`: `read_conversation`, today's `_read_conversation` closure.
  - `app/services/chat_turn/reply.py`: the post-graph reply branches (schedule, chat, pipeline voice-of-Jarvis fallback) and `build_complete_payload`.
  - `app/api/sse.py`: `ClosingStreamingResponse`.
- Modify:
  - `app/schemas/chat_stream.py`: `ChatStreamRequest`, moved from `chat.py::ChatRequest` without the v1-only fields (`day_start_hour`, `deadline_override`, the four duration bounds, `confirm_before_schedule`, `draft_schedule`).
  - `app/api/v1/endpoints/chat.py`: routing and parsing only.
  - `app/orchestrator/state.py`: `JarvisState.container`.
  - `app/orchestrator/checkpoint.py`: `"container"` in `_TRANSIENT_KEYS`.
- Test:
  - `tests/test_turn_locks.py`, `tests/test_checkpointer.py`, `tests/test_error_text_leaks.py`, `behaviour/conftest.py`: import `ChatStreamRequest` and `chat_stream`.
  - New `tests/test_chat_turn.py`.

**Interfaces:**
- Consumes: `SseEncoder`, `SSE_EVENTS` (Task 2).
- Produces:
```python
class ChatStreamRequest(BaseModel):  # app/schemas/chat_stream.py; fields and defaults as today, Field(description=...) each
    user_prompt: str; user_id: str; conversation_id: str | None = None
    file_base64: str | None = None; media_type: str | None = None; file_name: str | None = None
    model_mode: str = "auto"
class ChatTurn:
    session_id: str
    def frames(self) -> AsyncIterator[str]       # encoded SSE text; today's event_gen, structure kept
    async def finish(self) -> None               # today's _end_attribution (Starlette background task)
class ChatTurnService:
    def __init__(self, graph: Any, *, container: "AppContainer | None", db_client: Any = None) -> None
        # db_client: only for today's UserModel(db=...) and chat_history(supabase=...) calls; Task 6 and Task 7 remove it
    @classmethod
    def for_app(cls, state: Any) -> "ChatTurnService"        # state.jarvis_graph, and getattr(..., None) for container, db_client
    async def open_turn(self, request: ChatStreamRequest, *, request_id: str) -> ChatTurn
        # in the handler's context: force_cloud_var, attribution and session creation happen here,
        # before StreamingResponse, so the stream inherits them
@dataclass
class TurnContext:
    history: list[dict]; memory_note: str; memories_shown: list[dict]
    since_last_spoke: str | None; user_message_id: str | None; memory_snapshot: Any
async def read_conversation(container, user_model, request: ChatStreamRequest, session_id: str) -> TurnContext
class ClosingStreamingResponse(StreamingResponse):
    # after streaming, or when cancelled: await self.body_iterator.aclose() inside anyio.CancelScope(shield=True)
# JarvisState gains: container: Any   (transient; the turn's AppContainer, or None)
```

**Rules:**
- **The endpoint.** It is exactly `turn = await ChatTurnService.for_app(http_request.app.state).open_turn(request, request_id=request_id_of(http_request))`, returning a `ClosingStreamingResponse(turn.frames(), media_type="text/event-stream", headers=<today's>, background=BackgroundTask(turn.finish))`.
- **What stays in `service.py`.** The lock acquisition, `_read_conversation` under the lock, the pump task, and the `finally` with `anyio.CancelScope(shield=True)`, pump cancel and `await asyncio.wait([pump_task])` then `turn_lock.aclose()`. All of it moves as one block, statements in today's order.
- **Module calls stay late-bound.** `chat_history` functions are called through the module (`chat_history.get_or_create_session(...)`), so `_stub_chat_io` keeps patching them until Task 6 replaces them.
- **The container goes in the turn state.** `initial_state["container"]` is set from `self._container`, so nodes can read it from Task 4 on. Nothing reads it yet in this task.
- **File size.** `service.py` stays at or under 300 lines, and the other two modules each at or under 250.

**Pinned behaviour (kept, re-pointed only):**
- Every test in `tests/test_turn_locks.py` whose name starts `test_chat_v2__`.
- `tests/test_checkpointer.py` (the chat-stream test at line 644).
- `tests/test_error_text_leaks.py` (the stream test at line 232).
- `tests/behaviour/test_chat_turns.py`, `test_short_term_memory.py`, `test_long_term_memory.py`, `test_turn_learning.py`, `test_prompt_layout.py`.

- [ ] **Step 1: Write failing tests** in `tests/test_chat_turn.py`:
  - `test_chat_router__routing_and_parsing_only`, an AST check:
    - `app/api/v1/endpoints/chat.py` is at most 80 lines.
    - It defines no function with more than 12 statements.
    - It imports nothing from `app.orchestrator`, `app.core.observation`, `app.services.memory` or `app.core.user_model`.
  - `test_chat_turn_service__turn_state__carries_the_container`: a recording graph sees `state["container"] is container`.
  - `test_chat_stream__client_gone_while_a_frame_waits_to_send__lock_released_without_gc`:
    - `gc.disable()`.
    - Serve over ASGI with a `send` that blocks on the second body chunk and a `receive` that returns `http.disconnect` after the first.
    - After the response coroutine returns: `turn_locks._locks == {}` and the graph saw its cancellation.
    - `gc.enable()` in the finally.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the service, the response class and the thin router. Re-point the imports in the pinned tests.
- [ ] **Step 4: Run** the new tests, the pinned files, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `refactor(chat): the router routes; ChatTurnService owns the turn; a closed tab never waits for GC to free its lock`.
- [ ] **Step 6: Orchestrator: live checkpoint.**

### Task 4: Drafts, one path (`DraftStore` retired)

**Files:**
- Create:
  - `app/services/planning/services.py` (`PlanningServices`)
  - `tests/test_drafts_one_path.py`
- Delete:
  - `app/services/draft_store.py`
  - `tests/test_draft_store.py`
  - `tests/test_draft_integration.py`
- Modify:
  - `app/services/draft_service.py`: add the methods below.
  - `app/services/draft_actions.py`:
    - `resolve_draft`, `accept_draft_on_time`, `accept_draft_and_persist` and `reject_draft` take `DraftService` (and a `DraftPlanner`) instead of a store and a Supabase client.
    - Delete `_draft_service`, `task_repository` (the legacy shim) and `_resolved_from_now`'s global lookup.
  - `app/orchestrator/graph.py::_apply_draft_action`, `_closed_draft_reply`: use `state["container"].draft_service` and `.planner`. A missing container gives today's "draft system is offline" reply.
  - `app/modules/planning_graph.py`:
    - `PlanningState.planning` replaces `draft_store`.
    - `planning_state_in` copies `state["container"].planning`.
    - `create_draft` calls `planning.drafts.propose(...)`.
  - `app/modules/planning_replan.py`: the constructor takes `planning: PlanningServices` and puts it in the state; no `DraftStore`.
  - `app/api/v1/endpoints/drafts.py`:
    - Every handler goes through `get_container(request).draft_service`.
    - Delete the `/chat` route.
    - `DELETE /{id}` is `discard`.
    - Raise `NotFoundError` and `ValidationError`, never `HTTPException`.
    - `reject`, `modify`, `delete`, `edit` and `rearrange` declare the response models below.
  - `app/schemas/draft.py`: `DraftRejectResponse`, `DraftEditResponse`, `DraftRearrangeResponse`, `DraftDiscardResponse`, each with today's fields.
  - `app/container.py`:
    - `planning: PlanningServices`, built before the planner.
    - `PlanningGraphReplanner(planning)`.
  - `app/main.py`: no `DraftStore`.
  - `app/orchestrator/state.py` and `checkpoint.py`: drop `draft_store`; `"planning"` becomes transient.
  - `app/services/chat_turn/service.py`: no `draft_store` in the initial state.
  - `tests/test_layering.py`: drop `_LEGACY_SHIM` and the `services/draft_actions.py` entry.
  - `tests/fakes.py`: `make_jarvis_state` drops `draft_store` and gains `container=None`.

**Interfaces:**
- Consumes: `JarvisState.container` (Task 3).
- Produces:
```python
@dataclass(frozen=True)
class PlanningServices:          # app/services/planning/services.py; every field a port or a service
    drafts: DraftService; tasks: TaskService; task_repo: TaskRepository; constraints: ConstraintRepository
    profiles: ProfileRepository; memory: MemoryService; energy: EnergyEstimator
# DraftService gains
async def propose(self, user_id: str, *, tasks: list[dict], horizon_start: str, goal_id: str | None,
                  schedule: dict | None) -> DraftSchedule
async def open_draft(self, user_id: str, draft_id: str | None = None) -> DraftSchedule | None   # named one, else newest open
async def edit_tasks(self, user_id: str, draft_id: str, tasks: list[dict]) -> DraftSchedule     # transition(MODIFIED) first
async def edit_task(self, user_id: str, draft_id: str, task_id: str, edits: dict) -> DraftSchedule   # NotFoundError: no such task
async def discard(self, user_id: str, draft_id: str) -> DraftSchedule                            # -> EXPIRED
# AppContainer gains: planning: PlanningServices
# PlanningGraphReplanner(planning: PlanningServices)   (replaces drafts=, db_provider=, memory_provider=)
```

**Rules:**
- **A closed draft is refused by `DraftSchedule.transition`** (`InvalidStateTransition`, 409) before any write. The repository's `replace_tasks` contract is unchanged in this task.
- **Draft id resolution.** `create_draft` returns `{"draft_id": draft.id}`. Failure is still non-fatal and logged, and the draft id is then None.
- **Re-point, don't rewrite.** In `test_orchestrator.py`, `test_planning_graph.py`, `test_checkpointer.py`, `test_draft_endpoints.py`, `test_core_pipeline.py` (draft tests) and `test_store_resilience.py`, a `DraftStore` or `_FakeDraftStore` becomes a real `DraftService` over `RepositoryFactory.in_memory()`, or over `build_test_container(fake)` for tests that read `FakeSupabase` rows. A test that monkeypatched `accept_draft_on_time` keeps doing so with the new signature.
- **What the deleted draft-store tests covered.** `test_draft_integration.py`'s behaviours already in `test_repository_contracts.py` and `test_draft_service.py` are not re-added. The two that are not get new tests below:
  - edit of a closed draft;
  - edit of an unknown task leaving the draft untouched.
- **Endpoint tests whose assertion changes by decision:**
  - In `tests/test_draft_endpoints.py`, `test_delete_draft` asserts the row is now `expired`; it no longer asserts the row is gone (decision 11).
  - `test_modify_unknown_component` expects 422 `validation_error` instead of 400 (decision 12).
  - `test_chat_modify__is_not_implemented` goes with its route.
  - `tests/test_store_resilience.py::test_draft_store__explicit_none_client__no_env_fallback` goes with `DraftStore`.

- [ ] **Step 1: Write failing tests** in `tests/test_drafts_one_path.py`:
  - `test_planning_turn__draft_proposed__chat_accept_resolves_it_by_id` (RF3): a planning turn, then "looks good" on the same thread through the behaviour harness. The proposed draft is accepted and its tasks are written.
  - `test_rest_accept__after_a_chat_proposal__second_accept_409_and_tasks_written_once` (RF3).
  - `test_edit_tasks__closed_draft__invalid_state_transition_and_unchanged`.
  - `test_edit_task__unknown_task__not_found_and_draft_unchanged`.
  - `test_discard__open_draft__expired_and_row_kept`.
  - `test_draft_routes__no_501_chat_route_and_every_route_typed`.
  - `test_replanner__built_with_planning_services__no_process_wide_lookup`: construct it with a stub `PlanningServices`; `replan` reaches `planning.drafts.propose`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** and re-point.
- [ ] **Step 4: Run** the full suite. Expected: PASS, including `tests/test_accept_on_time.py`, `tests/test_draft_endpoints.py`, `tests/test_draft_service.py` and `behaviour/test_chat_turns.py`.
- [ ] **Step 5: Commit** `refactor(drafts): one path, DraftService over DraftRepository; DraftStore retired`.
- [ ] **Step 6: Orchestrator: live checkpoint** (steps 1–4 and 12–13 exercise drafts).

### Task 5: Tasks, rules, goals and habit trackers, one path

**Raw calls removed (grep at HEAD, after Task 1):**
- `user_tasks`:
  - `api/v1/endpoints/tasks.py`: 268, 293, 322, 332, 361 (deleted with their routes) and 390.
  - `services/analytical/missed_deadlines.py`: 71, 109, 153, 181.
  - `services/analytical/mastery_tracker.py`: 38, 56, 182, 212.
- `task_completion_signals`: `tasks.py:423`, deleted.
- `behavioral_constraints`:
  - `api/v1/endpoints/habits.py:88`
  - `services/extraction/behavioral_store.py`: 52, 74, 106, 113.
- `habit_trackers`: `services/analytical/sm2_engine.py`: 78, 94, 145.
- `user_plan_updates`: `mastery_tracker.py:119`.

(`core/user_model.py` belongs to Task 7, because its constructor is an injection site.)

**Files:**
- Modify:
  - `app/domain/entities.py`: `HabitTracker`, `GoalDeadlineUpdate`.
  - `app/domain/ports.py`: `TaskRepository.list_for_user`, `TaskRepository.bring_back`, `GoalRepository`, `HabitTrackerRepository`.
  - `app/infrastructure/persistence/{supabase_repos,in_memory,factory,_mapping}.py`: `Repositories.goals`, `Repositories.habit_trackers`.
  - `app/services/task_service.py`: `bring_back`.
  - `app/services/analytical/missed_deadlines.py`, `mastery_tracker.py`, `sm2_engine.py`.
  - `app/services/extraction/behavioral_store.py`: delete `delete_behavioral_constraint` and `_get_supabase`.
  - `app/api/v1/endpoints/tasks.py`: delete `update_task`, `delete_task`, `clear_all_tasks`, `_record_completion_signal`, `_get_supabase` and the `TaskUpdateRequest`/`TaskDeleteRequest`/`TaskClearResponse` models. (The skip reason, review M4, already reaches `TaskService.skip` at HEAD.)
  - `app/api/v1/endpoints/habits.py`: typed response models `ConstraintListResponse`, `DueTrackersResponse`, `TrackerCompleteResponse`, `ConstraintDeleteResponse`.
  - `app/api/v1/endpoints/ingestion.py`: delete the `behavioral-constraint` route.
  - `app/modules/coach.py`: the mastery calls take repositories from the container it already looks up. Task 7 moves that lookup into the state.
  - `app/modules/planning_graph.py::mark_missed`: one call-signature change, no `_db`.
- Test:
  - `tests/test_repository_contracts.py`, `tests/test_task_service.py`, `tests/test_task_lifecycle.py`, `tests/test_data_isolation.py`, `tests/test_local_day_fixes.py`, `tests/test_modules.py`, `tests/test_planning_graph.py` (mark-missed tests).
  - New `tests/test_tasks_one_path.py`.

**Interfaces — Produces:**
```python
class HabitTracker(BaseModel):   # the habit_trackers columns
    id: str; user_id: str; habit_id: str | None; constraint_id: str | None; repetitions: int (ge=0)
    quality_last: int | None (0..5); ef: float (ge=1.3); next_interval_days: int (ge=0); last_done_at: datetime | None
class GoalDeadlineUpdate(BaseModel):
    user_id: str; goal_id: str | None; source: str; deadline_date: date | None
    deadline_raw: str | None; context_snippet: str | None; created_at: datetime | None = None
# TaskRepository
async def list_for_user(self, user_id: str, *, statuses: frozenset[TaskStatus] | None = None) -> list[Task]   # newest first, <= TASK_LIST_LIMIT
async def bring_back(self, user_id: str, task_id: str) -> Task     # CAS MISSED->PENDING, deadline_hint cleared, one write; StaleVersionError
class GoalRepository(ABC):
    async def get(self, user_id: str, goal_id: str) -> Goal | None
    async def latest_context(self, user_id: str, goal_id: str) -> str | None      # newest user_plan_updates.context_snippet
    async def add_deadline_update(self, user_id: str, update: GoalDeadlineUpdate) -> None
class HabitTrackerRepository(ABC):
    async def get(self, user_id: str, tracker_id: str) -> HabitTracker | None
    async def list_for_user(self, user_id: str) -> list[HabitTracker]              # bounded
    async def record_review(self, user_id: str, tracker_id: str, *, repetitions: int, ef: float,
                            next_interval_days: int, done_at: datetime) -> HabitTracker   # NotFoundError
# TaskService
async def bring_back(self, user_id: str, task_id: str) -> Task | None              # None: not missed any more
# services
async def detect_and_mark_missed(user_id: str, tasks: TaskRepository, task_service: TaskService, zone: ZoneInfo | None = None) -> list[dict]
async def restore_missed(user_id: str, tasks: TaskRepository, task_service: TaskService) -> list[dict]
async def calculate_success_ratio(user_id: str, goal_id: str | None, task_events: TaskEventRepository) -> float
async def compute_quality_trend(user_id: str, task_events: TaskEventRepository) -> str
async def get_mastery_summary(user_id: str, tasks: TaskRepository, task_events: TaskEventRepository, goals: GoalRepository) -> dict[str, float]
async def record_completion(trackers: HabitTrackerRepository, user_id: str, tracker_id: str, quality: int) -> dict
async def get_due_trackers(trackers: HabitTrackerRepository, user_id: str, zone: ZoneInfo) -> list[dict]
async def store_behavioral_constraint(constraints: ConstraintRepository, habit: str, *, user_id: str, constraint_type: str = "habit") -> dict
async def get_behavioral_context_for_calendar(constraints: ConstraintRepository, user_id: str) -> str
```

**Rules:**
- **Success ratio.** Over the latest outcome per slot (`task_service.latest_outcome_per_slot`) of the last 60 days, completed divided by (completed + skipped + missed). It is 0.5 with no outcomes.
- **Average quality and trend.** From completed events' `metadata["quality"]`: newest 5 against the 5 before. Fewer than 4 gives `"stable"`. The thresholds (±0.5) are today's.
- **Deadline scan.** It always goes through `TaskService.mark_deadline_missed`; the no-service write path is deleted.
- **Without a container, `mark_missed` and the bring-back phrase do nothing.** They log at debug level and return no misses (Null behaviour). The `test_planning_graph.py` tests that patch `detect_and_mark_missed` or `restore_missed` register a `build_test_container()` first. Until Task 7 they use `runtime.set_container`; Task 7 moves them to the state.
- **`GET /tasks/`.** It returns `Task.model_dump(mode="json")` rows. The `status` query is `TaskStatus | None`.
- **Rules list.** `GET /habits/constraints` returns `ConstraintRepository.list_active` as `{id, raw_text, constraint_type, created_at}`, newest first. Expired rules are no longer listed, which matches the You page (review I2).
- **Outages.** A store outage is `PersistenceError` (503) everywhere these lists are read.

**Tests that go:**
- `test_task_lifecycle.py`: `test_patch_task__unknown_status__422`, `test_patch_task__status__422_points_to_complete_and_skip`, `test_patch_task__pacing_pushed__422`; the PATCH and DELETE cases of `test_task_endpoints__any_change__schedule_no_replan_and_claim_none` and `test_task_endpoints__unknown_task__not_found_envelope`; and the `DELETE /tasks/` case of `test_openapi__task_and_draft_accept_routes__declare_a_response_model`.
- `test_schema_alignment.py::test_complete_task__completion_signal__written_with_existing_columns_only`.

**Tests kept and re-pointed:**
- `test_data_isolation.py::test_tracker_complete__wrong_user__404`, `test_tracker_due__as_alice__only_alices_trackers`, `test_habits_lists__db_unavailable__503_not_an_empty_list`: over `FakeSupabase` through the container.
- `test_local_day_fixes.py` streak tests.
- `test_modules.py::test_coach_module_returns_message`.
- `test_task_lifecycle.py` list, constraint and complete/skip tests.
- `test_planning_graph.py` mark-missed and restore tests: new signatures.

- [ ] **Step 1: Write failing tests:**
  - Contract tests, both families:
    - `test_tasks__list_for_user__newest_first_bounded_and_status_filtered`
    - `test_tasks__bring_back__missed_to_pending_deadline_cleared`
    - `test_tasks__bring_back__not_missed__stale_version`
    - `test_goals__latest_context__newest_update_for_that_goal_only`
    - `test_goals__add_deadline_update__user_scoped`
    - `test_trackers__record_review__other_users_tracker__not_found`
    - `test_trackers__list_for_user__only_theirs`
  - In `tests/test_tasks_one_path.py`:
    - `test_list_tasks__seeded_size__every_field_the_dashboard_reads_newest_first` (RF5): 90 tasks. Each row has `task_id, title, status, duration_minutes, scheduled_start, scheduled_end, goal_id, deadline_hint`.
    - `test_habits_constraints__same_rules_as_the_you_page` (RF5): an expired rule is in neither list.
    - `test_task_routes__patch_and_deletes__gone_405_or_404`: `PATCH` and `DELETE /tasks/t1` give 404; `DELETE /tasks/` gives 405, because `GET /tasks/` remains. Both come back in the envelope.
    - `test_mastery__quality_from_completed_events__trend_improving`.
    - `test_success_ratio__latest_outcome_per_slot__missed_then_completed_counts_once`.
    - `test_deadline_scan__always_through_task_service__deadline_missed_event_per_task`.
    - `test_restore_missed__through_bring_back__pending_and_deadline_cleared`.
    - `test_tasks_and_habits_endpoints__no_raw_table_calls`: AST check over `tasks.py`, `habits.py`, `missed_deadlines.py`, `mastery_tracker.py`, `sm2_engine.py`, `behavioral_store.py`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the ports, both families, the services, the endpoints and the call sites.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `refactor(tasks): tasks, rules, goals and habit trackers each have one persistence path`.

### Task 6: Sessions, messages, documents and pending calendar, one path

**Raw calls removed:**
- `chat_sessions` and `chat_messages`:
  - `services/chat_history.py`: 97, 117, 155, 167, 173, 183, 211, 224, 253, 326.
  - `api/v1/endpoints/sessions.py`: 52, 100, 118, plus the archive at 131+.
- `ingested_documents` and `task_materials`:
  - `api/v1/endpoints/documents.py`: 29, 47, 100, 107.
  - `services/extraction/orchestrator.py:232`.
  - `services/extraction/task_material_linker.py:85`.
  - `services/analytical/workspace_builder.py:93`.
- `pending_calendar_updates`: `api/v1/endpoints/ingestion.py`: 168, 188; `services/extraction/orchestrator.py:138`.
- `user_tasks`: `services/extraction/orchestrator.py:247`, `task_material_linker.py:44`, `workspace_builder.py`: 298, 356.
- `user_plan_updates`: `services/extraction/orchestrator.py:267`, `workspace_builder.py:375`.

**Files:**
- Create:
  - `app/services/conversation_service.py`: `ConversationService`, with today's session titling, save rules and error handling, moved from `chat_history.py`.
  - `app/services/documents/service.py`: `DocumentService`; the Chroma chunk delete moves here from the endpoint.
  - `tests/test_conversations_one_path.py`.
- Delete:
  - `app/services/chat_history.py`
  - `tests/test_chat_history.py`: its 26 tests are re-homed. Title and truncation tests move to `ConversationService` tests; store tests become repository contract tests.
- Modify:
  - `app/domain/entities.py`:
    - `ConversationSession.pinned: bool = False` and `archived: bool = False` (columns `is_pinned`, `is_archived`).
    - New `IngestedDocument`, `TaskMaterial`, `PendingCalendarUpdate`.
  - `app/domain/ports.py`: `ConversationRepository` writes, `DocumentRepository`, `PendingCalendarRepository`.
  - `app/infrastructure/persistence/*`.
  - `app/services/extraction/orchestrator.py`: `process_ingestion(..., stores: IngestionStores)` replaces `db_client`.
  - `app/services/extraction/task_material_linker.py`, `app/services/analytical/workspace_builder.py`: task, material and goal reads through ports. The `task_workspaces` cache stays (decision 10).
  - `app/modules/knowledge_graph.py`: `knowledge_state_in` passes `container.ingestion` and no `user_model._db`.
  - `app/api/v1/endpoints/{sessions,documents,ingestion}.py`: through the services and repositories, with typed response models and `JarvisError`s. Lists answer 503 on an outage, never `[]` (spec §10).
  - `app/services/chat_turn/{service,context}.py`: sessions and messages through `container.conversation_service`.
  - `app/container.py`: `conversation_service: ConversationService`, `documents: DocumentService`, `ingestion: IngestionStores`.
- Test:
  - `tests/test_repository_contracts.py`, `tests/test_chat_sessions_integration.py`, `tests/test_document_integration.py`, `tests/test_data_isolation.py` (pending-calendar tests).
  - `tests/test_turn_locks.py`: `_stub_chat_io` is replaced by `app_state.container = build_test_container()`, an InMemory family that holds the session and messages.
  - `tests/test_checkpointer.py`, `tests/test_error_text_leaks.py`, `behaviour/test_short_term_memory.py`: chat-history patch targets become the container's repository.

**Interfaces — Produces:**
```python
# ConversationRepository gains
async def create_session(self, user_id: str, *, title: str | None) -> ConversationSession
async def add_message(self, user_id: str, session_id: str, role: Literal["user", "assistant"], content: str,
                      metadata: dict | None = None) -> ChatMessage     # also bumps the session's updated_at (activity)
async def list_sessions(self, user_id: str, *, limit: int = 50) -> list[ConversationSession]   # unarchived; pinned first, then updated_at desc
async def update_session(self, user_id: str, session_id: str, *, title: str | None = None,
                         pinned: bool | None = None, archived: bool | None = None) -> bool
async def archive_all(self, user_id: str) -> int
class ConversationService:
    def __init__(self, conversations: ConversationRepository) -> None
    async def get_or_create_session(self, user_id: str, session_id: str | None, first_message: str) -> str
    async def save_user_message(self, user_id: str, session_id: str, content: str) -> str | None
    async def save_assistant_message(self, user_id: str, session_id: str, content: str, intent: str, metadata: dict) -> str | None
    async def recent_window(self, user_id: str, session_id: str, *, max_messages: int = 10, max_chars: int = 500) -> list[dict]
class DocumentRepository(ABC):
    async def list_documents(self, user_id: str) -> list[IngestedDocument]               # with linked task counts, as the page shows
    async def upsert_document(self, user_id: str, doc: IngestedDocument) -> None
    async def delete_document(self, user_id: str, source_id: str) -> bool                  # removes its task_materials links too
    async def link(self, user_id: str, material: TaskMaterial) -> None                     # upsert on (user_id, task_id, source_id)
    async def materials_for_task(self, user_id: str, task_id: str) -> list[TaskMaterial]
class PendingCalendarRepository(ABC):
    async def add(self, user_id: str, update: PendingCalendarUpdate) -> PendingCalendarUpdate
    async def list_pending(self, user_id: str) -> list[PendingCalendarUpdate]
    async def resolve(self, user_id: str, pending_id: str, status: Literal["approved", "rejected"]) -> PendingCalendarUpdate  # NotFoundError: absent or not the user's
@dataclass(frozen=True)
class IngestionStores:   # app/services/extraction/orchestrator.py
    documents: DocumentRepository; pending_calendar: PendingCalendarRepository; constraints: ConstraintRepository
    goals: GoalRepository; tasks: TaskRepository
class DocumentService:
    def __init__(self, documents: DocumentRepository) -> None
    async def list(self, user_id: str) -> list[IngestedDocument]
    async def delete(self, user_id: str, source_id: str) -> None    # Chroma chunks (user-scoped) then the rows; NotFoundError
```

**Rules:**
- **Saves stay best-effort inside a turn.** A save failure inside a turn is logged and returns None, as today. Endpoint reads raise `PersistenceError`.
- **The session list sorts pinned first.**
- **The pending-calendar approve and reject handlers keep the IDOR behaviour.** Another user's row is a 404 and stays pending.

- [ ] **Step 1: Write failing tests:**
  - Contract tests for every new method, both families (one `__user_scoped` case each).
  - In `tests/test_conversations_one_path.py`:
    - `test_sessions_list__pinned_first_then_recent__archived_hidden_and_outage_503` (RF5)
    - `test_documents_list__same_fields_as_before_and_outage_503` (RF5)
    - `test_document_delete__chunks_and_links_gone__other_users_untouched`
    - `test_turn__session_and_both_messages__through_the_conversation_repository`
    - `test_ingestion__calendar_extracted__pending_row_through_the_repository`
    - `test_sessions_documents_ingestion__no_raw_table_calls`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement**, and re-home the `chat_history` tests.
- [ ] **Step 4: Run** the full suite. Expected: PASS, including every `tests/test_turn_locks.py` test (RF2) on the new stub.
- [ ] **Step 5: Commit** `refactor(conversations): sessions, messages, documents and pending calendar each have one persistence path`.
- [ ] **Step 6: Orchestrator: live checkpoint** (it exercises sessions: steps 7, 11, 11a).
- **If cutting:** the documents and pending-calendar half can be dropped. Do sessions and messages first and commit them on their own.

### Task 7: Injection: no global lookups, settings from the composition root

**Every `registered_container()` site at HEAD, and its new source:**

| Site | Becomes |
|---|---|
| `core/observation.py:209` (`persist_turn_memories`), `:382` (`look_for_patterns`), `:406` (`drain_pattern_frames`), `:486` (`run_observation_loop`) | `state["container"]` |
| `core/observation.py:339` `pearl_insights(user_id)` | `pearl_insights(patterns: PatternService \| None, user_id)`; `ChatTurnService` passes `container.patterns` |
| `core/model_router.py:129` `metered_web_search` | a `bus: EventBus \| None` parameter; `workspace_builder._search_web` gets it from its callers (the workspace endpoint's container, the research node's state) |
| `modules/coach.py:70, 112` | `state["container"]` |
| `modules/planning_graph.py:152` (`mark_missed`), `:174` (`_learned_focus`), `:388` (`_memory_slots`) | `state["planning"]` |
| `orchestrator/graph.py:287` (`_known_memories`), `:480` (`store_constraint`) | `state["container"]` |
| `services/draft_actions.py:149, 236, 250` | gone in Task 4 |
| `services/memory/extractor.py:138`, `control_policy.py:1270` | deleted in Task 1 |

**Files:**
- Delete:
  - `app/core/runtime.py`
  - the conftest fixture `_clean_runtime_registry`
- Create: `app/core/settings.py`; `tests/test_injection.py`.
- Modify:
  - every site in the table;
  - `app/core/user_model.py`;
  - `app/orchestrator/graph.py::_load_context`, which builds the facade from `state["container"].planning`;
  - `app/modules/planning_replan.py`;
  - `app/main.py`: no `set_container` or `set_shared_clients`;
  - `app/services/chat_turn/service.py`: builds `UserModel.for_planning`, and its `db_client` parameter goes;
  - `app/modules/knowledge_graph.py`, `app/modules/research_graph.py`;
  - `app/container.py`: settings;
  - the eight container-built services that read config: `task_service.py`, `memory/memory_service.py`, `memory/conversation_memory.py`, `memory/patterns.py`, `user_model_service.py`, `anticipation/{scheduler,service,triggers}.py`.
- Test:
  - Every test that calls `runtime.set_container` or `set_shared_clients` (grep). These are in `test_accept_on_time.py`, `test_planning_graph.py`, `test_observation.py`, `test_local_day_fixes.py`, `test_patterns.py`, `test_triggers.py`, `test_stated_rules.py`, `test_task_service.py`, `test_llm_call_sites.py`, `test_memory_constraint_bridge.py`, `test_container.py`, `behaviour/conftest.py` and `behaviour/test_learning.py`. Each passes the container in the node's state, or on `app.state`.
  - `test_container.py::test_runtime__reset_shared_clients__forgets_the_container` goes with the module.
  - The one test that monkeypatches `config.CONVERSATION_SUMMARY_TOKENS` passes `ConversationSettings` instead.

**Interfaces:**
- Consumes: `JarvisState.container` (Task 3), `PlanningServices` (Task 4), `TaskRepository.list_for_user` (Task 5).
- Produces:
```python
class UserModel:   # app/core/user_model.py; the facade over ports (no _db, no raw reads)
    def __init__(self, user_id: str, *, profiles: ProfileRepository, constraints: ConstraintRepository,
                 tasks: TaskRepository) -> None
    @classmethod
    def for_planning(cls, user_id: str, planning: PlanningServices) -> "UserModel"
    async def get_profile(self) -> UserProfile | None          # profiles.get_or_create; None on PersistenceError (logged)
    async def get_behavioral_constraints(self) -> list[dict]   # constraints.list_active, model_dump(mode="json")
    async def get_pending_tasks(self) -> list[dict]            # tasks.list_pending
    async def get_missed_tasks(self) -> list[dict]             # tasks.list_by_status({MISSED})
    async def get_all_tasks(self) -> list[dict]                # tasks.list_for_user
    async def get_estimated_energy(self) -> float
    def invalidate(self, key: str) -> None
# app/core/settings.py
@dataclass(frozen=True) class TaskSettings: slot_grace_minutes: int; sweep_lookback_days: int
@dataclass(frozen=True) class MemorySettings: dedupe_similarity: float; profile_tokens: int; recall_min_similarity: float
                                               recall_tokens: int; retention_floor: float; suppress_similarity: float
                                               summary_suppress_similarity: float
@dataclass(frozen=True) class ConversationSettings: compact_after_messages: int; compact_keep_recent: int
                                                     summary_tokens: int; history_max_chars_per_message: int
                                                     history_max_messages: int; idle_minutes: int; summary_lookback_days: int
                                                     summary_rescan_hours: int; summary_input_max_chars: int
                                                     summary_page_messages: int; summary_retry_minutes: int
@dataclass(frozen=True) class FocusSettings: window_days: int; prior_strength: float; min_observations: int
                                              # PatternService and UserModelService read window_days; the container passes
                                              # all three to HistoryEnergyEstimator's existing keyword parameters
@dataclass(frozen=True) class AnticipationSettings: tick_seconds: float; manual_cooldown_seconds: float
                                                     morning_brief_window_hours: int
@dataclass(frozen=True) class Settings: tasks: TaskSettings; memory: MemorySettings; conversation: ConversationSettings
                                         focus: FocusSettings; anticipation: AnticipationSettings
    @classmethod
    def from_config(cls, cfg: ModuleType) -> "Settings"      # the one place config values become settings
def default_settings() -> Settings                           # Settings.from_config(app.core.config); constructor defaults only
# each of the eight services: __init__(..., settings: <Its>Settings | None = None)  ->  settings or default_settings().<its>
```

**Rules:**
- **With no container, a feature is off.** A node logs at debug level, once per turn, and returns its empty result. It never builds or reaches a store itself.
- **The eight services stop reading config.** They no longer import `app.core.config`. Their values come from `self._settings`.
- **The settings step can be dropped** (see the task map). If it is dropped, the injection part still lands.

- [ ] **Step 1: Write failing tests** in `tests/test_injection.py`:
  - `test_runtime_registry__gone_and_imported_nowhere` (RF4): `find_spec("app.core.runtime") is None`, no file under `app/` or `tests/` imports `app.core.runtime`, and none names `registered_container`, `set_container`, `set_shared_clients`, `get_db` or `get_memory_store`.
  - `test_turn__container_only_in_turn_state__rule_memory_and_pattern_reach_the_turn` (RF4): a behaviour-harness Jarvis with only `app.state.container`.
    - Teach "no work before 11 AM", then plan: no slot before 11:00 local.
    - A fact said in session A is in session B's context note.
    - After three skips at 09:00, a `pattern_detected` frame arrives before `complete`.
  - `test_planning_nodes__read_planning_services_from_state__none_means_no_slots_no_focus_no_scan`.
  - `test_user_model__over_ports__no_db_attribute`.
  - `test_metered_web_search__bus_passed__usage_published_and_no_bus_nothing`.
  - `test_container__services_get_settings_from_the_composition_root`: a `cfg` stub with `SLOT_GRACE_MINUTES = 5` gives a task service grace of 5 minutes.
  - `test_container_built_services__import_no_config_module`: an AST check over the eight files.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** each site, the facade, the settings, and the test re-points.
- [ ] **Step 4: Run** the full suite. Expected: PASS, including `behaviour/*`, `tests/test_observation.py`, `tests/test_planning_graph.py` and `tests/test_seed_demo_user.py` (the seed builds services with default settings).
- [ ] **Step 5: Commit** `refactor(di): collaborators and settings come from the composition root; no global lookups`.
- [ ] **Step 6: Orchestrator: live checkpoint.**

### Task 8: API contract: typed responses, one error envelope, layering at (near) empty

**Files:**
- Create: `app/services/planning/scheduling.py`; `app/schemas/schedule.py`.
- Modify:
  - `app/api/v1/endpoints/schedule.py`: only the endpoint and `ScheduleRequest`.
  - `app/modules/planning_graph.py`, `app/services/analytical/schedule_modifier.py`, `app/modules/planning_replan.py`, `app/utils/deadline_parser.py` and `scripts/seed_demo_user.py`: import from `services/planning/scheduling` and `app/schemas/decomposition`.
  - `app/api/v1/endpoints/reasoning.py`: drop its `noqa: F401` re-exports of the decomposition schemas once nothing imports them from there.
  - `app/api/v1/endpoints/{ingestion,workspace,reasoning,models}.py`, `app/main.py` (`/`, `/health`): typed response models, and `JarvisError`s instead of `HTTPException`.
  - `app/api/v1/endpoints/chat.py`: the stream route declares `response_class=StreamingResponse`, plus `responses={200: {"content": {"text/event-stream": {"schema": ChatCompletePayload.model_json_schema()}}}}`.
  - `app/core/config.py`: `MAX_PROMPT_CHARS = 20_000`, `MAX_UPLOAD_BASE64_CHARS = 28_000_000`.
  - `app/schemas/chat_stream.py::ChatStreamRequest`:
    - `user_prompt` `max_length=MAX_PROMPT_CHARS`;
    - `model_mode: Literal["auto", "4b", "27b", "cloud", "gemini"] = "auto"`;
    - `file_base64` `max_length=MAX_UPLOAD_BASE64_CHARS`;
    - `media_type: Literal["pdf", "image", "png", "jpeg"] | None`.
  - `IngestionRequest`: the same file bounds.
  - `app/schemas/draft.py::DraftTaskEdit`: `duration_minutes` in [1, 240], `start_min >= 0` (spec §10).
  - `tests/test_layering.py`.
- Test:
  - `tests/test_scheduler.py`, `tests/test_local_time.py`, `tests/test_llm_call_sites.py`, `tests/test_draft_service.py`, `tests/test_seed_demo_user.py`: import paths.
  - `tests/test_planning_graph.py`:
    - The 11 patches of `"app.api.v1.endpoints.schedule.run_schedule"` become `"app.services.planning.scheduling.run_schedule"`.
    - `test_solve_schedule__422_maps_to_infeasible` raises `ScheduleInfeasibleError`.
    - `test_solve_schedule__non_422_http_error__propagates` becomes `test_solve_schedule__other_error__propagates`.
    - The ladder test at line ~950 uses `ScheduleInfeasibleError`.
  - `tests/test_errors.py`: the HTTP-exception handler tests stay; the handler is still registered for Starlette's own 404 and 405.
  - `tests/test_error_text_leaks.py`: the base64 test expects 422; the classifier test expects 503 `llm_unavailable`.
  - New `tests/test_api_contract.py`.

**Interfaces — Produces:**
```python
# app/services/planning/scheduling.py (moved from the endpoint; behaviour unchanged except as stated)
def run_schedule(graph: ExecutionGraph, daily_context: list[TimeSlot], horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
                 horizon_start: datetime | None = None, max_daily_deep_work_minutes: int | None = None,
                 min_daily_deep_work_minutes: int | None = None, max_task_duration_minutes: int | None = None,
                 min_task_duration_minutes: int | None = None, zone: ZoneInfo | None = None,
                 block_past: bool | None = None, focus_by_hour: list[float] | None = None,
                 success_rate: float = 0.5) -> GenerateScheduleResponse        # raises ScheduleInfeasibleError
def current_horizon_start(zone: ZoneInfo | None = None) -> datetime
def logical_horizon_start(zone: ZoneInfo | None = None) -> datetime          # today's _compute_horizon_start, made public
# app/schemas/schedule.py: ScheduledTask, GenerateScheduleResponse (moved)
```

**Layering test, final allow-lists (asserted exactly):**
- `_INFRASTRUCTURE_IMPORTS_ALLOWED == {"core/model_router.py": ...}`.
- `_API_IMPORTS_ALLOWED == {}`, with the scan extended to `services`, `domain`, `infrastructure`, `modules`, `orchestrator`, `core`, `utils` and `schemas`.
- `_SUPABASE_DRIVER_ALLOWED == {}`.
- `_LEGACY_LLM_CALLS_ALLOWED == {}`.
- `_WEB_SEARCH_CALLS_ALLOWED == {("services/analytical/workspace_builder.py", "_search_web"): ...}`.
- New `_RAW_TABLE_CALLS_ALLOWED == {"services/analytical/workspace_builder.py": "task_workspaces cache (decision 10)", "services/user_preferences.py": "learning_style read (decision 10)"}`. The scan counts any `.table(` call in `api`, `services`, `core`, `modules` and `orchestrator`. A stale entry fails too.

If Task 6's second half was dropped, its files join this list with the reason "Plan 3 cut".

- [ ] **Step 1: Write failing tests:**
  - In `tests/test_api_contract.py`:
    - `test_openapi__every_json_route__declares_a_typed_response_model`: every `APIRoute` except the stream has a `response_model` that is a `BaseModel` subclass (not `dict`, not None).
    - `test_openapi__chat_stream__documents_the_complete_frame`.
    - `test_endpoints__raise_no_http_exception`: an AST check. No name `HTTPException` under `app/api/v1/endpoints/`.
    - `test_endpoints__no_ad_hoc_error_bodies`: no `JSONResponse(` and no `{"status": "error"` literal under `app/api/v1/endpoints/`.
    - `test_chat_stream__unknown_model_mode__422_envelope`.
    - `test_chat_stream__oversized_upload__422_envelope`.
    - `test_draft_task_edit__duration_out_of_bounds__422`.
    - `test_run_schedule__infeasible__schedule_infeasible_error_and_generate_endpoint_422_envelope`.
  - In `tests/test_layering.py`: `test_layering__allow_lists__are_exactly_the_documented_leftovers`, and `test_layering__no_raw_table_calls_outside_the_allow_list` (with a tmp-path self-test like `test_layer_report__...`).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the move, the models, the errors, the bounds and the layering rules.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `refactor(api): a typed response on every route, one error envelope, and layering allow-lists at their documented leftovers`.

### Task 9 (droppable): Orchestration structure: `CognitiveModule` registry + `planning_graph` package

**Files:**
- Modify:
  - `app/core/module_framework.py`: the `CognitiveModule` ABC; `ModuleDefinition` implements it; `ModuleRegistry.routes`.
  - `app/orchestrator/routing.py`: `BUILTIN_ROUTES`, `intent_routes`; delete `INTENT_TO_MODULE`.
  - The `planning_module`, `research_module` and `knowledge_module` definitions declare `intents`.
  - `tests/test_orchestrator.py`: the `INTENT_TO_MODULE` uses at 74, 352 and 1455 become `intent_routes(module_registry)`.
- Create: `app/modules/planning_graph/` (a package replacing `planning_graph.py`):
  - `__init__.py`: re-exports the public names tests import; no logic.
  - `state.py`: `PlanningState`, `planning_state_in`/`_out`, `HABITS_UNREAD_NOTE`, `habits_unread`.
  - `constraints.py`: `fetch_constraints`, `_rules_of`, `plan_time_slots`, `translate_habits`, `expand_slots`, `memory_to_constraints`, `_memory_slots`, the precomputed-slot helpers.
  - `decompose.py`: `validate_goal`, `is_goal_clear`, `DECOMPOSITION_SYSTEM_PROMPT`, `decompose_goal`, `fuse_tasks`.
  - `solve.py`: `mark_missed`, `solve_schedule`, `create_draft`, `handle_infeasible`, `report_unsolved`, `check_feasibility`, `can_retry`, `HORIZON_RETRY_SEQUENCE`, `SOLVE_STEP_TIMEOUT_MS`, `compact_schedule`.
  - `definition.py`: `planning_module`, `build_planning_graph`.
- Test: `tests/test_module_registration.py`, `tests/test_orchestrator.py`; new `tests/test_cognitive_modules.py`.

**Interfaces — Produces:**
```python
class CognitiveModule(ABC):
    name: str
    intents: frozenset[str]
    @abstractmethod
    def build_graph(self) -> Any: ...
@dataclass
class ModuleDefinition(CognitiveModule):   # existing fields, plus:
    intents: frozenset[str] = frozenset()
    def build_graph(self) -> Any            # build_module_graph(self)
class ModuleRegistry:
    def routes(self) -> dict[str, str]      # intent -> module name; register() raises ValueError when an intent is already claimed
BUILTIN_ROUTES: dict[str, str]              # draft_action x4, store_constraint, coach_module, conversation_module x3
def intent_routes(registry: ModuleRegistry) -> dict[str, str]   # BUILTIN_ROUTES | registry.routes(); a clash raises
```
Intents: planning `{"PLAN_DAY", "ADD_CONSTRAINT"}`; knowledge `{"CALENDAR_SYNC", "KNOWLEDGE_INGESTION", "ACTION_ITEM"}`; research `{"RESEARCH"}`.

**Rules:**
- **Module files resolve collaborators at call time.** A `planning_graph/` submodule calls other modules' functions through the module (`md.detect_and_mark_missed`, `scheduling.run_schedule`), so every existing patch target keeps working. `tests/test_planning_graph.py` changes no patch target.
- **Size.** Each submodule is at or under 300 lines.

- [ ] **Step 1: Write failing tests** in `tests/test_cognitive_modules.py`:
  - `test_routing__derived_table__equals_the_fifteen_routes_of_today`: pin every `IntentType` to its destination as at HEAD.
  - `test_routing__new_module_registered__its_intent_routes_without_editing_routing`.
  - `test_registry__two_modules_claim_one_intent__refused`.
  - `test_planning_package__every_submodule_at_most_300_lines`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the registry first, then the package move. Make two commits:
  - `refactor(modules): modules declare their intents; routing is derived (OCP)`
  - `refactor(planning): the planning module is a package of focused files`

  Either commit can stand alone.
- [ ] **Step 4: Run** the full suite after each commit. Expected: PASS.

### Task 10: Carried fixes: the streak from the backend, unslotted tasks, the Patterns tile

**Files (backend, after Task 7):**
- `app/services/analytical/mastery_tracker.py`: `completion_streak`, moved from `coach._completion_streak`.
- `app/modules/coach.py`: calls it.
- `app/services/user_model_service.py`: `YouView.streak_days`.
- `app/api/v1/endpoints/you.py`: `YouResponse.streak_days`.
- Test: `tests/test_you_api.py`, `tests/test_local_day_fixes.py`.

**Files (frontend; the unslotted and Patterns steps can start any time after Task 1's frontend commit):**
- Create: `lib/schedule.ts`; `lib/__tests__/schedule.test.ts`.
- Modify:
  - `app/(app)/schedule/page.tsx`: the Day, Week and Month grids draw only slotted tasks; a "Not scheduled yet" strip lists the rest.
  - `lib/contracts.ts::parseYouResponse`: reads `streak_days`, an optional non-negative integer.
  - `lib/types.ts`.
  - `app/(app)/dashboard/page.tsx`: `streakDays` from `you.streak_days`, null until known.
  - `components/app/StatsStrip.tsx`: `streakDays: number | null`, showing "—" while unknown.
  - `lib/dashboard.ts`: delete `completionStreak`.
  - `lib/__tests__/dashboard.test.ts`: drop its `completionStreak` block.
  - `lib/__tests__/contracts.test.ts`.

**Interfaces — Produces:**
```python
async def completion_streak(task_events: TaskEventRepository, user_id: str, zone: ZoneInfo, now: datetime) -> int
    # STREAK_WINDOW_DAYS of completed events, compute_streak_from_events; the coach and /you share it
# YouResponse gains: streak_days: int = Field(ge=0, description="Consecutive local days with something completed.")
```
```ts
export function tasksOnDay(tasks: ScheduleTask[], day: Date): ScheduleTask[]        // never an `unscheduled` task
export function unscheduledPending(tasks: ScheduleTask[]): ScheduleTask[]          // pending and unscheduled, sorted by title
export function streakLabel(days: number | null): string                            // lib/dashboard.ts: "—" | "1 day" | "N days"
```

- [ ] **Step 1: Write the failing backend tests:**
  - `test_you__streak_days__the_number_the_coach_uses`
  - `test_you__streak__completion_at_00_30_ist_counts_for_that_local_day`
  - `test_you__patterns__one_card_per_pattern_key`
- [ ] **Step 2: Write the failing frontend tests:**
  - `tasksOnDay never includes an unslotted task`
  - `unscheduledPending lists pending unslotted tasks only`
  - `parseYouResponse reads streak_days and rejects a negative one`
  - `streakLabel shows — until /you answers, then 1 day / N days`
- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/test_you_api.py -q` and `npm test`. Expected: FAIL.
- [ ] **Step 4: Implement.** The frontend reads `streak_days` only when present, so the frontend can land before the backend restart.
- [ ] **Step 5: The Patterns tile (droppable).**
  1. Orchestrator only: run a read-only probe in the scratchpad that prints `PatternService.current("demo")` pattern keys against the live database. `GET /you` writes, so it is not used.
  2. Expected from the code: a fourth card is a real `repeated_skip:<task_id>` pattern. The tile then already equals the You page's card count, and the only change is the pinning test above.
  3. If the probe shows a duplicate key or a dismissed pattern, write the failing test for that case first, then fix `PatternService.current`.
- [ ] **Step 6: Run** the full suite and the frontend checks. Expected: PASS.
- [ ] **Step 7: Commit** in both repos:
  - `feat(you): the streak comes from the outcome history, the same number the coach uses`
  - `fix: the schedule never draws an unslotted task at a made-up 8 AM; the streak comes from /you`

### Task 11: Live E2E, the alias removed, the docs (orchestrator)

**Files:**
- `app/api/v1/endpoints/chat.py`: delete the `/v2/stream` alias.
- `tests/test_routes.py`.
- `docs/PROJECT_STATUS.md`.
- `.claude/CLAUDE.md`.

- [ ] **Step 1: Remove the alias.** Rename `test_routes__chat__stream_and_hidden_alias_are_the_one_turn_handler` to `test_routes__chat__only_the_stream_remains`: the set of `/api/v1/chat` paths is `{"/api/v1/chat/stream"}`. Run the full suite. Expected: PASS.
- [ ] **Step 2: Restart and run live.** Restart the backend and run `scripts/live_e2e.py`. Expected: ALL 18 PASSED and a server log with no tracebacks.
- [ ] **Step 3: Check the pages in a headless browser.** `/dashboard` (streak, Patterns tile, Today), `/schedule` (no 8 AM ghost; the "Not scheduled yet" strip), `/chat` (a greeting streams; a brain dump shows a draft and Accept), `/you`. Save the screenshots in the scratchpad.
- [ ] **Step 4: Update `docs/PROJECT_STATUS.md`:**
  - the repository layout (deleted modules, `services/chat_turn/`, `services/planning/{goal_ids,brain_dump_prompt,scheduling,services}.py`, `core/settings.py`, no `core/runtime.py`);
  - the request flow on `/api/v1/chat/stream`;
  - the endpoint table, from the keep/delete table above;
  - the new ports;
  - the layering allow-lists;
  - the test counts;
  - Known Issues, minus what this plan closed.
- [ ] **Step 5: Update `.claude/CLAUDE.md`:**
  - the Repository Layout, the Request Flow diagram, the API Endpoints table and "Architecture Decisions to Preserve";
  - `/api/v1/chat/stream` as the single entry point;
  - `run_schedule` in `app/services/planning/scheduling.py`;
  - the composition root passed through turn state and constructors, never looked up;
  - the test count;
  - the deprecated-v1 rows deleted.
- [ ] **Step 6: Commit** `docs: the project status and CLAUDE.md describe the finished structure` in Jarvis-Engine.
