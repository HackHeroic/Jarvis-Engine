# Capstone Plan 2 — Make the Vision Visible (core) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jarvis keeps a true record of what happens to every task, learns the user's focus hours and habits from that record, remembers what the user tells it across conversations (and forgets on request, for good), and makes the first move each morning. All of it shows on a new You page and a "While you were away" panel.

**Architecture:** One append-only outcome history (`task_events`) written by `TaskService`, `DraftService` and a `SlotPassedSweep`. PEARL (`PatternService`) and `HistoryEnergyEstimator` read only that history. Memory becomes a domain entity behind a `MemoryRepository` (Supabase + pgvector / InMemory). `MemoryService` gives each memory a lifecycle: status, pattern key, provenance, decay. Memory also gets a fixed token budget and a cache-stable place in every prompt. An in-process `AnticipationScheduler` runs per-subsystem `Sweep`s and Template-Method `Trigger`s. Each trigger's `ProactiveAction` passes a Chain-of-Responsibility `ConsentPolicy` before it becomes a `Suggestion`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, supabase-py, pgvector (Supabase extension), OR-Tools CP-SAT, LangGraph, pytest + pytest-asyncio. Frontend: Next.js 14, React 18, Vitest (new; pure helpers only).

**Spec:** `docs/superpowers/specs/2026-11-14-capstone-lld-and-anticipation-design.md`, sections §7, §8.1, §8.2, §9, §9.1, §9.2, §10, §11, and §12 phase 3a. This is plan 2 of 4. Plan 1's ledger is `.superpowers/sdd/2026-11-14-capstone-plan-1-safety-and-spine/progress.md`; its Plan-2 follow-ups are folded in below.

**Code base this plan was verified against:**
- Jarvis-Engine `capstone-lld` at `482397e` (includes the `EVENING_CUTOFF_HOUR` evening rule in `app/services/planning/local_time.py`).
- jarvis-frontend `capstone-lld` at `d38a4d9`.

## Global Constraints

**Branch, commits and tests**
- Work on branch `capstone-lld` in both repos. Commit after every task, ending the message with the session's Co-Authored-By trailer. Never `git stash`, never check out another branch, never rewrite history.
- The full suite passes after every task: `.venv/bin/python -m pytest tests/ -q` (baseline 1,122 passed, 4 xfailed, 1 deselected). Tests never touch the network (conftest socket guard).
- Tests use pytest-asyncio and the real CP-SAT solver, with no real LLM or cloud calls in unit tests. Fakes are `FakeSupabase`, the InMemory family and a scripted chain via `set_chain`. Names follow `test_{thing}__{condition}__{expected}`.

**Layering** (`tests/test_layering.py`)
- `app/domain/**` imports only the standard library, pydantic and itself.
- `app/services/**` and `app/orchestrator/**` never import `app.infrastructure`. Services depend on ports and get concretes from `AppContainer`, the only place that picks a repository family.
- `app/infrastructure/**` never imports `app.api` or `app.orchestrator`.

**Data and persistence**
- Every DB read and write filters by `user_id`. Every repository method takes `user_id` first. The approved exceptions are `ProfileRepository.list_user_ids` and `UsageRepository.record`.
- New SQL goes only in `supabase/migrations/`, as timestamped, idempotent, additive files with CHECK constraints on enums and ranges. Every new user table has RLS on and the `owner_all` policy. The live DB may be migrated (Plan 1 decision), but only by the orchestrator, in Task 16.
- Ports are `abc.ABC` with `@abstractmethod`. Entities and request/response models are Pydantic v2 with `Field(description=...)` on every field.

**LLM calls**
- Every LLM call goes through `route_llm_call(..., response_schema=...)`. The exceptions are free-text reply streams, which are already the case today.
- PRIMARY-role calls are never concurrent. Background work (sweeps, triggers) calls models only through `route_llm_call`, inside `attribute_llm_calls(user_id, None)`, so every call is metered to its user.

**Errors**
- Errors use the one envelope `{"error": {"code","message","details","request_id"}}`. Raise `JarvisError` subclasses. No exception text reaches a client, in HTTP bodies or SSE frames.

**Copy**
- Anti-guilt: no blame language for anything not done. Use "set aside", "found a new slot", "didn't happen". Never use "missed" in user-facing copy, "failed", "you didn't", "behind", or any phrase in `tests/behaviour/conftest.py::BLAME_PHRASES`.
- Never describe Jarvis as a calendar or a planner in copy.

**Integrity**
- Never copy code from any leaked Claude Code source. Ideas only (consent chain, typed reasons, sweeps).

**Time**
- Any code that reads "today", an hour of a timestamp, or turns minutes into wall time uses the user's zone: `app/services/planning/local_time.py` (`user_zone`, `local_today`, `logical_day_start`, `end_of_local_day`).
- Plan minute 0 is `DAY_START_HOUR` local, so local hour = `(DAY_START_HOUR + minute // 60) % 24`.

**Claude-specific code**
- Before touching prompt layout or anything Claude-specific, invoke the `claude-api` skill and read `shared/prompt-caching.md`.
- The minimum cacheable prefix for `claude-sonnet-5-5` is 512 tokens. Mid-conversation `system` messages are supported there without a beta.

## Decisions this plan makes where the spec is silent

**Memory storage**
1. **Vector store: pgvector, not Chroma.** Memories are relational rows with a lifecycle. One filtered SQL call applies user, status and type together with similarity. Chroma would mean a second store kept in sync on every forget, a network hop per recall, and the unfiltered-fallback leak class Plan 1 had to remove. The search is an exact scan with no ANN index: per-user rows number tens to hundreds, behind the indexed `user_id`.
2. **Forget keeps a tombstone.** The row keeps its embedding and `pattern_key` but loses its text, so the same fact is recognised and never stored again. Dismiss keeps the text, hidden. Neither ever comes back on its own.

**Learning from history**

3. **The focus profile.** Outcome events are bucketed by the local hour of the slot's `scheduled_start`, over 28 days. Each hour's score is smoothed toward today's heuristic table with pseudo-count 3. The profile counts as "learned" from 10 outcome events.
4. **The energy term steers the solver only with a learned profile.** A hardcoded table steering plans would be faked intelligence. The weight is 40 × difficulty. Tasks due today or overdue get no energy weight, which is how the term stays below TMT by construction.
5. **Skipped and missed (slot passed) both count as "set aside"** for PEARL.
6. **Peak-hour patterns never block time.** The constraint bridge reads only `skip_time_window:*` patterns. Today it would turn any "hour N" pattern into a soft block.

**Conversations and prompts**

7. **Memory extraction is folded into the brain-dump call** (user's words only, so provenance is "you said"). The separate `memory_extraction` call leaves the v2 path: one model call fewer per turn.
8. **All volatile context goes in one `system` note after the history.** That covers memory, "since we last spoke", the plate, documents and the time. Every provider folds it into the final user turn. The system prompts become byte-constant and at least 512 tokens.
9. **Summaries.** They use the FAST role. Compaction runs when more than 16 messages are unsummarized and keeps the newest 8. An idle summary runs after 30 min, by sweep. "Since we last spoke" is deterministic: no model call on the hot path.

**Anticipation**

10. **The morning brief's copy is deterministic** (no model call). The brief fires in [wake_time, wake_time+6 h). It re-slots slot-passed misses only; deadline misses stay set aside until the user asks for them.
11. **Accepting a PatternRule** stores a behavioural constraint carrying a precomputed `blocked` semantic slot, then drafts a re-plan. Planning skips habit translation for precomputed slots.
12. **Consent chain order:** repeat-dismissal → read-only → already-stated rule → autonomy mode. Denied suggestions are not stored; the decision is returned. A new `consent_kind` column stores the typed reason.
13. **Suggestion accept is claim-then-execute** (compare-and-set). An action that is out of date marks the suggestion expired and returns 409.
14. **A process-wide lock wraps every PRIMARY call** in `route_llm_call`. The scheduler makes background PRIMARY calls possible for the first time.
15. **Sweeps belong to their subsystem:** slot-passed in Task 1, memory decay in Task 5, checkpoint retention in Task 8, session summary in Task 9. The scheduler only runs them.

**Pulled forward and supporting decisions**

16. A `ConversationRepository` is pulled forward from Plan 3, with only the read and summary methods it needs.
17. **The seed script drives the live services with a simulated clock.** Drafts are accepted by `DraftService` and outcomes recorded by `TaskService`, so it cannot write a row shape the app doesn't.
18. **`pacing_pushed` is retired:** existing rows become `skipped` and the value leaves the CHECK.
19. **`GET /you` runs PEARL's idempotent observe first**, so every pattern shown has a memory id the user can confirm or dismiss.
20. **Frontend:** Vitest for pure helpers, and an inline SVG focus chart with no chart library.

## Review Focus

1. **A brand-new user (no history, no memories).** Recall is empty and never shows another user's rows. The focus profile is the prior, labelled as such, and does not steer the solver. The morning brief has nothing to say, so no suggestion is created and no model is called. Pinned by: Task 4 `test_memories__nearest__user_without_memories__empty_never_another_users`, Task 3 `test_history_estimator__no_events__prior_and_source_prior`, Task 11 `test_brief__nothing_to_say__no_suggestion_and_zero_llm_calls`.
2. **Double-click or race on Complete/Skip, or the slot sweep racing a late completion.** Exactly one outcome event is written. A task completed inside the 30-minute grace is never marked missed. Pinned by: Task 1 `test_complete__double_click__second_returns_none_and_one_event`, `test_sweep__completed_inside_grace__never_marked_missed`.
3. **The user forgets a fact, then says it again in a later conversation.** It is not stored, not recalled and not shown, and no learning frame is emitted. Pinned by: Task 5 `test_remember__after_forget__suppressed_and_nothing_written`, Task 7 `test_turn__forgotten_fact_said_again__no_frame_no_row`.
4. **The user accepts a stale suggestion, or double-clicks Accept.** "Stale" means the brief's draft was superseded by a plan accepted in chat. The result is a 409 with a plain message, the suggestion is marked expired, and tasks are unchanged; the action runs once. Pinned by: Task 10 `test_accept__double_click__second_409_and_action_executed_once`, Task 11 `test_brief__accept_after_newer_plan_accepted__409_and_expired_tasks_unchanged`.
5. **An IST user just after local midnight** (00:00–05:30 IST is still the previous UTC day). The brief's once-a-day rule, suggestion idempotency, the streak and the slot sweep all use the local day or real instants, never the UTC date. Pinned by: Task 10 `test_suggestions__exists_today__ist_half_past_midnight_is_new_local_day`, Task 11 `test_brief__utc_midnight_crossing__fires_once_per_local_day`, Task 12 `test_streak__completion_at_00_30_ist__counts_for_that_local_day`, Task 1 `test_sweep__slot_just_after_local_midnight__uses_instants`.

---

## Task map, parallelism and shared files

| # | Task | Subsystem | Depends on | Can run in parallel with |
|---|---|---|---|---|
| 1 | TaskService: outcome events, slot sweep | tasks | — | 3, 4, 14 |
| 2 | Accept writes scheduled/rescheduled; re-slots missed | drafts | 1 | 5, 10, 13 |
| 3 | Learned focus profile + solver energy term | scheduling | — | 1, 4, 14 |
| 4 | Memory entity, MemoryRepository, pgvector | memory/persistence | — | 1, 3, 14 |
| 5 | MemoryService + memories API | memory | 1, 4 | 2, 10 |
| 6 | PatternService (PEARL on events) | learning | 5 | 13 |
| 7 | Learning in the turn | chat/learning | 6 | 11, 12 |
| 8 | Budgeted recall + cache-stable prompts + checkpoint hygiene | chat/memory | 7 | 11, 12 |
| 9 | Session summaries, compaction, "since we last spoke" | chat/memory | 8 | 11, 12 |
| 10 | Anticipation framework + API | anticipation | 1 | 2, 5 |
| 11 | MorningBrief, PatternRule, replanner | anticipation/planning | 2, 3, 6, 10 | 7, 8, 9 |
| 12 | You API + local-day fixes | user model | 3, 5, 6 | 7–9, 11 |
| 13 | Seed demo user | scripts | 1, 2, 5 | 6–12 |
| 14 | FE: learning moments + Plan-1 follow-ups | frontend | contract in 7 | all backend |
| 15 | FE: You page + While you were away | frontend | 14; contracts in 10, 12 | backend 7–13 |
| 16 | Live E2E + live migrations | ops | all | — |

**Shared files.** Merge these sequentially; edits are additive and small:
- `app/container.py`: Tasks 1–6 and 8–12.
- `app/core/config.py`: Tasks 1, 3, 5, 8–11.
- `app/domain/{entities,ports}.py`: Tasks 1, 3, 4, 6, 9, 10, 11.
- `app/infrastructure/persistence/*` and `tests/test_repository_contracts.py`: Tasks 2, 4, 9, 10, 11.
- `tests/fakes.py`: Tasks 2, 4.
- `app/modules/planning_graph.py`: Tasks 1, 3, 6, 11.
- `app/api/v1/router.py`: Tasks 10, 12.
- Frontend `lib/api.ts`, `lib/types.ts` and the dashboard page: Tasks 14 then 15.

**Must run one after another.** Tasks 7 → 8 → 9 all edit `app/api/v1/endpoints/chat.py::chat_stream_v2`.

**Migration timestamps, in order:**
- `20261115000000` (Task 1)
- `…0001` (Task 2)
- `…0002` (Task 4)
- `…0003` (Task 9)
- `…0004` (Task 10)

---

### Task 1: TaskService — every outcome is an event; "missed" is inferred

**Files:**
- Create: `app/services/task_service.py`, `supabase/migrations/20261115000000_skip_writes_skipped.sql`
- Modify:
  - `app/domain/ports.py` (add `Sweep`)
  - `app/domain/events.py` (add `DeadlineMissed`)
  - `app/api/v1/endpoints/tasks.py`:
    - `complete_task` and `skip_task` delegate to the service.
    - The `TaskUpdateRequest.status` Literal drops `pacing_pushed`.
    - The fire-and-forget `detect_patterns` calls are removed; Task 6's observer replaces them.
  - `app/services/analytical/missed_deadlines.py`: `detect_and_mark_missed` gains `task_service`.
  - `app/modules/planning_graph.py::mark_missed`: passes `registered_container().task_service` when one is registered.
  - `app/modules/coach.py:96`: counts `skipped`.
  - `app/container.py`: `task_service`, `sweeps`.
  - `app/core/config.py`: `SLOT_GRACE_MINUTES = 30`, `SLOT_SWEEP_LOOKBACK_DAYS = 7`.
  - `docs/capstone/_schema_notes.md`.
- Test: `tests/test_task_service.py`, `tests/test_task_lifecycle.py` (update the skip and PATCH tests), `tests/test_migrations.py`

**Interfaces:**
- Consumes: `TaskRepository.update_status/get/list_scheduled_between`, `TaskEventRepository`, `EventBus`, `TaskCompleted/TaskSkipped/TaskMissed` (Plan 1).
- Produces:
```python
# app/domain/ports.py
class Sweep(ABC):
    name: ClassVar[str]
    interval: ClassVar[timedelta]
    @abstractmethod
    async def run(self, user_ids: list[str], now: datetime) -> int: ...   # items changed; one user's failure is logged, never raised
# app/domain/events.py
class DeadlineMissed(DomainEvent): task_id: str
# app/services/task_service.py
class TaskService:
    def __init__(self, tasks: TaskRepository, task_events: TaskEventRepository, bus: EventBus,
                 *, clock: Callable[[], datetime] = utc_now) -> None
    async def complete(self, user_id: str, task_id: str, *, actual_duration_minutes: int | None = None,
                       quality: int | None = None) -> TaskEvent | None
    async def skip(self, user_id: str, task_id: str, *, reason: str | None = None) -> TaskEvent | None
    async def mark_deadline_missed(self, user_id: str, task_id: str) -> TaskEvent | None
    async def sweep_passed_slots(self, user_id: str, now: datetime) -> list[TaskEvent]
class SlotPassedSweep(Sweep):   # name = "slot_passed", interval = timedelta(minutes=1)
    def __init__(self, tasks: TaskService) -> None
# AppContainer gains: task_service: TaskService; sweeps: list[Sweep]  (starts as [SlotPassedSweep(task_service)])
# detect_and_mark_missed(user_id, supabase, zone=None, *, task_service: TaskService | None = None)
```

**Rules:**
- **Return value.** `None` means "already in that state, nothing written".
- **Event metadata.** This is the live shape the seed copies. Every event carries `{"title", "goal_id", "deadline_hint"}`; completed events add `actual_duration_minutes` and `quality`; skipped events add `reason`. Each event carries the task's `scheduled_start` and `scheduled_end` at that moment.
- **Legal moves:**

  | Outcome | Allowed from |
  |---|---|
  | Complete | pending, missed, skipped ("I actually did that") |
  | Skip | pending |
  | Deadline-missed | pending |
  | Slot-missed | pending |

  Each move is a compare-and-set, `update_status(..., expected=current)`. On `StaleVersionError` the service re-reads; if the task is already at the target it returns `None`.
- **Status for deadline misses.** A deadline-missed task gets status `missed` and event `deadline_missed` (spec §9.1).
- **Publishing.** The domain event is published after the row is stored.
- **Sweep candidates.** These are the tasks returned by `list_scheduled_between(user_id, now - 7 d, now)`. A task is marked missed only if all of these hold:
  - its status is `pending`;
  - `scheduled_end + SLOT_GRACE_MINUTES <= now`;
  - no completed, skipped or missed event for it has `occurred_at >= scheduled_start`.
- **Skip endpoint.**
  - It returns `status="skipped"` with today's buffer-advice message.
  - It no longer writes `reschedule_count` or the re-decomposition memory. Task 6's `RepeatedSkipDetector` and Task 12's event count replace them.
  - An unknown task returns a 404 envelope.
- **Migration.** Run `update user_tasks set status='skipped' where status='pacing_pushed'`, then replace the status CHECK with `('pending','completed','skipped','missed')`. `_mapping._LEGACY_TASK_STATUSES` stays for old reads.

- [ ] **Step 1: Write failing tests** in `tests/test_task_service.py` (InMemory family, fixed clock):
```python
async def test_complete__pending_task__status_completed_and_one_completed_event_with_slot_and_metadata(): ...
    # event.event_type == COMPLETED; event.scheduled_start == task.scheduled_start
    # set(event.metadata) >= {"title", "goal_id", "deadline_hint", "actual_duration_minutes", "quality"}
async def test_complete__double_click__second_returns_none_and_one_event(): ...     # RF2
async def test_complete__missed_task__allowed_and_status_completed(): ...
async def test_skip__pending__skipped_event_with_reason_and_task_skipped_published(): ...
async def test_skip__completed_task__returns_none_no_event(): ...
async def test_sweep__slot_ended_31_min_ago__missed_event_and_status(): ...
async def test_sweep__slot_ended_29_min_ago__untouched(): ...
async def test_sweep__completed_inside_grace__never_marked_missed(): ...             # RF2
async def test_sweep__race_with_completion__stale_version_swallowed_one_event(): ...
async def test_sweep__twice__idempotent(): ...
async def test_sweep__slot_just_after_local_midnight__uses_instants(): ...          # RF5: 00:15-00:40 IST slot, now 01:15 IST
async def test_slot_passed_sweep__one_users_failure__others_still_swept(): ...
async def test_mark_deadline_missed__pending__deadline_missed_event_and_status_missed(): ...
async def test_detect_and_mark_missed__with_task_service__writes_deadline_missed_events(): ...
```
  In `tests/test_task_lifecycle.py`:
  - `test_task_skip__marks_skipped` asserts `status == "skipped"` and a `task_events` row.
  - `test_patch_task__pacing_pushed__422` replaces the `pacing_pushed` parametrisation.
  - `test_complete_endpoint__unknown_task__404_envelope`.

  In `tests/test_migrations.py`: `test_migrations__skip_migration__maps_pacing_pushed_and_drops_it_from_check`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_task_service.py tests/test_task_lifecycle.py tests/test_migrations.py -q`. Expected: FAIL, because `app.services.task_service` is missing.
- [ ] **Step 3: Implement** the service, sweep, endpoint delegation, migration and container wiring as specified above.
- [ ] **Step 4: Run** the new tests, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(tasks): every outcome is an event; missed is inferred by a slot sweep`.

### Task 2: Accepting a plan records `scheduled` / `rescheduled`; accept re-slots missed tasks

**Files:**
- Create: `supabase/migrations/20261115000001_accept_draft_reslots_missed.sql`
- Modify:
  - `app/services/draft_service.py`
  - `app/infrastructure/persistence/in_memory.py`: `InMemoryDraftRepository.accept_atomic`.
  - `tests/fakes.py`: `FakeSupabase._accept_draft`.
  - `app/domain/ports.py`: the `DraftRepository.accept_atomic` docstring.
  - `app/container.py`.
  - `app/services/draft_actions.py::_draft_service`: the legacy shim passes `SupabaseTaskEventRepository(supabase)`.
- Test: `tests/test_draft_service.py`, `tests/test_repository_contracts.py`, `tests/test_migrations.py`

**Interfaces:**
- Consumes: `TaskEventRepository` (Plan 1), the event metadata shape from Task 1.
- Produces:
```python
class DraftService:
    def __init__(self, drafts: DraftRepository, events: EventBus, *,
                 task_events: TaskEventRepository | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None
```

**Rules:**
- After `accept_atomic` commits, each written row with a `scheduled_start` is compared with that task's latest `SCHEDULED` or `RESCHEDULED` event:

  | Latest known slot | Event written |
  |---|---|
  | none | `SCHEDULED` |
  | a different start | `RESCHEDULED`, metadata adds `previous_start` (ISO) |
  | the same start | nothing |

- An append failure is logged with `logger.warning` and never fails or undoes the accept. With `task_events=None`, behaviour is today's.
- **Delete rule.** The SQL function, the fake rpc and the InMemory `accept_atomic` must stay identical. The delete becomes "the user's pending tasks, plus their `missed` tasks whose `task_id` appears in `p_rows`".
- **Migration body.** Copy the whole `create or replace function public.accept_draft` from `20261114000001_capstone_lld.sql` and change only the `delete` line. Repeat the `revoke` and `grant` lines and the `comment on function`.

- [ ] **Step 1: Write failing tests:**
  - `test_accept__new_tasks__one_scheduled_event_each_with_slot`
  - `test_accept__replan_moves_a_task__rescheduled_event_with_previous_start`
  - `test_accept__replan_same_slot__no_new_event`
  - `test_accept__event_store_down__accept_still_succeeds_and_logs` (caplog contains `"task event"`)

  Contract tests, both families:
  - `test_drafts__accept_atomic__reslots_missed_row_with_same_task_id` (no `ConflictError`; the row is now pending with the new times)
  - `test_drafts__accept_atomic__unrelated_missed_row_kept`

  Migration test: `test_migrations__accept_draft_v2__deletes_reslotted_missed_rows`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the service change, the migration, and the matching fake and InMemory delete rules.
- [ ] **Step 4: Run** the new tests and the full suite → PASS.
- [ ] **Step 5: Commit** `feat(drafts): accepting a plan records scheduled and rescheduled events; missed tasks can be re-slotted`.

### Task 3: The learned focus profile and the solver's energy term

**Files:**
- Create: `app/services/energy.py`
- Modify:
  - `app/domain/entities.py`: `HourFocus`, `FocusProfile`.
  - `app/domain/ports.py`: `EnergyEstimator`.
  - `app/core/or_tools/constraints.py`: `energy_objective_terms`.
  - `app/core/or_tools/solver.py`: `add_task(..., energy_weight=0)`, `set_focus_penalties`, and `solve` adds the terms.
  - `app/api/v1/endpoints/schedule.py::run_schedule`: the `focus_by_hour` kwarg.
  - `app/modules/planning_graph.py`:
    - `PlanningState.focus_by_hour: Optional[list]`.
    - `fetch_constraints` sets it once per turn.
    - `solve_schedule` passes it to `run_schedule`.
  - `app/core/user_model.py::get_estimated_energy`: delegates to `HeuristicEnergyEstimator`, same values.
  - `app/core/config.py`:
    - `ENERGY_OBJECTIVE_ENABLED = os.getenv("JARVIS_ENERGY_OBJECTIVE", "true").lower() != "false"`
    - `ENERGY_OBJECTIVE_WEIGHT = 40`
    - `FOCUS_WINDOW_DAYS = 28`
    - `FOCUS_PRIOR_STRENGTH = 3.0`
    - `MIN_FOCUS_OBSERVATIONS = 10`
  - `app/container.py`: `energy`.
- Test: `tests/test_energy.py`, `tests/test_scheduler.py`, `tests/test_planning_graph.py`, `tests/behaviour/test_learning.py`

**Interfaces:**
- Consumes: `TaskEventRepository.list_for_user`, `user_zone`.
- Produces:
```python
class HourFocus(BaseModel): hour: int (0..23); score: float (0..1); done: int (ge=0); not_done: int (ge=0)
class FocusProfile(BaseModel):
    source: Literal["prior", "learned"]; observations: int (ge=0); hours: list[HourFocus]   # exactly 24, index == hour
    def scores(self) -> list[float]
class EnergyEstimator(ABC):
    @abstractmethod
    async def estimate(self, user_id: str, zone: ZoneInfo, *, now: datetime) -> FocusProfile: ...
# app/services/energy.py
class HeuristicEnergyEstimator(EnergyEstimator):
    def score(self, hour: int) -> float          # today's UserModel.get_estimated_energy table, verbatim
class HistoryEnergyEstimator(EnergyEstimator):
    def __init__(self, task_events: TaskEventRepository, prior: HeuristicEnergyEstimator, *,
                 window_days: int = FOCUS_WINDOW_DAYS, prior_strength: float = FOCUS_PRIOR_STRENGTH,
                 min_observations: int = MIN_FOCUS_OBSERVATIONS) -> None
# app/core/or_tools/constraints.py
def energy_objective_terms(model: cp_model.CpModel, tasks: dict[str, dict],
                           penalty_by_hour: list[int], day_start_hour: int) -> list[cp_model.LinearExpr]
# JarvisScheduler
def set_focus_penalties(self, penalty_by_hour: list[int], day_start_hour: int) -> None
def add_task(self, ..., energy_weight: int = 0) -> None
# run_schedule(..., focus_by_hour: list[float] | None = None)
```

**Algorithm (decisions 3 and 4):**
- **Outcome events.** Use completed, skipped and missed events since `now - window_days`. Bucket each by the local hour of its `scheduled_start`; events without one are ignored.
- **Score.** For each hour, `score = (done + k·prior(h)) / (done + not_done + k)`, with `k = prior_strength`.
- **Source.** `source = "learned"` iff the total number of outcome events is at least `min_observations`.
- **Penalty table.** `penalty_by_hour[h] = round(10 × (1 − score[h]))`.
- **Task weight.** `energy_weight = round(ENERGY_OBJECTIVE_WEIGHT × difficulty_weight)` when the task's TMT `delay_h >= 24`, else 0.
- **CP-SAT terms.** Per weighted task:
  1. `h = start // 60` (`add_division_equality`);
  2. `local = (h + day_start_hour) % 24` (`add_modulo_equality`);
  3. `pen = table[local]` (`add_element`);
  4. add the term `energy_weight * pen` to the objective.
- **Planning.** `fetch_constraints` sets `focus_by_hour = profile.scores()` only when `ENERGY_OBJECTIVE_ENABLED` and `profile.source == "learned"`; otherwise it sets `None`. It reads `registered_container().energy`; with no container it sets `None`.

- [ ] **Step 1: Write failing tests:**

  Estimator tests, in `tests/test_energy.py`:
  - `test_history_estimator__no_events__prior_and_source_prior` (RF1): scores equal `HeuristicEnergyEstimator().score(h)` for all 24 hours, and `observations == 0`.
  - `test_history_estimator__evening_done_morning_skipped__learned_and_evening_higher`: `profile.hours[19].score > profile.hours[9].score`, and `source == "learned"`.
  - `test_history_estimator__buckets_by_local_hour_of_slot`: a `03:30Z` slot is IST 09:00 and lands in hour 9.
  - `test_history_estimator__other_users_events__ignored`.
  - `test_history_estimator__sparse_hour__pulled_toward_prior`: 1 done at hour 14 gives `prior(14) < score < 1.0`.
  - `test_heuristic__matches_user_model_table_for_every_hour`.

  Solver tests, in `tests/test_scheduler.py`, using real CP-SAT:
  - `test_energy_term__two_free_slots__hard_task_takes_high_focus_slot`:
    - Hard blocks leave only 09:00–09:30 and 19:00–19:30 free.
    - Task A has difficulty 0.9 and task B 0.1; both are due in 3 days.
    - The profile peaks at hour 19.
    - Expect A at minute 660 and B at minute 60.
  - `test_energy_term__flag_off__identical_to_no_profile`.
  - `test_energy_term__task_due_today__tmt_order_kept`.
  - `test_energy_term__hard_blocks_never_violated`: 5 profiles × a fixed block set; no task interval intersects a block.
  - `test_energy_term__minimal_work_window_at_peak__hard_task_still_kept_out`.

  Planning tests:
  - `test_fetch_constraints__learned_profile__focus_on_state`
  - `test_fetch_constraints__prior_profile__no_focus`

  Behaviour test:
  - `test_energy__learned_evening_profile__teaching_a_rule_still_moves_the_schedule`: seed 12 evening completed events, teach "no work before 11 AM", plan, and expect no slot before 11:00 local.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** run the new tests and the full suite → PASS. The existing scheduler and behaviour tests stay unchanged: the term is off without a learned profile.
- [ ] **Step 5: Commit** `feat(scheduling): a learned focus profile leans hard tasks toward the user's best hours`.

### Task 4: Memory as a domain entity, stored with pgvector

**Files:**
- Create: `app/infrastructure/embedding.py`, `supabase/migrations/20261115000002_memory_lifecycle_and_vectors.sql`
- Modify:
  - `app/domain/entities.py`
  - `app/domain/ports.py`
  - `app/infrastructure/persistence/{in_memory,supabase_repos,factory,_mapping}.py`
  - `tests/fakes.py`:
    - `rpc("match_memories")`.
    - `BagOfWordsEmbedder(TextEmbedder)`, moved from `tests/behaviour/conftest.py::bag_of_words_embedding`, which then imports it.
  - `docs/capstone/_schema_notes.md`
- Test: `tests/test_repository_contracts.py`, `tests/test_migrations.py`, `tests/test_domain.py`

**Interfaces — Produces:**
```python
# app/domain/entities.py
class MemoryStatus(str, Enum): ACTIVE="active"; DISMISSED="dismissed"; FORGOTTEN="forgotten"; FADED="faded"
class MemorySource(str, Enum): SAID="said"; INFERRED="inferred"
MemoryType = Literal["fact","preference","behavioral_pattern","temporal_event","goal","feedback","constraint"]
SUPPRESSING_STATUSES: frozenset[MemoryStatus] = frozenset({MemoryStatus.DISMISSED, MemoryStatus.FORGOTTEN})
BASE_HALFLIFE_HOURS = 168
class Memory(BaseModel):
    id: str | None = None; user_id: str; memory_type: MemoryType; content: str  # max_length=2000; "" only when forgotten
    status: MemoryStatus = MemoryStatus.ACTIVE; provenance: MemorySource; origin: str = "conversation"
    pattern_key: str | None = None
    confidence: float = 0.5   # 0..1
    strength: float = 1.0     # 0..1
    stability: float = 1.0    # 0.01..20
    strength_as_of: datetime; observation_count: int = 1   # ge=1
    source_session_id: str | None = None; source_message_id: str | None = None
    created_at: datetime; last_confirmed_at: datetime | None = None
    superseded_by: str | None = None; expires_at: datetime | None = None; applied_as: str | None = None
    def strength_at(self, now: datetime) -> float   # strength * exp(-hours(now - strength_as_of) / (stability * 168))
# app/domain/ports.py
class MemoryMatch(BaseModel): memory: Memory; similarity: float
class MemoryRepository(ABC):
    async def add(self, user_id: str, m: Memory, *, embedding: list[float] | None = None) -> Memory   # ConflictError on a duplicate (user_id, pattern_key)
    async def get(self, user_id: str, memory_id: str) -> Memory | None
    async def list(self, user_id: str, *, statuses: frozenset[MemoryStatus] = frozenset({MemoryStatus.ACTIVE}),
                   types: frozenset[str] | None = None) -> list[Memory]          # superseded rows excluded
    async def find_by_pattern_key(self, user_id: str, pattern_key: str) -> Memory | None   # any status
    async def nearest(self, user_id: str, embedding: list[float], *, statuses: frozenset[MemoryStatus],
                      types: frozenset[str] | None = None, limit: int = 5,
                      min_similarity: float = 0.0) -> list[MemoryMatch]          # most similar first
    async def update(self, user_id: str, memory_id: str, **fields: Any) -> Memory   # NotFoundError; "embedding" allowed
class TextEmbedder(ABC):
    dimensions: int
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...   # sync CPU work; callers use asyncio.to_thread
# app/infrastructure/embedding.py
class MiniLMEmbedder(TextEmbedder): dimensions = 384   # wraps app.utils.embedding.embed_texts; InfrastructureError if unavailable
# Repositories gains `memories: MemoryRepository`; RepositoryFactory.supabase/in_memory build it
```

**Column mapping.**
- Entity `provenance` maps to a new column `provenance`.
- Entity `origin` maps to the legacy column `source` (conversation, behavior, …), which stays untouched for the v1 path.
- Reads use an explicit column list that never includes `embedding`. This fixes today's `select("*")` with embeddings, run three times per turn.

**Migration** (idempotent, additive):
1. `create extension if not exists vector with schema extensions;`
2. Add the new `user_memories` columns, each with its CHECK:
   - `status text not null default 'active'`
   - `provenance text not null default 'said'`
   - `pattern_key text`
   - `source_session_id uuid references chat_sessions(id) on delete set null`
   - `source_message_id uuid references chat_messages(id) on delete set null`
   - `last_confirmed_at timestamptz`
   - `strength_as_of timestamptz not null default now()`
3. Backfill provenance: `update … set provenance='inferred' where source='behavior'`.
4. Add CHECKs `observation_count >= 1` and `stability between 0.01 and 20`.
5. `embedding` becomes `extensions.vector(384)`, using a cast that NULLs arrays whose length is not 384.
6. Indexes:
   - a unique partial index `(user_id, pattern_key) where pattern_key is not null`;
   - an index on `(user_id, status)`.
7. `match_memories(p_user_id text, p_query extensions.vector(384), p_statuses text[], p_types text[], p_limit int, p_min_similarity double precision)`:
   - It returns every memory column except `embedding`, plus `similarity double precision`.
   - It is `language sql stable` with `set search_path = public, extensions`.
   - It filters on user, status, type (`p_types` null means any), `embedding is not null` and `superseded_by is null`.
   - `similarity = 1 - (embedding <=> p_query)`, kept only when `>= p_min_similarity`; ordered by distance; limited to `p_limit`.
8. `revoke execute … from public, anon; grant … to service_role, authenticated`.

**Apply note for the orchestrator.** Apply this migration live only together with Task 7. Until Task 7 lands, the legacy extractor (`MemoryStore.find_similar_memory`) still reads `embedding` as a float array.

- [ ] **Step 1: Write failing tests.**

  Contract tests (both families via the `repos` fixture):
  - `test_memories__add_get__roundtrip_with_provenance_and_session`
  - `test_memories__list__active_only_by_default`
  - `test_memories__list__types_filter`
  - `test_memories__list__never_returns_embeddings` (Supabase variant: the fake records selected columns; `"embedding"` is never among them)
  - `test_memories__find_by_pattern_key__returns_dismissed_row`
  - `test_memories__duplicate_pattern_key__raises_conflict`
  - `test_memories__nearest__most_similar_first_status_and_type_filtered`
  - `test_memories__nearest__user_without_memories__empty_never_another_users` (RF1)
  - `test_memories__update__other_user__raises_not_found`

  Domain tests:
  - `test_memory__strength_at__decays_by_e_each_stability_week`: at `stability=1`, 168 h → `≈ e^-1`.
  - `test_memory__confidence_out_of_range__rejected`.

  Migration tests:
  - `test_migrations__memory_migration__enables_vector_and_defines_match_memories`
  - `test_migrations__match_memories__filters_user_and_status`
  - `test_migrations__memory_migration__idempotent_statements`
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** the entities, ports, both adapters, the factory, the fake rpc (cosine over stored lists, same filters) and the migration. **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `feat(memory): memory is a domain entity with a lifecycle, stored with pgvector`.

### Task 5: MemoryService — forget and dismiss stick, provenance, the memories API

**Files:**
- Create: `app/services/memory/memory_service.py`
- Modify:
  - `app/api/v1/endpoints/memories.py`: every route on `MemoryService`, `user: CurrentUser`, `response_model`.
  - `app/api/v1/endpoints/drafts.py`: the reject reason goes to `memory_service.remember(... memory_type="feedback", origin="draft_rejection")`, awaited, with errors logged.
  - `app/container.py`:
    - `memory_service`.
    - `AppContainer.build(..., embedder: TextEmbedder | None = None, install_chain: bool = True)`, where `None` means `MiniLMEmbedder()`.
    - Append `MemoryDecaySweep`.
  - `app/main.py`: warm the embedder with `asyncio.create_task(asyncio.to_thread(embedder.embed, ["warm up"]))`; failures are logged.
  - `app/core/config.py`:
    - `MEMORY_SUPPRESS_SIMILARITY = 0.85`
    - `MEMORY_DEDUPE_SIMILARITY = 0.85`
    - `MEMORY_RETENTION_FLOOR = 0.1`
  - `tests/behaviour/conftest.py`:
    - The `jarvis` fixture builds `AppContainer.build(supabase_client=supabase, cfg=config, hooks=get_hooks(), embedder=BagOfWordsEmbedder(), install_chain=False)`.
    - It calls `set_container(...)` and sets `state.container`.
    - The `_memory_api` helpers set `app.state.container`.
- Test: `tests/test_memory_service.py`, `tests/test_memories_api.py`, `tests/behaviour/test_long_term_memory.py`

**Interfaces:**
- Consumes: `MemoryRepository`, `TextEmbedder`, `Memory*` (Task 4); `Sweep` (Task 1).
- Produces:
```python
class MemoryCandidate(BaseModel):
    memory_type: MemoryType; content: str  # 1..2000 chars
    confidence: float = 0.5; contradicts: str | None = None; expires_at: datetime | None = None
class RememberOutcome(BaseModel):
    action: Literal["created", "reinforced", "superseded", "suppressed"]; memory: Memory | None
class MemoryService:
    def __init__(self, memories: MemoryRepository, embedder: TextEmbedder, *, clock: Callable[[], datetime] = utc_now) -> None
    async def remember(self, user_id: str, candidate: MemoryCandidate, *, provenance: MemorySource = MemorySource.SAID,
                       origin: str = "conversation", source_session_id: str | None = None,
                       source_message_id: str | None = None) -> RememberOutcome
    async def confirm(self, user_id: str, memory_id: str) -> Memory
    async def dismiss(self, user_id: str, memory_id: str) -> Memory
    async def forget(self, user_id: str, memory_id: str) -> None
    async def edit(self, user_id: str, memory_id: str, *, content: str | None = None, memory_type: MemoryType | None = None) -> Memory
    async def list_active(self, user_id: str, *, types: frozenset[str] | None = None, now: datetime | None = None) -> list[Memory]
    async def fade_stale(self, user_id: str, now: datetime) -> int
class MemoryDecaySweep(Sweep):   # name = "memory_decay", interval = timedelta(hours=24)
    def __init__(self, memory: MemoryService) -> None
```

**`remember`, in order:**
1. Embed off the loop.
2. **Suppressed:** `nearest(statuses=SUPPRESSING_STATUSES, types={type}, limit=1, min_similarity=MEMORY_SUPPRESS_SIMILARITY)` finds a row, and nothing is written.
3. **Superseded:** `contradicts` names this user's active memory, so the new memory is added and the old row gets `superseded_by`.
4. **Reinforced:** `nearest(statuses={ACTIVE, FADED}, types={type}, limit=1, min_similarity=MEMORY_DEDUPE_SIMILARITY)` finds a row. That row gets:
   - status active, `strength=1`, `strength_as_of=now`;
   - `stability +1` (cap 20), `observation_count +1`, `confidence +0.1` (cap 1);
   - `last_confirmed_at=now` when its provenance is `said` (saying it again confirms it).
5. Otherwise the memory is **created**.

**The other operations:**
- **`confirm`** reinforces the memory, sets `confidence = max(confidence, 0.9)` and sets `last_confirmed_at = now`.
- **`dismiss`** sets status dismissed.
- **`forget`** sets status forgotten and content to "". The embedding and `pattern_key` stay so the fact is recognised; the API description says so.
- **`list_active`** returns rows that are active, unsuperseded and unexpired, with `strength_at(now) >= MEMORY_RETENTION_FLOOR`.
- **`fade_stale`** handles the user's active memories:
  - It persists `strength = strength_at(now)` and `strength_as_of = now`, so there is no double decay.
  - Rows below the floor whose type is not `constraint` become `faded`.
  - A re-mention reactivates a faded row; dismissed and forgotten rows never come back.
- **API** (envelope errors; another user's id → 404):

  | Route | Response model |
  |---|---|
  | `GET /memories/` | `MemoryListResponse{memories: list[MemoryOut], count}` |
  | `PATCH /memories/{id}` | `MemoryOut` |
  | `DELETE /memories/{id}` | `ForgetResponse{status: Literal["forgotten"]}` |
  | `POST /memories/{id}/confirm` | `MemoryOut` |
  | `POST /memories/{id}/dismiss` | `MemoryOut` |

  `MemoryOut` fields: `memory_id, memory_type, content, confidence, provenance, freshness (strength_at now), noted_at (created_at), last_confirmed_at, observation_count, pattern_key`.

- [ ] **Step 1: Write failing tests** in `tests/test_memory_service.py` (InMemory family, `BagOfWordsEmbedder`, fixed clock):
  - `test_remember__new_fact__created_with_said_provenance_and_source_ids`
  - `test_remember__same_fact_again__reinforced_not_duplicated_and_confirmed`
  - `test_remember__contradiction__old_superseded_new_active`
  - `test_remember__after_forget__suppressed_and_nothing_written` (RF3)
  - `test_remember__after_dismiss__suppressed`
  - `test_remember__faded_fact_mentioned__reactivated`
  - `test_forget__content_blanked_and_hidden_from_list_active`
  - `test_confirm__raises_confidence_and_sets_last_confirmed_at`
  - `test_list_active__expired_temporal_event__excluded`
  - `test_fade_stale__month_unused__faded_and_constraint_kept`
  - `test_fade_stale__persists_strength_without_double_decay`: `strength_at(t)` is equal before and after the fade.
  - `test_memory_decay_sweep__one_users_failure__others_faded`

  In `tests/test_memories_api.py`:
  - `test_memories_api__forget_then_list__gone`
  - `test_memories_api__other_users_memory__404`
  - `test_memories_api__store_down__503_envelope`
  - `test_memories_api__dismiss__status_dismissed`

  In the behaviour suite, `test_memory__dismiss_lowers_and_confirm_raises_confidence` becomes `test_memory__dismiss_hides_and_confirm_raises_confidence`. Its seeding goes through `memory_service.remember`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `feat(memory): forget and dismiss stick; memories carry provenance; decay is persisted`.

### Task 6: PEARL reads events — PatternService, detectors as strategies, sticky dismissals

**Files:**
- Create: `app/services/memory/patterns.py`
- Modify:
  - `app/domain/entities.py`: `Pattern` gains `pattern_key: str | None = None` and `hour: int | None = None`.
  - `app/services/memory/constraint_bridge.py`: add `memory_slots`; the legacy `memories_to_constraints` stays for v1.
  - `app/modules/planning_graph.py::memory_to_constraints`: uses `memory_service.list_active(types={"constraint","behavioral_pattern"})` and `memory_slots`, with the legacy path when no container is registered.
  - `app/core/observation.py`: `detect_pearl_patterns` calls `patterns.observe`.
  - `app/container.py`: `patterns`; `PatternObserver` subscribed to `TaskCompleted`, `TaskSkipped` and `TaskMissed`.
  - `tests/behaviour/test_learning.py`:
    - `_seed_history_at_nine` writes `task_events` rows in Task 1's shape.
    - Delete the strict `xfail` on `test_bridge__dismissed_pattern__hands_the_hour_back`.
    - Update the module docstring.
- Test: `tests/test_patterns.py`, `tests/test_memory_constraint_bridge.py`

**Interfaces:**
- Consumes: `TaskEventRepository`, `MemoryRepository`, `ProfileRepository`, `EventBus`, `PatternDetected` (Plan 1), and Task 1's event metadata.
- Produces:
```python
class PatternDetector(ABC):
    name: ClassVar[str]
    @abstractmethod
    def detect(self, events: list[TaskEvent], zone: ZoneInfo) -> list[Pattern]: ...
class SkipTimeWindowDetector(PatternDetector); class PeakHourDetector(PatternDetector)
class DeadlineCrunchDetector(PatternDetector); class RepeatedSkipDetector(PatternDetector)
class PatternChange(BaseModel): pattern: Pattern; memory_id: str; action: Literal["created", "strengthened"]
class PatternView(BaseModel): pattern: Pattern; memory_id: str | None; confirmed: bool
class PatternService:
    def __init__(self, task_events: TaskEventRepository, memories: MemoryRepository, profiles: ProfileRepository,
                 bus: EventBus, *, detectors: list[PatternDetector] | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None
    async def observe(self, user_id: str) -> list[PatternChange]
    async def current(self, user_id: str) -> list[PatternView]          # live detection joined with memories; suppressed excluded
    async def insights(self, user_id: str) -> list[dict]               # {"insight","confidence","occurrences","memory_id"}
class PatternObserver(EventListener):
    def __init__(self, patterns: PatternService) -> None
def memory_slots(memories: list[Memory]) -> list[TimeSlot]
```

**Detector rules.** An outcome is a completed, skipped or missed event. Outcomes are bucketed by the local hour of `scheduled_start`, over the last `FOCUS_WINDOW_DAYS`. Confidence is the rate.

| detector | fires when | description (the bridge and insights read it) | key |
|---|---|---|---|
| skip_time_window | hour has ≥ 3 outcomes and (skipped+missed)/total ≥ 0.7 | `User avoids tasks during hour {h} ({n} of {total} set aside)` | `skip_time_window:{h}` |
| peak_hour | the single best hour with ≥ 3 outcomes and done/total ≥ 0.8 | `User does their best work around hour {h} ({d} of {total} done)` | `peak_hour:{h}` |
| deadline_crunch | ≥ 3 completed events with a `deadline_hint`, ≥ 70% within 24 h before it (date-only deadlines end at local 23:59:59, `missed_deadlines._parse_deadline`) | `User tends to finish deadlined work in the last day ({k} of {n})` | `deadline_crunch` |
| repeated_skip | ≥ 3 skipped events for one task_id | `'{title}' has been set aside {n} times; smaller steps may help` | `repeated_skip:{task_id}` |

`evidence` holds up to 5 entries, newest first: `"{event_type} {local:%a %d %b %H:%M} · {title}"`.

**`observe`.** For each detected pattern, look up `find_by_pattern_key`:

| Existing memory | Action | Result |
|---|---|---|
| suppressing status | skip | sticky |
| none | add it (see below) | `created` |
| active or faded, with fewer observations than now | update content, confidence, observation_count, status=active, strength=1, strength_as_of=now | `strengthened` |
| otherwise | nothing; this ends per-turn reinforcement | no change |

A new memory is added with:
- `memory_type="behavioral_pattern"`, provenance inferred, `origin="behavior"`;
- `confidence=min(0.9, rate)`, `observation_count=occurrences`;
- `applied_as="soft_preference"` for skip windows.

Every change publishes `PatternDetected`.

**`insights`** is built from `current()`: views that have a stored, unsuppressed memory and whose live pattern has confidence ≥ 0.7 and occurrences ≥ 3. The copy uses the live pattern's numbers, never the memory's capped confidence: `{n} = round(confidence × occurrences)`, `{total} = occurrences`.

**`insights` copy** (truthful per key prefix):
- skip: `"I keep demanding work out of the {h12} hour — you've set aside {n} of {total} there."`
- peak: `"You do your best work around {h12}."`
- crunch: `"You tend to finish deadlined work on the last day."`
- repeated_skip: uses the description.

**`memory_slots`:**
- `constraint` memories become hard blocks via `_parse_time_range`.
- Only `skip_time_window:{h}` patterns with confidence ≥ 0.6 become `minimal_work` hour slots.
- Every other pattern is ignored. This fixes a latent bug where any "hour N" text blocked that hour.

**Observation frame** (keys kept, two added): `{"type": detector, "content", "confidence": rate, "occurrence_count", "action", "pattern_key", "memory_id"}`.

- [ ] **Step 1: Write failing tests** in `tests/test_patterns.py`.

  Detector tests:
  - `test_skip_window__three_of_three_at_nine__pattern_hour_9`
  - `test_skip_window__two_of_three__nothing`
  - `test_skip_window__missed_counts_as_set_aside`
  - `test_skip_window__utc_timestamps__bucketed_by_local_hour`
  - `test_peak_hour__evening_completions__exactly_one_pattern`
  - `test_deadline_crunch__last_day_completions__pattern`
  - `test_repeated_skip__same_task_three_times__pattern_names_title`

  Service tests:
  - `test_observe__first_time__created_and_pattern_detected_published`
  - `test_observe__no_new_evidence__no_change_no_reinforcement`
  - `test_observe__more_evidence__strengthened`
  - `test_observe__dismissed_key__never_recreated`
  - `test_observe__other_users_events__ignored`
  - `test_pattern_observer__task_skipped__observe_runs`

  Bridge tests, in `tests/test_memory_constraint_bridge.py`:
  - `test_memory_slots__peak_hour_pattern__never_blocks_that_hour`
  - `test_memory_slots__skip_pattern__minimal_work_hour`
  - `test_memory_slots__constraint_memory__hard_block`

  Behaviour tests on events: the four PEARL parametrised cases, the learning-moment shape test and the other-users test keep their assertions. `test_bridge__dismissed_pattern__hands_the_hour_back` now passes without its xfail.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS, with 3 xfails remaining.
- [ ] **Step 5: Commit** `feat(learning): PEARL reads task events; dismissed patterns stay dismissed`.

### Task 7: Learning in the turn — one extraction call, persisted inline, frames before `complete`

**Files:**
- Modify:
  - `app/schemas/context.py`: `BrainDumpExtraction.memories`.
  - `app/schemas/memory.py`: `ExtractedMemory.contradicts` holds a handle such as `"m3"`.
  - `app/services/analytical/control_policy.py::BRAIN_DUMP_EXTRACTION_PROMPT`: a stable memory section.
  - `app/orchestrator/graph.py::_extract_brain_dump`: the user prompt lists up to 15 known memories as handles.
  - `app/orchestrator/state.py`: `session_id`, `user_message_id`, `memory_handles: dict[str, str]`.
  - `app/core/observation.py`: memories are persisted and frames emitted inline; `_background_memory_extraction` is no longer called.
  - `app/api/v1/endpoints/chat.py::chat_stream_v2`:
    - The initial state carries `session_id` and the id `save_user_message` returns.
    - The `complete` payload fields below.
    - The dead node-state list frames for `memory_extracted`/`pattern_detected` are removed.
- Test: `tests/test_turn_learning.py`, `tests/test_observation.py`, `tests/behaviour/test_long_term_memory.py`, `tests/behaviour/test_chat_turns.py`

**Interfaces:**
- Consumes: `MemoryService.remember/list_active` (Task 5), `PatternService.observe/insights` (Task 6), via `registered_container()`. With no container, learning is skipped.
- Produces the SSE contract that Task 14 builds against:
```text
event: memory_extracted   one frame per created / reinforced / superseded memory (suppressed: no frame)
  {"memory_id": str, "type": str, "content": str, "confidence": float, "provenance": "said",
   "action": "created" | "reinforced" | "superseded"}
event: pattern_detected   one frame per PatternChange (Task 6 frame)
  {"memory_id", "pattern_key", "type", "content", "confidence", "occurrence_count", "action": "created" | "strengthened"}
both strictly before `event: complete`; nothing learning-related is spawned after the stream.
complete payload adds:
  "pearl_insights": [{"insight": str, "confidence": float, "occurrences": int, "memory_id": str}] | null
  "suggested_action": "replan" | null      # "replan" when saved_constraints non-empty or anti_guilt_message set
  "schedule_status": "draft" | "infeasible" | null   # "infeasible" when final error == "INFEASIBLE_EXHAUSTED"
  "conflict_summary": str | null           # that turn's clarification text
```
```python
class BrainDumpExtraction:   # added field
    memories: list[ExtractedMemory] = Field(default_factory=list, description=(
        "Lasting facts, preferences, goals or constraints the USER states about themselves in THIS message. "
        "Never from the assistant's words, never today-only moods (those are state_updates), "
        "never a repeat of inline_habits. contradicts: the [mN] handle of a known memory this replaces."))
# app/core/observation.py
async def persist_turn_memories(state: dict) -> list[RememberOutcome]   # remember() each brain_dump.memories, provenance SAID,
                                                                        # source ids from state, total capped by MEMORY_PERSIST_TIMEOUT_S (2.0)
```

**Prompt layout.**
- The memory instructions go in the stable system prompt.
- The known memories go in the **user** prompt, which is volatile, as lines like `[m1] (preference) …`.
- Handles map back to memory ids from `memory_handles`. An unknown handle is ignored.
- **Order inside the observation loop:** persist memories → emit memory frames → `patterns.observe` (0.5 s cap) → emit pattern frames.

- [ ] **Step 1: Write failing tests** in `tests/test_turn_learning.py`. These go through `chat_stream_v2` with the behaviour harness pieces: ScriptedLLM, FakeSupabase and the container.
  - `test_turn__non_trivial_message__no_separate_memory_extraction_call`: `llm.tasks() == ["brain_dump_extraction", "voice_of_jarvis"]`.
  - `test_turn__stated_preference__memory_extracted_frame_before_complete`: the frame's index is less than the `complete` frame's index.
  - `test_turn__pattern_change__pattern_detected_frame_before_complete`.
  - `test_turn__memory__provenance_said_with_session_and_message_ids`.
  - `test_turn__forgotten_fact_said_again__no_frame_no_row` (RF3).
  - `test_turn__contradiction_handle__supersedes_that_memory`.
  - `test_turn__unknown_handle__ignored`.
  - `test_turn__persist_past_cap__frames_for_what_landed_and_turn_completes`.
  - `test_turn__greeting__no_extraction_and_no_learning_frames`.
  - `test_complete__saved_constraint__suggested_action_replan`.
  - `test_complete__infeasible_exhausted__schedule_status_and_conflict_summary`.
  - `test_complete__confirmed_pattern__pearl_insights_listed`.

  Behaviour memory tests now script `brain_dump_extraction` with `memories` instead of `memory_extraction`. `tests/test_observation.py` drops its background-extraction assertions.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `feat(learning): learning moments arrive inside the turn, from one extraction call`.

### Task 8: Budgeted recall and a cache-stable prompt; checkpoint hygiene

**First:** invoke the `claude-api` skill and read `shared/prompt-caching.md`.

**Files:**
- Create: `app/services/memory/context.py`, `app/infrastructure/llm/messages.py`
- Modify:
  - `app/services/memory/memory_service.py`: `recall`.
  - `app/modules/conversation.py::run_general_chat`:
    - The system prompt is the constant `JARVIS_CHAT_PROMPT`.
    - Memory, documents and the plate travel as the note.
    - The `build_chat_context` fallback call is dropped.
  - `app/services/analytical/voice_of_jarvis.py`: both synthesis paths use the constant `VOICE_OF_JARVIS_PROMPT`, and memory goes in the note.
  - `app/api/v1/endpoints/chat.py::_read_conversation`:
    - `recall` plus `render_context_note` produce `memory_context`.
    - The panel `memories` are the recalled ones: `{memory_id, memory_type, content, confidence, provenance}`.
  - `app/infrastructure/llm/claude.py`: `_messages` uses `fold_system_notes`.
  - `app/infrastructure/llm/local.py` and `gemini.py`: fold before `hybrid_route_query`.
  - `app/orchestrator/checkpoint.py`: `_TRANSIENT_KEYS` gains `conversation_history` and `memory_context`; add `prune_checkpoints` and `CheckpointRetentionSweep`.
  - `app/main.py`: append `CheckpointRetentionSweep(checkpointer)` to `container.sweeps`.
  - `app/core/config.py`:
    - `MEMORY_PROFILE_TOKENS = 200`
    - `MEMORY_RECALL_TOKENS = 300`
    - `MEMORY_RECALL_MIN_SIMILARITY = 0.3`
    - `CHECKPOINT_KEEP_PER_THREAD = 20`
- Test: `tests/test_recall.py`, `tests/test_prompt_layout.py`, `tests/test_checkpointer.py`, `tests/test_llm_providers.py`, `tests/test_claude_provider.py`

**Interfaces:**
- Consumes: `MemoryRepository.nearest`, `Memory.strength_at`, `TextEmbedder` (Task 4), `MemoryService` (Task 5), `Sweep` (Task 1).
- Produces:
```python
class Recall(BaseModel):
    profile: list[Memory]; relevant: list[Memory]; tokens: int
    def all(self) -> list[Memory]
# MemoryService
async def recall(self, user_id: str, query: str, *, trivial: bool = False, now: datetime | None = None) -> Recall
# app/services/memory/context.py
def estimate_tokens(text: str) -> int                                   # ceil(len(text) / 4)
def render_memory_line(m: Memory, now: datetime) -> str                 # "- (you said, 12 days ago) …" / "- (I noticed, 3 days ago; seen 4 times) …"
def render_context_note(recall: Recall, *, now: datetime, zone: ZoneInfo, since: str | None = None,
                        plate: list[str] | None = None, documents: list[str] | None = None) -> str | None
def with_context_note(history: list[dict] | None, note: str | None) -> list[dict]   # appends {"role": "system", "content": note}
# app/infrastructure/llm/messages.py
def fold_system_notes(history: list[dict] | None, prompt: str) -> tuple[list[dict], str]   # claude._messages' rule, extracted
# app/orchestrator/checkpoint.py
async def prune_checkpoints(saver: AsyncSqliteSaver, keep_per_thread: int) -> int
class CheckpointRetentionSweep(Sweep):   # name = "checkpoint_retention", interval = timedelta(hours=24)
    def __init__(self, saver: AsyncSqliteSaver, keep_per_thread: int = CHECKPOINT_KEEP_PER_THREAD) -> None
```

**Recall:**
- **Profile.** Take active memories of types constraint, preference and goal with confidence ≥ 0.6. Order them by that type order, then by `last_confirmed_at or created_at`, newest first. Add lines while they fit `MEMORY_PROFILE_TOKENS`.
- **Relevant** (skipped when `trivial`, with no embedding call):
  1. `nearest(embed(query), statuses={ACTIVE}, limit=12, min_similarity=MEMORY_RECALL_MIN_SIMILARITY)`, minus the profile ids.
  2. Score each as `similarity × (0.5 + 0.5·strength_at(now)) × confidence`.
  3. Add them in score order while they fit `MEMORY_RECALL_TOKENS`.
- **Exclusions.** Faded, expired and below-floor memories never appear.

**Note layout.** One `system` entry after the history. Empty sections are omitted:
```text
<memory>
About you:
- (you said, 12 days ago) …
Relevant now:
- (I noticed, 3 days ago; seen 4 times) …
</memory>
<since>…</since>                         (Task 9)
<plate>up to 8 pending task titles with due hints</plate>
<documents>up to 3 chunks, 600 chars each</documents>
<now>Mon 16 Nov 2026, 08:05 (Asia/Kolkata)</now>
```

**Stable system prompts.**
- `JARVIS_CHAT_PROMPT` and `VOICE_OF_JARVIS_PROMPT` become byte-constant module strings.
- Each gains a stable "How to read the context you're given" section covering:
  - the tags above;
  - "you said" outranks "I noticed";
  - never invent a memory;
  - anti-guilt wording.
- This lifts each to at least 2,400 characters, a safe margin over 512 tokens.
- The reason: today they are about 450 and 250 tokens, below Sonnet 5.5's 512-token cache minimum, so the cached block is never read.

**Providers.**
- `claude.py` already folds history `system` entries into the final user turn.
- The helper extracts that rule so local and Gemini render identically. Gemma's chat template rejects a mid-conversation system turn, and LM Studio's prefix KV cache gains from the same stable prefix.

**Retention.** Per `(thread_id, checkpoint_ns)`, delete every checkpoint except the newest `keep_per_thread` (checkpoint ids are time-ordered), and delete their `writes` rows.

- [ ] **Step 1: Write failing tests.**

  Recall tests:
  - `test_recall__new_user__empty_and_no_other_users` (RF1)
  - `test_recall__profile_capped_at_budget`
  - `test_recall__relevant_ranked_by_similarity_freshness_confidence`
  - `test_recall__trivial__profile_only_and_no_embedding_call`
  - `test_recall__faded_or_expired__absent`
  - `test_recall__hundred_memories__never_over_500_tokens`

  Layout tests, in `tests/test_prompt_layout.py`, capturing `LLMRequest` through ScriptedLLM:
  - `test_chat_system_prompt__identical_for_two_users_and_two_times`
  - `test_chat_system_prompt__at_least_2400_chars`
  - `test_voj_system_prompt__identical_and_at_least_2400_chars`
  - `test_reply_request__memory_note_is_last_history_entry`
  - `test_render_memory_line__said_vs_noticed_and_age`

  Provider tests:
  - `test_fold_system_notes__notes_prefixed_to_prompt_and_removed_from_turns`
  - `test_local_provider__system_note__folded_before_transport`

  The existing `test_claude_provider.py` tests stay green.

  Checkpoint tests:
  - `test_checkpoint__history_and_memory_context__not_persisted`
  - `test_prune_checkpoints__keeps_newest_n_per_thread`
  - `test_checkpoint_retention_sweep__returns_deleted_count`
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `feat(memory): budgeted recall in a cache-stable prompt; checkpoints stay small`.

### Task 9: Conversations that remember — session summaries, rolling compaction, "since we last spoke"

**Files:**
- Create: `app/services/memory/conversation_memory.py`, `supabase/migrations/20261115000003_session_summaries.sql`
- Modify:
  - `app/domain/entities.py`
  - `app/domain/ports.py`
  - The persistence families and factory: `conversations`.
  - `app/schemas/memory.py`: `SessionSummary`.
  - `app/core/model_router.py`: `MODEL_ROUTING["session_summary"] = ModelRole.FAST`.
  - `app/api/v1/endpoints/chat.py`:
    - History comes from `build_history`.
    - `since_last_spoke` goes into the note and the `complete` payload.
    - After the `complete` frame, `compact_if_needed` runs as a tracked background task, held in a strong-reference set as `observation._spawn_background` does.
  - `app/container.py`: `conversation_memory`; append `SessionSummarySweep`.
  - `app/core/config.py`:
    - `HISTORY_MAX_MESSAGES = 24`
    - `HISTORY_MAX_CHARS_PER_MESSAGE = 1500`
    - `COMPACT_AFTER_MESSAGES = 16`
    - `COMPACT_KEEP_RECENT = 8`
    - `SESSION_IDLE_MINUTES = 30`
  - `docs/capstone/_schema_notes.md`.
- Test: `tests/test_conversation_memory.py`, `tests/test_repository_contracts.py`, `tests/test_migrations.py`, `tests/behaviour/test_short_term_memory.py`

**Interfaces:**
- Consumes: `TaskEventRepository` (event counts), `route_llm_call`, `Sweep` (Task 1), `render_context_note(since=...)` (Task 8).
- Produces:
```python
class ConversationSession(BaseModel):
    id: str; user_id: str; title: str | None; created_at: datetime; updated_at: datetime
    summary: str | None = None; summary_through: datetime | None = None; summary_updated_at: datetime | None = None
class ChatMessage(BaseModel): id: str; session_id: str; user_id: str; role: Literal["user","assistant","system"]; content: str; created_at: datetime
class ConversationRepository(ABC):     # reads chat_sessions / chat_messages; writes only the summary columns
    async def get_session(self, user_id: str, session_id: str) -> ConversationSession | None
    async def previous_session(self, user_id: str, before_session_id: str) -> ConversationSession | None   # newest other session with ≥ 1 message
    async def messages(self, user_id: str, session_id: str, *, after: datetime | None = None, limit: int = 50) -> list[ChatMessage]  # newest `limit`, returned oldest first
    async def set_summary(self, user_id: str, session_id: str, summary: str, through: datetime) -> None
    async def recent_sessions(self, user_id: str, *, since: datetime) -> list[ConversationSession]
class SessionSummary(BaseModel):
    summary: str = Field(max_length=1200, description=...); open_threads: list[str] = Field(default_factory=list, max_length=5, description=...)
class HistoryWindow(BaseModel): history: list[dict]; summarized: bool; message_count: int
class ConversationMemoryService:
    def __init__(self, conversations: ConversationRepository, task_events: TaskEventRepository, *, clock=utc_now) -> None
    async def build_history(self, user_id: str, session_id: str) -> HistoryWindow
    async def compact_if_needed(self, user_id: str, session_id: str) -> bool
    async def summarize_session(self, user_id: str, session_id: str) -> bool
    async def since_last_spoke(self, user_id: str, session_id: str, *, zone: ZoneInfo) -> str | None
class SessionSummarySweep(Sweep):   # name = "session_summary", interval = timedelta(minutes=5)
    def __init__(self, conversation_memory: ConversationMemoryService) -> None
# complete payload adds: "since_last_spoke": str | null   (what the model was told)
```

**Rules:**
- **`build_history`:**
  - It takes messages after `summary_through`: the newest `HISTORY_MAX_MESSAGES`, each cut to `HISTORY_MAX_CHARS_PER_MESSAGE`.
  - A summary becomes a leading `system` entry: `Earlier in this conversation: {summary}`.
  - A lagging summary only makes the window longer.
  - If more unsummarized messages exist than fit, the leading note says older turns are not shown, as today.
- **`compact_if_needed`:**
  - It runs when more than `COMPACT_AFTER_MESSAGES` messages are unsummarized.
  - It makes one FAST call, `route_llm_call(task="session_summary", ..., response_schema=SessionSummary)`, over the old summary plus every unsummarized message except the newest `COMPACT_KEEP_RECENT`.
  - It then calls `set_summary(through = created_at of the last summarized message)`.
  - On any failure it logs and changes nothing.
- **`summarize_session`:**
  - It does the same over all unsummarized messages, but only when there are at least 2.
  - The sweep picks sessions idle at least `SESSION_IDLE_MINUTES` whose `summary_updated_at` is null or older than `updated_at`.
- **`since_last_spoke`:**
  - It applies only when the current session has no messages and a previous session exists.
  - It is deterministic, with no model call: `Since we last spoke ({gap}): {prev.summary or 'you were talking about "' + prev.title + '"'}. Since then: {n} done, {m} set aside.`
  - The counts come from completed versus skipped+missed events since `prev.updated_at`.
  - A zero clause is omitted. No blame phrases.
- **Migration.** Add to `chat_sessions`: `summary text`, `summary_through timestamptz`, `summary_updated_at timestamptz`, and a check `summary is null or char_length(summary) <= 4000`.

- [ ] **Step 1: Write failing tests:**
  - `test_build_history__short_session__all_messages_no_summary`
  - `test_build_history__summary__leading_note_then_messages_after_through`
  - `test_build_history__long_message__cut_to_1500_chars`
  - `test_compact__17_unsummarized__one_fast_call_keeps_newest_8` (ScriptedLLM: one `session_summary` call with role `fast`)
  - `test_compact__16__no_call`
  - `test_compact__llm_fails__nothing_changes`
  - `test_long_conversation__60_turns__window_bounded_and_newest_kept`
  - `test_summary_sweep__only_idle_unsummarized_sessions`
  - `test_since_last_spoke__new_session__previous_summary_and_counts`
  - `test_since_last_spoke__unsummarized_previous__title_fallback_and_no_llm_call`
  - `test_since_last_spoke__second_turn__none`
  - `test_since_last_spoke__no_blame_phrases`

  Contract tests:
  - `test_conversations__previous_session__newest_other_with_messages`
  - `test_conversations__other_user__invisible`
  - `test_conversations__set_summary__roundtrip`

  Behaviour test: `test_short_term__new_session__model_told_since_we_last_spoke`.

  Migration test: `test_migrations__session_summaries__columns_and_check`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `feat(memory): conversations are summarised, compacted, and picked up where they left off`.

### Task 10: Anticipation framework — consent chain, trigger template, scheduler, API

**Files:**
- Create:
  - `app/domain/anticipation.py`
  - `app/services/anticipation/__init__.py`, `service.py`, `scheduler.py`
  - `app/api/v1/endpoints/anticipation.py`
  - `supabase/migrations/20261115000004_suggestion_consent_kind.sql`
- Modify:
  - `app/domain/entities.py`: new enum `ConsentReasonKind` (defined here, beside the other enums, because `app.domain.anticipation` imports entities and the reverse import would be circular) and `Suggestion.consent_kind: ConsentReasonKind | None = None`.
  - `app/domain/ports.py`:
    - `SuggestionRepository.exists_today(user_id, trigger, day, *, tz: str = "UTC")`
    - `SuggestionRepository.set_status(user_id, suggestion_id, to, *, expected: SuggestionStatus | None = None)`
  - `app/infrastructure/persistence/{in_memory,supabase_repos,_mapping}.py`: `day_bounds(day, zone)`; compare-and-set on `set_status`.
  - `app/api/v1/router.py`: prefix `/anticipation`.
  - `app/container.py`: `consent`, `actions: ActionFactory`, `anticipation: AnticipationService`.
  - `app/main.py`: the lifespan builds the scheduler, starts it when enabled, and stops it before `usage_ledger.shutdown()`.
  - `app/core/config.py`: `ANTICIPATION_ENABLED = os.getenv("JARVIS_ANTICIPATION", "true").lower() != "false"`, `ANTICIPATION_TICK_SECONDS = 60`.
  - `tests/conftest.py`: autouse `monkeypatch.setattr(config, "ANTICIPATION_ENABLED", False)`.
- Test: `tests/test_consent.py`, `tests/test_trigger_template.py`, `tests/test_anticipation_service.py`, `tests/test_anticipation_scheduler.py`, `tests/test_anticipation_api.py`, `tests/test_repository_contracts.py`

**Interfaces:**
- Consumes: `Sweep` and `container.sweeps` (Task 1), `SuggestionRepository`, `ProfileRepository`, `ConstraintRepository` (Plan 1), `constraint_bridge._parse_time_range`, `attribute_llm_calls`.
- Produces:
```python
# app/domain/anticipation.py
class ActionRisk(str, Enum): READ_ONLY="read_only"; LOW_RISK="low_risk"; SCHEDULE_CHANGE="schedule_change"
# ConsentReasonKind lives in app/domain/entities.py: RULE_MATCH="rule_match"; MODE_DECISION="mode_decision"; RISK_CHECK="risk_check"; LEARNED_PREFERENCE="learned_preference"
class ConsentDecision(BaseModel): behavior: Literal["allow","ask","deny"]; kind: ConsentReasonKind; message: str
class ActionResult(BaseModel): kind: str; message: str; tasks_written: int | None = None; draft_id: str | None = None
class ProactiveAction(ABC):                              # Command
    kind: ClassVar[str]; risk: ClassVar[ActionRisk]
    @abstractmethod
    def describe(self) -> str: ...
    @abstractmethod
    def to_payload(self) -> dict: ...                     # includes "kind" and "dedupe_key"
    @abstractmethod
    async def execute(self, user_id: str) -> ActionResult: ...
    def dedupe_key(self) -> str                           # default: kind
    def blocked_hours(self) -> tuple[int, int] | None     # local [start, end) it would block; default None
class ConsentContext(BaseModel): autonomy_mode: AutonomyMode; stated_blocks: list[tuple[int, int, str]]; dismissed_keys: frozenset[str]
class ConsentRule(ABC):
    @abstractmethod
    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision | None: ...
class RepeatDismissalRule(ConsentRule); class RiskCheckRule(ConsentRule)
class StatedRuleCoverageRule(ConsentRule); class ModePolicyRule(ConsentRule)
class AutonomyPolicy(ABC):
    @abstractmethod
    def decide(self, action: ProactiveAction) -> ConsentDecision: ...
class SuggestOnly(AutonomyPolicy); class AskFirst(AutonomyPolicy); class AutoLowRisk(AutonomyPolicy)
def autonomy_policy_for(mode: AutonomyMode) -> AutonomyPolicy
class ConsentPolicy:                                     # Chain of Responsibility
    def __init__(self, rules: list[ConsentRule]) -> None # ValueError unless the last rule is a ModePolicyRule
    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision
    @classmethod
    def default(cls) -> "ConsentPolicy"
class TriggerContext(BaseModel): user_id: str; profile: UserProfile; now: datetime; local_day: date; force: bool = False
class SuggestionDraft(BaseModel): kind: str; title: str; body: str; action: ProactiveAction; expires_at: datetime | None = None  # arbitrary_types_allowed
class TriggerOutcome(BaseModel): status: Literal["created","already_done","not_due","nothing_to_say","denied"]; suggestion: Suggestion | None = None; decision: ConsentDecision | None = None
class Trigger(ABC):                                      # Template Method
    name: ClassVar[str]
    async def run(self, ctx: TriggerContext, *, consent: ConsentPolicy, consent_ctx: ConsentContext,
                  suggestions: SuggestionRepository) -> TriggerOutcome      # the skeleton; subclasses never override it
    async def already_done(self, ctx: TriggerContext, suggestions: SuggestionRepository) -> bool   # default: exists_today(name, local_day, tz=profile.timezone)
    @abstractmethod
    async def should_fire(self, ctx: TriggerContext) -> bool: ...
    @abstractmethod
    async def gather(self, ctx: TriggerContext) -> Any | None: ...           # None = nothing to say: no model call, nothing stored
    @abstractmethod
    async def build_suggestion(self, ctx: TriggerContext, gathered: Any) -> SuggestionDraft: ...
# app/services/anticipation/service.py
class ActionFactory:
    def register(self, kind: str, build: Callable[[dict], ProactiveAction]) -> None
    def from_payload(self, payload: dict) -> ProactiveAction           # ValidationError for an unknown kind
class AnticipationService:
    def __init__(self, triggers: dict[str, Trigger], actions: ActionFactory, consent: ConsentPolicy,
                 suggestions: SuggestionRepository, profiles: ProfileRepository, constraints: ConstraintRepository,
                 *, clock: Callable[[], datetime] = utc_now) -> None
    def register_trigger(self, trigger: Trigger) -> None
    async def run_trigger(self, user_id: str, name: str, *, force: bool = False, now: datetime | None = None) -> TriggerOutcome
    async def run_due(self, user_id: str, now: datetime) -> list[TriggerOutcome]
    async def list_suggestions(self, user_id: str, *, status: SuggestionStatus | None = SuggestionStatus.PENDING) -> list[Suggestion]
    async def accept(self, user_id: str, suggestion_id: str) -> tuple[Suggestion, ActionResult]
    async def dismiss(self, user_id: str, suggestion_id: str) -> Suggestion
# app/services/anticipation/scheduler.py
class AnticipationScheduler:
    def __init__(self, service: AnticipationService, profiles: ProfileRepository, sweeps: list[Sweep], *,
                 tick_s: float = ANTICIPATION_TICK_SECONDS, clock: Callable[[], datetime] = utc_now) -> None
    async def tick(self, now: datetime) -> dict          # {"sweeps": {name: changed}, "outcomes": int, "errors": int}
    def start(self) -> None
    async def stop(self) -> None
```

**Consent chain order and copy.** The first non-`None` decision wins:

| # | Rule | When | Decision | Kind | Message |
|---|---|---|---|---|---|
| 1 | RepeatDismissal | `dedupe_key` is in `dismissed_keys` | deny | LEARNED_PREFERENCE | "You dismissed this before, so I won't bring it up again." |
| 2 | RiskCheck | READ_ONLY | allow | RISK_CHECK | "Nothing changes until you act on it." |
| 3 | StatedRuleCoverage | `blocked_hours` lies inside a stated block | deny | RULE_MATCH | `You already told me: "{raw_text}".` |
| 4 | ModePolicy | always (delegates to the autonomy policy) | allow / ask | MODE_DECISION | see below |

- **ModePolicy outcomes:**
  - SuggestOnly → ask, "You asked me to only suggest."
  - AskFirst → ask, "This changes your schedule, so I'm asking first."
  - AutoLowRisk → allow for LOW_RISK, ask otherwise.
- **Building `ConsentContext`.** The service builds it per user:
  - `stated_blocks` comes from the user's `source == "said"` constraints that `_parse_time_range` can read, converted to local clock hours.
  - `dismissed_keys` is the `dedupe_key` of each dismissed suggestion.

**`Trigger.run` outcomes, in order:**
1. `already_done` → `already_done`.
2. Not forced and `should_fire` is false → `not_due`.
3. `gather` returns `None` → `nothing_to_say`.
4. The decision is deny → `denied` (nothing stored).
5. Otherwise the suggestion is stored as `Suggestion(status=PENDING, consent_behavior, consent_reason=message, consent_kind, action=to_payload(), ...)` → `created`.

An allowed **non**-read-only action runs at once and is stored as `AUTO_APPLIED`. An allowed read-only one is stored as pending, because it is information.

**`accept`:**
1. Claim the suggestion with `set_status(ACCEPTED, expected=PENDING)`. A `StaleVersionError` means 409.
2. Execute the action.
3. If execution raises `InvalidStateTransition` or `NotFoundError`, set status `EXPIRED` and raise `InvalidStateTransition("This suggestion is out of date — a newer plan replaced it.")` (409).
4. If execution raises `PersistenceError`, set status back to `PENDING` and re-raise (503).

**`dismiss`** requires `PENDING` and sets `DISMISSED`.

**Scheduler:**
- Each tick runs the sweeps whose `interval` has elapsed, over `profiles.list_user_ids()`, then calls `run_due` per user, one user at a time. Each user's work is wrapped in `attribute_llm_calls(user_id, None)`.
- An exception for one user or one sweep is logged with the user id; the tick carries on.
- `start()` creates one task that ticks every `tick_s`, with the first tick after `tick_s`. `stop()` cancels the task and awaits it.

**API.** Every route takes `user: CurrentUser` and declares a `response_model`:

| Route | Response |
|---|---|
| `GET /api/v1/anticipation/suggestions?status=pending` | `SuggestionListResponse{suggestions: list[SuggestionOut]}` |
| `POST /api/v1/anticipation/suggestions/{id}/accept` | `AcceptSuggestionResponse{suggestion: SuggestionOut, result: ActionResultOut}` |
| `POST /api/v1/anticipation/suggestions/{id}/dismiss` | `SuggestionOut` |
| `POST /api/v1/anticipation/run`, body `RunTriggerRequest{trigger: Literal["morning_brief","pattern_rule"]}` (runs with `force=True`) | `RunTriggerResponse{status, suggestion: SuggestionOut \| None, consent: ConsentOut \| None}` |

- `SuggestionOut` fields: `id, trigger, kind, title, body, status, consent_behavior, consent_kind, consent_reason, created_at, expires_at, details: dict` (the action payload).

**Migration:** `alter table proactive_suggestions add column if not exists consent_kind text`, with a CHECK on the four values or null.

- [ ] **Step 1: Write failing tests.**

  Consent tests:
  - one test per rule outcome
  - `test_consent_policy__last_rule_not_mode__value_error`
  - `test_autonomy__suggest_only__never_allows_a_schedule_change`
  - `test_autonomy__auto_low_risk__allows_low_risk_asks_schedule_change`

  Trigger-template tests, using a counting fake trigger:
  - `test_trigger_run__already_done__gather_never_called`
  - `test_trigger_run__not_due__nothing_stored`
  - `test_trigger_run__force__skips_should_fire_not_idempotency`
  - `test_trigger_run__nothing_to_say__no_suggestion_and_zero_llm_calls` (RF1)
  - `test_trigger_run__deny__nothing_stored_decision_returned`
  - `test_trigger_run__ask__pending_with_typed_reason`

  Service tests:
  - `test_accept__pending__claims_executes_accepted`
  - `test_accept__double_click__second_409_and_action_executed_once` (RF4)
  - `test_accept__action_out_of_date__expired_and_409_plain_message`
  - `test_accept__store_down__back_to_pending_and_503`
  - `test_accept__other_users_suggestion__404`
  - `test_dismiss__then_same_key__denied_next_time`

  Scheduler tests:
  - `test_tick__sweep_runs_once_per_interval`
  - `test_tick__one_users_trigger_raises__others_still_run`
  - `test_tick__profiles_unreachable__logged_and_next_tick_runs`
  - `test_start_stop__clean_cancel`
  - `test_tick__background_llm_calls_attributed_to_user` (the fake chain sees `req.user_id`)

  Contract tests:
  - `test_suggestions__exists_today__ist_half_past_midnight_is_new_local_day` (RF5)
  - `test_suggestions__set_status_expected_mismatch__stale_version`

  API tests:
  - `test_anticipation_api__run_unknown_trigger__422_envelope`
  - `test_anticipation_api__list__pending_only_by_default`
  - `test_anticipation_api__accept_twice__409_envelope`
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS. Start the server once: the startup log shows the scheduler started, and `GET /api/v1/anticipation/suggestions?user_id=x` returns 200 `{"suggestions": []}`.
- [ ] **Step 5: Commit** `feat(anticipation): consent chain, trigger template, scheduler and suggestions API`.

### Task 11: The first move — MorningBrief, PatternRule, and the replanner

**Files:**
- Create: `app/services/anticipation/triggers.py`, `app/services/anticipation/actions.py`, `app/modules/planning_replan.py`
- Modify:
  - `app/domain/ports.py`: `DraftPlanner`; `TaskRepository.list_by_status`.
  - The persistence families: `list_by_status`.
  - `app/core/user_model.py`: `get_missed_tasks()`, same cache pattern as `get_pending_tasks`.
  - `app/modules/planning_graph.py`: replan mode; precomputed habit slots.
  - `app/core/model_router.py`: the PRIMARY lock.
  - `app/container.py`:
    - Register both triggers and `build_action_factory(...)`.
    - Subscribe `AnticipationListener` to `PatternDetected`.
    - `planner: DraftPlanner = PlanningGraphReplanner(...)`.
  - `app/core/config.py`: `MORNING_BRIEF_WINDOW_HOURS = 6`.
- Test: `tests/test_triggers.py`, `tests/test_planning_replan.py`, `tests/test_model_router.py`, `tests/test_repository_contracts.py`, `tests/behaviour/test_anticipation.py`

**Interfaces:**
- Consumes:
  - `Trigger`, `ProactiveAction`, `ActionFactory`, `AnticipationService` (Task 10)
  - `DraftService` (Task 2)
  - `EnergyEstimator` (Task 3)
  - `PatternService.current/insights` (Task 6)
  - `MemoryService.confirm` (Task 5)
  - `TaskEventRepository`, `ConstraintRepository`, `SemanticTimeSlot`, `constraint_bridge._to_horizon`
- Produces:
```python
class DraftPlanner(ABC):
    @abstractmethod
    async def replan(self, user_id: str, *, reslot_task_ids: list[str]) -> DraftSchedule | None: ...
        # None: nothing to plan. ScheduleInfeasibleError: nothing fits even in 30 days. Never decomposes.
# TaskRepository
async def list_by_status(self, user_id: str, statuses: frozenset[TaskStatus]) -> list[Task]
# app/modules/planning_replan.py
class PlanningGraphReplanner(DraftPlanner):
    def __init__(self, drafts: DraftRepository, *, db_provider: Callable[[], Any] = get_db,
                 memory_provider: Callable[[], Any] = get_memory_store) -> None
# app/services/anticipation/actions.py
class ApplyDraftAction(ProactiveAction):     # kind="apply_plan", risk=SCHEDULE_CHANGE, dedupe_key=f"apply_plan:{draft_id}"
    def __init__(self, draft_id: str, *, drafts: DraftService) -> None
class AdoptRuleAction(ProactiveAction):      # kind="adopt_rule", risk=SCHEDULE_CHANGE, dedupe_key=f"pattern_rule:{pattern_key}", blocked_hours=(hour, hour+1)
    def __init__(self, pattern_key: str, hour: int, memory_id: str, *, constraints: ConstraintRepository,
                 memory: MemoryService, planner: DraftPlanner) -> None
class ShareInsightAction(ProactiveAction):   # kind="share_insight", risk=READ_ONLY; execute returns "Noted."
    def __init__(self, insight: str) -> None
def build_action_factory(*, drafts: DraftService, constraints: ConstraintRepository, memory: MemoryService,
                         planner: DraftPlanner) -> ActionFactory
# app/services/anticipation/triggers.py
class MorningBriefTrigger(Trigger):          # name = "morning_brief"
    def __init__(self, tasks: TaskRepository, task_events: TaskEventRepository, planner: DraftPlanner,
                 energy: EnergyEstimator, patterns: PatternService, actions: ActionFactory) -> None
class PatternRuleTrigger(Trigger):           # name = "pattern_rule"
    def __init__(self, patterns: PatternService, suggestions: SuggestionRepository, actions: ActionFactory) -> None
class AnticipationListener(EventListener):   # PatternDetected with a skip_time_window key -> run_trigger("pattern_rule", force=True)
    def __init__(self, anticipation: AnticipationService) -> None
```

**Planning changes (exactly these):**
- **New state.** `PlanningState` gains `replan_only: bool` and `reslot_task_ids: list`.
- **`is_goal_clear`** returns `True` when `replan_only`.
- **`decompose_goal`** returns `{"task_chunks": [], "goal_id": None}` with no model call when `replan_only`.
- **`fuse_tasks`** also merges the user's `missed` rows whose `task_id` is in `reslot_task_ids` (via `UserModel.get_missed_tasks()`) and adds them to `restored_task_ids`.
- **`fetch_constraints`:**
  - A habit whose `structured_semantics` has `semantic_slots` stays out of `habits_text`.
  - Its slots go to state `precomputed_slots`.
  - `translate_habits` returns the LLM slots plus the precomputed ones, with no model call when only precomputed slots exist.
- **`PlanningGraphReplanner.replan`:**
  - It invokes the compiled planning sub-graph (`build_planning_graph()`) with `user_id`, `UserModel(user_id, db_provider())` (memory store set), `user_message=""`, `replan_only=True`, `reslot_task_ids`, `draft_store=DraftStore(supabase)`, `horizon_minutes=DEFAULT_HORIZON_MINUTES` and `retry_count=0`.
  - It returns `await drafts.get(user_id, draft_id)`, or `None`.
  - `error == "INFEASIBLE_EXHAUSTED"` raises `ScheduleInfeasibleError(clarification)`.
  - The only model call it can make is PRIMARY habit translation, and only for free-text habits.
- **PRIMARY lock.** `route_llm_call` holds a module-level `asyncio.Lock` around every PRIMARY-role call, so a background brief and a user's planning turn never run two heavy calls at once. FAST calls never wait on it.

**MorningBrief:**
- **should_fire:** the local time is within `[wake_time, wake_time + MORNING_BRIEF_WINDOW_HOURS)` on `local_day`.
- **already_done:** the default, so the brief runs once per local day.
- **gather:**
  - `pending` = `list_by_status({PENDING})`.
  - `reslot` = tasks with status `missed` whose latest outcome event is `missed` (slot passed, not `deadline_missed`) and whose event occurred since 00:00 local yesterday.
  - `insight` = the first of `patterns.insights`.
  - `focus` = `energy.estimate`.
  - Return `None` when pending, reslot and insight are all empty.
- **build:**
  - If there are pending or reslot tasks, call `planner.replan(reslot_task_ids=...)` and attach an `ApplyDraftAction` with title "Today is ready".
  - Otherwise attach a `ShareInsightAction` with title "Something I noticed".
  - If `replan` returns `None` or raises `ScheduleInfeasibleError`, fall back to a read-only "I couldn't fit everything into today — want to look at it together?".
  - `expires_at` is `end_of_local_day`.
- **body** (deterministic, no model call; each line only when it applies):
  - `"{n} tasks are planned for today; the hardest, '{title}', is at {HH:MM}{peak}."`, where `peak = " — your strongest hour"` only when the profile is learned, that hour is its maximum, and its score is at least 0.7.
  - `"{k} from yesterday found a new slot."`
  - `"I noticed: {insight} (seen {occurrences} times)."`

**PatternRule:**
- **Firing.** `should_fire` is always `True`. `already_done` returns `False`. `gather` dedupes instead: it picks the first `skip_time_window` `PatternView` with confidence ≥ 0.7, occurrences ≥ 3 and not confirmed, whose `pattern_rule:{key}` has no suggestion in any status. With none, `gather` returns `None`.
- **Copy.** Title `"A pattern around {h12}"`. Body `"You've set aside {n} of the last {total} tasks around {h12}. Want me to stop putting demanding work there?"`, where `{n} = round(confidence × occurrences)` and `{total} = occurrences`, both from the live pattern.
- **`AdoptRuleAction.execute`:**
  1. `constraints.add(...)`, with these fields:
     - `BehavioralConstraint(id=uuid4().hex, raw_text=f"No demanding work between {h:02d}:00 and {h+1:02d}:00", constraint_type="habit", recurrence="daily", source="inferred", ...)`
     - `structured_semantics={"pattern_key": key, "semantic_slots": [SemanticTimeSlot(name=f"learned_rule_{h:02d}", start_min=_to_horizon(h*60), end_min=_to_horizon(h*60)+60, availability="blocked", recurrence="daily").model_dump()]}`
  2. `memory.confirm(memory_id)`.
  3. `draft = planner.replan(reslot_task_ids=[])`.
  4. Return `ActionResult(kind="adopt_rule", draft_id=..., message=...)`, with one of two messages:
     - with a draft: "Saved. I've drafted a plan that keeps {h12} free — accept it when it suits you."
     - without: "Saved. Every plan from now on keeps {h12} free."

- [ ] **Step 1: Write failing tests.**

  MorningBrief tests:
  - `test_brief__before_wake__not_due`
  - `test_brief__after_wake__pending_suggestion_with_draft_and_ask_reason`
  - `test_brief__second_run_same_local_day__already_done`
  - `test_brief__utc_midnight_crossing__fires_once_per_local_day` (RF5): runs at 07:05 IST (01:35 UTC) and at 11:00 IST.
  - `test_brief__forced_at_00_30_ist__counts_against_the_new_local_day`
  - `test_brief__nothing_to_say__no_suggestion_and_zero_llm_calls` (RF1)
  - `test_brief__yesterdays_slot_misses__reslot_ids_passed_and_deadline_misses_excluded`
  - `test_brief__copy__no_blame_phrases_and_peak_claim_only_when_learned`
  - `test_brief__replan_infeasible__read_only_suggestion_no_draft`
  - `test_brief__accept__tasks_written_and_rescheduled_events`
  - `test_brief__accept_after_newer_plan_accepted__409_and_expired_tasks_unchanged` (RF4)

  PatternRule tests:
  - `test_pattern_rule__qualifying_skip_window__suggestion_with_adopt_rule`
  - `test_pattern_rule__dismissed_before__denied`
  - `test_pattern_rule__user_already_said_rule__denied_rule_match` ("no work before 11 AM" plus a pattern at hour 9)
  - `test_pattern_rule__accept__constraint_with_semantic_slot_memory_confirmed_and_draft`
  - `test_anticipation_listener__pattern_detected__suggestion_created`

  Planning tests, in `tests/test_planning_replan.py`, using the real sub-graph over FakeSupabase and real CP-SAT:
  - `test_replan__no_tasks__none_and_no_llm_call`
  - `test_replan__pending_tasks__draft_without_decomposition`
  - `test_replan__reslot_missed__included_and_accept_reslots`
  - `test_fetch_constraints__precomputed_slots__no_habit_translation_call`
  - `test_replan__learned_rule__no_task_in_that_hour`

  Router tests:
  - `test_route_llm_call__two_primary_calls__never_overlap`
  - `test_route_llm_call__fast_call__not_blocked_by_primary`

  Contract test: `test_tasks__list_by_status__filters_and_isolates`.

  Behaviour test, in `tests/behaviour/test_anticipation.py`: `test_anticipation__three_skips_at_nine__rule_suggested_accepted_and_next_plan_keeps_nine_free`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `feat(anticipation): Jarvis makes the first move — morning brief and learned-rule proposals`.

### Task 12: The You page API, and the remaining UTC "today"s

**Files:**
- Create: `app/services/user_model_service.py`, `app/api/v1/endpoints/you.py`
- Modify:
  - `app/api/v1/router.py`: prefix `/you`.
  - `app/container.py`: `user_model_service`.
  - `app/services/analytical/mastery_tracker.py`: add `compute_streak_from_events`; `_get_reschedule_count` counts skipped events whose `metadata.goal_id` matches, read from `task_events`.
  - `app/modules/coach.py`: the streak comes from events in the user's zone, via `registered_container().repos.task_events`.
  - `app/services/analytical/sm2_engine.py::get_due_trackers`.
  - `app/api/v1/endpoints/habits.py::list_due_trackers`: passes the user's zone, read from the profile.
- Test: `tests/test_user_model_service.py`, `tests/test_you_api.py`, `tests/test_local_day_fixes.py`

**Interfaces:**
- Consumes:
  - `ProfileRepository`, `ConstraintRepository`, `UsageLedger.receipt` (Plan 1)
  - `EnergyEstimator` (Task 3)
  - `MemoryService.list_active` (Task 5)
  - `PatternService.observe/current` (Task 6)
  - `MemoryOut` (Task 5)
- Produces:
```python
class UserModelService:
    def __init__(self, profiles: ProfileRepository, memory: MemoryService, patterns: PatternService,
                 constraints: ConstraintRepository, energy: EnergyEstimator, usage: UsageLedger,
                 *, clock: Callable[[], datetime] = utc_now) -> None
    async def snapshot(self, user_id: str) -> YouView        # runs patterns.observe first (idempotent upsert)
    async def update_settings(self, user_id: str, *, autonomy_mode: AutonomyMode | None = None,
                              wake_time: time | None = None, timezone: str | None = None) -> UserProfile   # ValidationError on an unknown IANA zone
def compute_streak_from_events(events: list[TaskEvent], zone: ZoneInfo, today: date) -> int   # consecutive local dates with ≥1 completed event, ending today
async def get_due_trackers(user_id: str, supabase_client: Any = None, as_of_date: datetime | None = None,
                           zone: ZoneInfo | None = None) -> list[dict]   # local dates on both sides of the comparison
```
API contract, which Task 15 builds against. Every response model uses `Field(description=...)`:
```text
GET /api/v1/you?user_id=   -> YouResponse
{ "profile":  {"display_name": str|null, "timezone": str, "wake_time": "HH:MM",
               "autonomy_mode": "suggest_only"|"ask_first"|"auto_low_risk"},
  "patterns": [{"memory_id": str|null, "pattern_key": str, "detector": str, "description": str,
                "confidence": float, "occurrences": int, "evidence": [str], "confirmed": bool}],
  "rules":    [{"id": str, "text": str, "provenance": "said"|"inferred", "created_at": str|null}],
  "memories": [MemoryOut ...],                          # Task 5 shape; behavioral_pattern excluded
  "focus":    {"source": "prior"|"learned", "observations": int,
               "hours": [{"hour": int, "score": float, "done": int, "not_done": int}]},
  "receipt":  {"on_device_calls": int, "cloud_calls": int, "pii_redactions": int,
               "input_tokens": int, "output_tokens": int} }
PATCH /api/v1/you/settings?user_id=   body {"autonomy_mode"?, "wake_time"? ("HH:MM", pattern ^\d{2}:\d{2}$), "timezone"? (max 64)}
  -> YouResponse["profile"]
```
- [ ] **Step 1: Write failing tests.**

  Service tests:
  - `test_snapshot__new_user__empty_sections_prior_focus_zero_receipt` (RF1)
  - `test_snapshot__history__patterns_with_evidence_and_memory_ids`
  - `test_snapshot__dismissed_pattern__absent`
  - `test_snapshot__memories__provenance_and_freshness`
  - `test_snapshot__other_users_data__never_included`
  - `test_update_settings__autonomy_and_wake_time__persisted`

  API tests:
  - `test_you_api__get__matches_response_model`
  - `test_you_api__bad_timezone__422_envelope`
  - `test_you_api__store_down__503_envelope`

  Local-day tests:
  - `test_streak__completion_at_00_30_ist__counts_for_that_local_day` (RF5)
  - `test_streak__reads_events_not_statuses`
  - `test_reschedule_count__counts_skipped_events_for_goal`
  - `test_due_trackers__ist_morning__uses_local_date`
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `feat(you): one read of what Jarvis knows about you; every "today" is the user's`.

### Task 13: `scripts/seed_demo_user.py` — seeded history, live inference

**Files:**
- Create: `scripts/seed_demo_user.py`, `tests/test_seed_demo_user.py`
- Modify: nothing under `app/`. If the seed needs something the live app cannot do, stop and report it; never add a seed-only write path.

**Interfaces:**
- Consumes:
  - `DraftService` (Task 2, `clock`)
  - `TaskService` (Task 1, `clock`, `sweep_passed_slots`)
  - `MemoryService` (Task 5, `clock`)
  - `DraftRepository.create`, `ProfileRepository`
  - `AppContainer.build(..., install_chain=False)` (Task 5)
- Produces:
```python
class SimClock:
    def __init__(self, start: datetime) -> None
    def __call__(self) -> datetime
    def set(self, moment: datetime) -> None
@dataclass
class SeedServices:
    drafts: DraftService; draft_repo: DraftRepository; tasks: TaskService
    profiles: ProfileRepository; memory: MemoryService; clock: SimClock
def build_seed_services(repos: Repositories, embedder: TextEmbedder, clock: SimClock) -> SeedServices
async def seed_history(s: SeedServices, user_id: str, *, days: int, today: date, zone: ZoneInfo,
                       rng: random.Random) -> dict                 # {"days","tasks","events_by_type","memories"}
def reset_user(client: Any, user_id: str) -> None                  # llm_usage_events first, then user_profiles (cascade)
def main(argv: list[str] | None = None) -> int                     # --user demo --days 21 --reset --dry-run
```

**Mechanics.** Every row goes through a live service under the simulated clock:
1. The profile is created with `get_or_create`, then updated with `timezone="Asia/Kolkata"`, `wake_time=07:00`, `display_name="Demo"`, `autonomy_mode=ask_first`.
2. For each past local day D, oldest first:
   - Create a draft: 08:00 local on D as `horizon_start`, TaskChunk-valid tasks (≤ 25 min, WOOP if-then filled), and a slot map.
   - Accept it with `DraftService.accept`, clock at D 07:30. That writes `user_tasks` and `scheduled` events.
   - Record outcomes with `TaskService.complete`/`skip`, clock just after each slot.
   - At D 23:59, run `sweep_passed_slots`: untouched tasks become `missed`.
3. Three `said` memories are created with `MemoryService.remember` on their own dates.

**Regularities** (rng seed 42):
- **Weekday 09:00 "DSA practice"** (difficulty 0.8): 80% skipped, 10% untouched (missed by the sweep), 10% done.
- **19:00 and 20:00 tasks:** 90% done.
- **Weekly "OS assignment".** The deadline is Friday. Its Mon–Wed chunks go untouched (missed), then are completed from `missed` on Friday morning ("I actually did that"), which yields `deadline_crunch`.
- **Afternoons:** 50% done.

**Idempotency.** Without `--reset`, an existing user with any `task_events` prints `demo already seeded (N events) — pass --reset to rebuild` and exits 0. `--dry-run` runs on the InMemory family and prints the report, with no network.

- [ ] **Step 1: Write failing tests**, offline, on the InMemory family with `BagOfWordsEmbedder` and a chain that fails any call:
  - `test_seed__history__pearl_finds_morning_skips_and_evening_peak`: the real `PatternService.current` has `skip_time_window:9` and a `peak_hour` in 18–21.
  - `test_seed__history__focus_profile_learned_evening_above_morning`.
  - `test_seed__history__deadline_crunch_detected`.
  - `test_seed__every_row_shape_is_a_live_shape`:
    - event types ⊆ `TaskEventType`;
    - metadata keys ⊆ Task 1's keys plus `previous_start`;
    - every `user_tasks` row has a `plan_id`.
  - `test_seed__twice_without_reset__second_is_a_no_op`.
  - `test_seed__same_seed__same_counts`.
  - `test_seed__no_llm_calls`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** run the tests, then `.venv/bin/python scripts/seed_demo_user.py --dry-run`. Expected: the report prints around 21 days and around 90 tasks, with every event type except `deadline_missed`.
- [ ] **Step 5: Commit** `feat(demo): seed three weeks of history through the live services`.

### Task 14: Frontend — learning moments arrive, and the Plan-1 follow-ups

**Repo:** `jarvis-frontend`, branch `capstone-lld`.

**Files:**
- Create:
  - `vitest.config.ts` (environment node, alias `@` → repo root)
  - `lib/learning.ts`
  - `lib/__tests__/learning.test.ts`
  - `lib/__tests__/sse.test.ts`
  - `components/app/LearningMoments.tsx`
  - `components/app/ConsentNotice.tsx`
  - `components/app/ErrorState.tsx`
- Modify:
  - `package.json`: devDependency `vitest`, script `"test": "vitest run"`.
  - `lib/api.ts`:
    - Export `consumeSSE` and dispatch `consent_request`.
    - `listTasks`, `listDocuments`, `listMemories`, `getBehavioralConstraints` and `getDueHabits` throw `ApiError` on 5xx instead of returning `[]`.
  - `lib/types.ts`: the Task 7 SSE and payload fields; `PearlInsight{insight, confidence, occurrences, memory_id}`.
  - `lib/hooks/useJarvisChat.ts`:
    - Learning moments are collected per message.
    - `consent_request` is stored on the message.
    - The accept result goes through `describeAcceptOutcome`.
    - Remove the unused `PatternDetectedEvent` import.
  - `lib/context/JarvisContext.tsx`: `tasksError`.
  - `components/app/JarvisResponse.tsx`:
    - `LearningMoments` inline and `ConsentNotice`.
    - `ReplanBanner` shows on `suggested_action === "replan"`.
    - `InfeasibleGuidance` shows on `schedule_status === "infeasible"` with `conflict_summary`. Its three buttons send "Let's reduce the scope", "Let's extend the deadline" and "I can do more each day".
  - `components/app/LearningToast.tsx`: uses `shouldToast`.
  - `app/(app)/chat/page.tsx`.
  - `app/(app)/dashboard/page.tsx`: `DEMO_TASKS` and the memories fallback are deleted. Use `EmptyState` ("Nothing planned yet — tell Jarvis what's on your mind.", linking to chat) or `ErrorState`.
  - `app/(app)/workspace/[taskId]/page.tsx`: `WoopCard` replaces the inline WOOP block, fed `wish=title`, `obstacle=obstacle_trigger`, `plan=behavioral_response`.
  - `app/(app)/documents/page.tsx`, `app/(app)/habits/page.tsx`: error versus empty.
  - `components/app/IntelligentTrace.tsx:5` and `components/app/ModelModeSelector.tsx:59`: lint errors.

**Interfaces:**
- Consumes: the Task 7 SSE contract and `complete` fields, and the error envelope (`ApiError`, Plan 1 follow-up).
- Produces:
```ts
// lib/learning.ts
export function shouldToast(p: { confidence: number; occurrence_count: number }): boolean        // confidence >= 0.7 && occurrence_count >= 3
export interface LearningMoment { kind: 'memory' | 'pattern'; id: string; content: string;
  provenance: 'said' | 'inferred'; confidence: number; occurrences?: number; action: string }
export function toLearningMoment(e: MemoryExtractedEvent | PatternDetectedEvent, kind: 'memory' | 'pattern'): LearningMoment
export function describeAcceptOutcome(res: { status?: string; task_count?: number }): { ok: boolean; tone: 'success' | 'info' | 'error'; message: string }
export type ListState<T> = { kind: 'loading' } | { kind: 'error'; message: string } | { kind: 'empty' } | { kind: 'ready'; items: T[] }
export function toListState<T>(r: { items?: T[]; error?: unknown }): ListState<T>
// lib/api.ts
export interface StreamHandlers { /* … */ onConsentRequest?: (e: { reason: string; module: string }) => void }
export async function consumeSSE(res: Response, handlers: StreamHandlers, errorLabel: string): Promise<void>
```

**Rules:**
- **Observations.** Every observation shows inline under its reply: "Noted — you said: …" or "I noticed: …". The toast shows only when `shouldToast`.
- **Accept outcomes:**

  | Result | Toast |
  |---|---|
  | `accepted` with `task_count > 0` | success: "Schedule saved — N tasks." (then navigate) |
  | `empty` | info: "Nothing in this plan could be scheduled."; the draft stays open |
  | anything else | error: "Failed to save schedule. Try again." |

- **Lists.** 5xx shows `ErrorState` ("Can't reach Jarvis right now", with Retry). A 200 with `[]` shows `EmptyState`.
- **ConsentNotice.** It shows the reason and module. There is no approve button: the backend has no resume path in this plan.
- **Copy.** Never "calendar"; no blame words.

- [ ] **Step 1: Write failing tests** with vitest, pure:
  - `shouldToast`: `(0.69,3)` → false, `(0.7,2)` → false, `(0.7,3)` → true.
  - `describeAcceptOutcome`:
    - `{status:"accepted",task_count:0}` → `ok:false`
    - `{status:"empty"}` → `ok:false, tone:"info"`
    - `{status:"accepted",task_count:3}` → `ok:true` and the message contains `"3"`
  - `toListState`: `{error}` → `kind:"error"`; `{items:[]}` → `kind:"empty"`.
  - `consumeSSE` over a `ReadableStream` of frames `memory_extracted`, `pattern_detected`, `consent_request`, `complete` calls the handlers in that order.
- [ ] **Step 2: Run** `npm test`. Expected: FAIL, because the modules are missing.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test`, `npx tsc --noEmit`, `npx next lint` (0 errors) and `npm run build`. Expected: all pass.
- [ ] **Step 5: Commit** in jarvis-frontend: `feat: learning moments arrive inline; lists tell an outage from empty; honest accept`.

### Task 15: Frontend — the You page and "While you were away"

**Files:**
- Create:
  - `app/(app)/you/page.tsx`
  - `components/app/you/PatternList.tsx`, `RuleList.tsx`, `MemoryList.tsx`, `FocusChart.tsx`, `AutonomySettings.tsx`, `PrivacyReceipt.tsx`
  - `components/app/WhileYouWereAway.tsx`
  - `lib/you.ts`
  - `lib/__tests__/you.test.ts`
- Modify:
  - `lib/api.ts`: `getYou`, `updateYouSettings`, `listSuggestions`, `acceptSuggestion`, `dismissSuggestion`, `runTrigger`. Each passes `?user_id=` and throws `ApiError`.
  - `lib/types.ts`: the Task 12 and Task 10 response types.
  - `lib/constants.ts`: `NAV_ITEMS` gains `{ id: "you", label: "You", icon: "User", href: "/you" }`.
  - `components/app/NavRail.tsx`: `User` in `iconMap`.
  - `app/(app)/dashboard/page.tsx`: mount `WhileYouWereAway`.

**Interfaces:**
- Consumes: the Task 12 `/you` contract, the Task 10 suggestions API, the Task 5 memories routes, `DELETE /habits/constraints/{id}`, `LearningToast`/`shouldToast` (Task 14).
- Produces:
```ts
export function freshnessLabel(notedAt: string, now: Date): string   // "noted today" | "noted yesterday" | "noted 12 days ago"
export function provenanceLabel(p: 'said' | 'inferred'): string      // "you said" | "I noticed"
export function receiptLine(r: Receipt): string                      // "12 on this device · 3 sent to the cloud · 1 redaction"
export function acceptResultMessage(r: ActionResultOut): string      // tasks_written → "N tasks are on today's plan."; draft_id → r.message
```

**You page** (spec §8.1):
- **Patterns.** Each shows its description and "seen N times", with evidence collapsed. Actions: Confirm → `/memories/{id}/confirm`; Not right → `/dismiss`. Both are disabled when `memory_id` is null.
- **Rules.** Each shows its text and a said/inferred badge. Forget → `DELETE /habits/constraints/{id}`.
- **Memories.** Each shows its content, provenance label and freshness. Actions: Confirm, and Forget with the copy "Forget — Jarvis won't store this again."
- **Focus chart.** An inline SVG with 24 bars, the hour labels and an accessible `<title>`.
  - The caption is "Learned from N outcomes" when learned.
  - Otherwise it is "Not enough history yet — this is a starting guess".
- **Autonomy and wake time.** An autonomy radio group (three modes, plain descriptions) and a wake-time input, saved through `PATCH /you/settings`.
- **Privacy receipt.** Uses `receiptLine`.
- **Removal.** Forget and dismiss remove the item optimistically and roll back on error.

**WhileYouWereAway:**
- **Loading.** It fetches pending suggestions on mount and every 30 s while `document.visibilityState === "visible"`.
- **Card.** Shows title, body and the consent reason (small), with Accept and Dismiss.
- **Accept.** Shows a toast with `acceptResultMessage`, then calls `refreshTasks()`.
- **409.** Shows an info toast with the server message and removes the card.
- **Learning toast.** A new `pattern_rule` suggestion whose `details` carry `confidence ≥ 0.7` and `occurrences ≥ 3` raises `LearningToast`.
- **Empty.** The panel renders nothing when there are no suggestions.

- [ ] **Step 1: Write failing tests** for `freshnessLabel` (0, 1 and 12 days), `provenanceLabel`, `receiptLine` (singular and plural) and `acceptResultMessage` (`tasks_written` and `draft_id` variants).
- [ ] **Step 2: Run** `npm test` → FAIL. **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test`, `npx tsc --noEmit`, `npx next lint` (0 errors) and `npm run build` → pass.
  Manual pass against the backend with the seeded `demo` user:
  - `/you` shows the morning-skip pattern with evidence and an evening focus peak captioned "Learned from …".
  - After `POST /api/v1/anticipation/run {"trigger":"morning_brief"}`, the dashboard shows the brief card; Accept leads to a toast, and tasks appear on `/schedule`.
- [ ] **Step 5: Commit** `feat: the You page shows what Jarvis knows; the dashboard shows what it did while you were away`.

### Task 16: Live E2E — the vision checked against the real stack (orchestrator)

**Files:**
- Modify: `scripts/live_e2e.py`, `tests/test_live_e2e_script.py` (helper tests only), `docs/PROJECT_STATUS.md`

**Interfaces:**
- Consumes every API above.
- Produces new checks, run after step 9. Cleanup is renumbered to 15.

```text
10 forget sticks across re-extraction
   new session: FACT_PROMPT = "Something about me: I'm a night owl and do my best work after 9 pm."
   -> poll client.memories() for /night owl/ -> DELETE it
   -> another new session, FACT_PROMPT again
   -> that turn has no memory_extracted frame matching, client.memories() has none,
      and user_memories has no row with status 'active' matching (DB check)
11 new session knows what happened
   session with "I finished the graphs revision today" + "thanks"; then a new session "Hi again"
   -> complete.since_last_spoke is non-null and starts with "Since we last spoke"
12 morning brief on demand
   POST /api/v1/anticipation/run {"trigger": "morning_brief"}
   -> status in {"created", "already_done"}; when created: suggestion.kind == "morning_brief"
      and details.draft_id is set
13 accept the brief -> tasks written
   POST /anticipation/suggestions/{id}/accept -> 200, result.tasks_written > 0,
   user_tasks rows carry the brief draft's plan; a second accept -> 409
14 task outcome -> event
   POST /tasks/{id}/complete -> a 'completed' task_events row; POST /tasks/{id2}/skip
   -> a 'skipped' row and user_tasks.status == 'skipped'
15 cleanup also asserts proactive_suggestions and task_events are empty for the user
```
- [ ] **Step 1: Apply migrations live.** This needs the linked Supabase project; Plan 1's ledger has the path. Run `supabase db push --include-all` for `20261115000000`…`20261115000004`. Verify:
  - `select extname from pg_extension where extname='vector'` returns one row;
  - `select match_memories('x', array_fill(0, array[384])::extensions.vector, array['active'], null, 1, 0)` returns no error.
- [ ] **Step 2:** Add the helper test `test_matches_fact__night_owl_variants`, then the new checks. Run `pytest tests/test_live_e2e_script.py -q` → PASS.
- [ ] **Step 3:** Start the backend with `CLAUDE_PRIMARY=true` and run `.venv/bin/python scripts/live_e2e.py`. Expected: `ALL PASSED`, and a server log with no tracebacks and no refusals.
- [ ] **Step 4:** Run `scripts/seed_demo_user.py --reset`, then open `/you` and the dashboard (Task 15's manual pass) on the live stack.
- [ ] **Step 5:** Update `docs/PROJECT_STATUS.md`: test count, `task_events` writers, memory lifecycle, anticipation engine, the new endpoints, and the 5 migrations. Commit `test(e2e): the vision core passes against the live stack` in Jarvis-Engine.
