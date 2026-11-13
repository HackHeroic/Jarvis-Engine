# Capstone Plan 1 — Safety Fixes + The Spine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three real bugs and build the layered core (errors, domain, repositories, LLM providers incl. Claude, event bus, composition root) that every later capstone plan stands on.

**Architecture:** New `app/domain` (entities, ABC ports, errors, events — no framework imports), new `app/infrastructure` (Supabase + InMemory repositories, LLM providers), a composition root (`AppContainer`) built in the FastAPI lifespan. Existing modules keep working: old functions become thin delegates to the new classes, so the 509 existing tests stay green at every commit.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, supabase-py, pytest + pytest-asyncio, `anthropic` SDK (new), LiteLLM (existing transport for local + Gemini).

**Spec:** `docs/superpowers/specs/2026-11-14-capstone-lld-and-anticipation-design.md` (§5, §6, §10, §11, §9.1 table only, §12 phases 1 + 2a). This is plan 1 of 4; plans 2–4 (3a vision core, 2b finish restructure, 3b + 4 extras/docs) are written after this one lands.

## Global Constraints

- Branch `capstone-lld`. Commit after every task. Commit trailer: `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.
- The full suite must pass after every task: `.venv/bin/python -m pytest tests/ -q` (baseline: 509 passed). Tests never touch the network (conftest socket guard).
- `app/domain/**` imports nothing from `app.api`, `app.services`, `app.orchestrator`, `app.infrastructure`, `fastapi`, `supabase`, `litellm`, `anthropic`.
- Ports are `abc.ABC` classes with `@abstractmethod` (spec ADR-2). Entities are Pydantic v2 `BaseModel`s with `Field(description=...)`.
- Every repository method takes `user_id: str` as its first argument and filters by it. No exceptions.
- No `str(exc)` in any HTTP response. One error envelope: `{"error": {"code", "message", "details", "request_id"}}`.
- Never copy code from `claude-code-src-code-main`.
- Never run two PRIMARY-role LLM calls concurrently (existing project rule).
- Claude calls: official `anthropic` SDK (`AsyncAnthropic`), model `claude-opus-5-5`, never via LiteLLM. Before writing any Claude code, invoke the `claude-api` skill and read `python/claude-api/README.md`, `python/claude-api/tool-use.md` (structured outputs), `shared/prompt-caching.md`. Never guess SDK names.
- Test names follow `test_{thing}__{condition}__{expected}`.
- Keep old import paths working with re-exports where existing tests patch them.

## Review Focus

1. **Double-click accept:** two accept calls for the same draft → the second gets 409 `invalid_state_transition`, and `user_tasks` holds each task exactly once. (Task 6)
2. **Missing `user_id`:** any route called without a user → 422, never another user's rows. (Task 2)
3. **Hung LLM provider:** a provider that never answers → timeout → next provider answers; the call completes. (Task 8)
4. **Expired / invalid Claude key (the current key expires within a day):** 401 from Anthropic → falls through to Gemini without crashing, and is not retried. (Task 9)
5. **Database down:** a list endpoint during an outage → 503 envelope, not `[]`. (Task 2)

---

### Task 1: Error hierarchy and the single error envelope

**Files:**
- Create: `app/domain/__init__.py`, `app/domain/errors.py`, `app/api/error_handlers.py`
- Modify: `app/main.py` (register handlers; add request-id middleware)
- Test: `tests/test_errors.py`

**Interfaces — Produces:**
```python
# app/domain/errors.py
class JarvisError(Exception):
    code: str = "internal_error"; http_status: int = 500
    def __init__(self, message: str, *, details: dict | None = None): ...
class DomainError(JarvisError): ...
class ValidationError(DomainError):            code="validation_error";          http_status=422
class NotFoundError(DomainError):              code="not_found";                 http_status=404
class ConflictError(DomainError):              code="conflict";                  http_status=409
class InvalidStateTransition(ConflictError):   code="invalid_state_transition"
class StaleVersionError(ConflictError):        code="stale_version"
class ScheduleInfeasibleError(DomainError):    code="schedule_infeasible";       http_status=422
class ConsentRequiredError(DomainError):       code="consent_required";          http_status=202
class InfrastructureError(JarvisError): ...
class PersistenceError(InfrastructureError):   code="persistence_unavailable";   http_status=503
class LLMError(InfrastructureError):           code="llm_error";                 http_status=503
class LLMTimeoutError(LLMError):               code="llm_timeout"
class LLMUnavailableError(LLMError):           code="llm_unavailable"
class LLMOutputParseError(LLMError):           code="llm_output_parse"
# app/api/error_handlers.py
class ErrorBody(BaseModel): code: str; message: str; details: dict | None = None; request_id: str
class ErrorResponse(BaseModel): error: ErrorBody
def register_error_handlers(app: FastAPI) -> None
```
Handlers: `JarvisError` → its `http_status` + envelope; `RequestValidationError` → 422 `validation_error` with field errors in `details`; `HTTPException` → same status, envelope with code `http_error` (keeps existing routes' status codes); bare `Exception` → 500 `internal_error`, message `"Something went wrong on our side."`, real exception logged with the request id. Request id: middleware sets `request.state.request_id = uuid4().hex` and the `X-Request-ID` response header.

- [ ] **Step 1: Write failing tests** in `tests/test_errors.py` using a tiny FastAPI app + `TestClient`:
  - `test_error_envelope__jarvis_error__maps_status_and_code`: route raises `NotFoundError("no draft")` → 404, body `{"error": {"code": "not_found", "message": "no draft", "details": None, "request_id": <32 hex>}}`.
  - `test_error_envelope__unhandled_exception__hides_internals`: route raises `RuntimeError("secret db dsn")` → 500, `"secret"` not in response text, code `internal_error`.
  - `test_error_envelope__validation__422_with_field_details`.
  - `test_error_envelope__http_exception__keeps_status`: `HTTPException(404, "x")` → 404, `error.code == "http_error"`, `error.message == "x"`.
  - `test_errors__hierarchy__subclassing`: `issubclass(InvalidStateTransition, ConflictError)`, `issubclass(LLMTimeoutError, InfrastructureError)`.
- [ ] **Step 2: Run** `pytest tests/test_errors.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** the two modules and wire `register_error_handlers(app)` + the middleware in `app/main.py`. `raise_server_exceptions=False` in the test client for the 500 case.
- [ ] **Step 4: Run** the new tests → PASS; run the full suite. Existing tests that assert on `response.json()["detail"]` will now see the envelope: update those assertions to `["error"]["message"]` (grep `\["detail"\]` in `tests/`).
- [ ] **Step 5: Commit** `feat(errors): JarvisError hierarchy and a single error envelope`.

### Task 2: Close the data-isolation holes (`CurrentUser` seam)

**Files:**
- Create: `app/api/deps.py`
- Modify: `app/api/v1/endpoints/ingestion.py` (list/approve/reject pending-calendar, ~lines 160–245), `app/api/v1/endpoints/habits.py` (`complete_tracker` ~line 26), `app/services/analytical/sm2_engine.py` (`record_completion` ownership check ~line 66–95)
- Test: `tests/test_data_isolation.py`

**Interfaces — Produces:**
```python
# app/api/deps.py
async def current_user(user_id: str = Query(..., min_length=1, max_length=128,
        description="Caller identity. Auth seam: a JWT subject replaces this.")) -> str
CurrentUser = Annotated[str, Depends(current_user)]
def require_supabase(request: Request) -> Any   # raises PersistenceError if no live client
```

- [ ] **Step 1: Write failing tests** (FastAPI `TestClient`, `tests.fakes.FakeDBClient` seeded with pending-calendar rows for users `alice` and `bob`):
  - `test_pending_calendar_list__no_user_id__422`.
  - `test_pending_calendar_list__as_alice__only_alice_rows`.
  - `test_pending_calendar_approve__bob_approves_alice_row__404_and_row_still_pending`.
  - `test_pending_calendar_reject__bob_rejects_alice_row__404_and_row_still_pending`.
  - `test_pending_calendar_list__db_unavailable__503_envelope` (app.state.db_client has `supabase=None`) → 503, `error.code == "persistence_unavailable"` (Review Focus 5).
  - `test_tracker_complete__no_user_id__422`; `test_tracker_complete__wrong_user__404`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** All three pending-calendar routes take `user: CurrentUser` and add `.eq("user_id", user)` to the query/update. `complete_tracker` takes `user: CurrentUser`; `record_completion` makes `user_id` required and treats a non-owner as not found. Replace the `except Exception: return {"pending": []}` and `detail=str(exc)` paths with `raise PersistenceError("Could not reach the database.") from exc`. Use `require_supabase`.
- [ ] **Step 4: Run** new tests + full suite → PASS (update any existing test that called these routes without `user_id`).
- [ ] **Step 5: Commit** `fix(security): pending-calendar and tracker routes are user-scoped`.

### Task 3: Fix the writes the schema rejects

**Files:**
- Modify: `app/api/v1/endpoints/tasks.py` (completion-signal insert ~lines 495–510), `app/services/analytical/missed_deadlines.py` (~line 79), `app/services/analytical/mastery_tracker.py` (~line 135), `app/db/supabase_py.py` (`check_connection`), `app/main.py` (`/health` if it references `user_state`)
- Test: `tests/test_schema_alignment.py`

Decisions: the completion signal row is `{user_id, task_id, signal_type: "completed", metadata: {"actual_duration_minutes": n}, created_at}` (only columns that exist); the insert failure is logged with `logger.warning`, never a bare `except: pass`. `missed_deadlines` writes status `"missed"` (allowed by the 2026-08-13 CLI migration — verify by reading `supabase/migrations/20260813000000_user_tasks_status_missed_pacing.sql`; if `missed` is not in that CHECK, add it in Task 7's migration and note it). `mastery_tracker` drops the `'archived'` filter value. `check_connection` queries `user_tasks` with `limit(1)` (a table every migration set creates).

- [ ] **Step 1: Write failing tests:** `test_complete_task__writes_signal_with_existing_columns_only` (FakeSupabase; assert the inserted row's keys ⊆ `{id,user_id,task_id,signal_type,metadata,created_at}` and `signal_type == "completed"`); `test_check_connection__queries_user_tasks` (FakeSupabase records table names).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** full suite → PASS.
- [ ] **Step 5: Commit** `fix(db): stop writing columns and statuses the schema does not have`.

### Task 4: Domain entities, events and ports

**Files:**
- Create: `app/domain/entities.py`, `app/domain/events.py`, `app/domain/ports.py`
- Test: `tests/test_domain.py`, `tests/test_layering.py`

**Interfaces — Produces:**
```python
# app/domain/entities.py  (all Pydantic BaseModel; enums are str Enums)
class TaskStatus(str, Enum): PENDING="pending"; COMPLETED="completed"; SKIPPED="skipped"; MISSED="missed"
class DraftStatus(str, Enum): PENDING="pending"; MODIFIED="modified"; ACCEPTED="accepted"; REJECTED="rejected"; EXPIRED="expired"; SUPERSEDED="superseded"
class TaskEventType(str, Enum): SCHEDULED="scheduled"; COMPLETED="completed"; SKIPPED="skipped"; MISSED="missed"; RESCHEDULED="rescheduled"; DEADLINE_MISSED="deadline_missed"
class AutonomyMode(str, Enum): SUGGEST_ONLY="suggest_only"; ASK_FIRST="ask_first"; AUTO_LOW_RISK="auto_low_risk"
class SuggestionStatus(str, Enum): PENDING="pending"; ACCEPTED="accepted"; DISMISSED="dismissed"; EXPIRED="expired"; AUTO_APPLIED="auto_applied"
class LLMLocation(str, Enum): LOCAL="local"; CLOUD="cloud"

class UserProfile: user_id: str; display_name: str | None; timezone: str = "Asia/Kolkata"; wake_time: time = time(7, 0); autonomy_mode: AutonomyMode = AutonomyMode.ASK_FIRST
class Goal: id: str; user_id: str; title: str; deadline: datetime | None; status: str = "active"
class Task: id: str | None; user_id: str; task_id: str; goal_id: str | None; plan_id: str | None; title: str; duration_minutes: int (gt=0); difficulty_weight: float (0..1); status: TaskStatus; dependencies: list[str]; scheduled_start: datetime | None; scheduled_end: datetime | None; deadline_hint: str | None
class TaskEvent: id: str | None; user_id: str; task_id: str; event_type: TaskEventType; occurred_at: datetime; scheduled_start: datetime | None; scheduled_end: datetime | None; metadata: dict = {}
class DraftSchedule: id: str; user_id: str; goal_id: str | None; tasks: list[dict]; schedule: dict | None; horizon_start: str | None; status: DraftStatus; expires_at: datetime | None; rejection_reason: str | None
    def transition(self, to: DraftStatus) -> "DraftSchedule"   # raises InvalidStateTransition
class BehavioralConstraint: id: str; user_id: str; raw_text: str; constraint_type: str; recurrence: str | None; structured_semantics: dict | None; source: Literal["said","inferred"] = "said"
class Pattern: detector: str; description: str; occurrences: int; confidence: float; evidence: list[str]
class Suggestion: id: str | None; user_id: str; trigger: str; kind: str; title: str; body: str; action: dict; status: SuggestionStatus; consent_behavior: Literal["allow","ask","deny"]; consent_reason: str; created_at: datetime; expires_at: datetime | None
class LLMUsageEvent: user_id: str | None; session_id: str | None; task: str; role: str; provider: str; model: str; location: LLMLocation; input_tokens: int; output_tokens: int; cache_read_tokens: int = 0; latency_ms: int; pii_redactions: int = 0; created_at: datetime

DRAFT_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
  PENDING:  {MODIFIED, ACCEPTED, REJECTED, EXPIRED, SUPERSEDED},
  MODIFIED: {MODIFIED, ACCEPTED, REJECTED, EXPIRED, SUPERSEDED},
  ACCEPTED: {}, REJECTED: {}, EXPIRED: {}, SUPERSEDED: {} }

# app/domain/events.py
class DomainEvent(BaseModel): occurred_at: datetime; user_id: str | None
class TaskCompleted(DomainEvent): task_id: str
class TaskSkipped(DomainEvent): task_id: str; reason: str | None
class TaskMissed(DomainEvent): task_id: str
class DraftAccepted(DomainEvent): draft_id: str; task_count: int
class PatternDetected(DomainEvent): pattern: Pattern
class LLMCallCompleted(DomainEvent): usage: LLMUsageEvent
class EventListener(ABC):
    @abstractmethod
    async def handle(self, event: DomainEvent) -> None
class EventBus:                       # Observer subject
    def subscribe(self, event_type: type[DomainEvent], listener: EventListener) -> None
    async def publish(self, event: DomainEvent) -> None   # a failing listener is logged, never propagates; others still run
class NullEventBus(EventBus): ...     # publish is a no-op

# app/domain/ports.py  (every method async; every repo is an ABC)
class TaskRepository(ABC):
    list_pending(user_id) -> list[Task]
    get(user_id, task_id) -> Task | None
    list_scheduled_between(user_id, start: datetime, end: datetime) -> list[Task]
    update_status(user_id, task_id, status: TaskStatus, *, expected: TaskStatus | None = None) -> Task   # NotFoundError / StaleVersionError
    reschedule(user_id, task_id, start: datetime, end: datetime) -> Task
class TaskEventRepository(ABC):
    append(user_id, event: TaskEvent) -> TaskEvent
    list_for_user(user_id, *, since: datetime | None = None, types: set[TaskEventType] | None = None) -> list[TaskEvent]
class DraftRepository(ABC):
    create(user_id, *, tasks: list[dict], horizon_start: str | None, goal_id: str | None, schedule: dict | None) -> DraftSchedule
    get(user_id, draft_id) -> DraftSchedule | None
    get_open(user_id) -> DraftSchedule | None          # newest PENDING or MODIFIED
    replace_tasks(user_id, draft_id, tasks: list[dict]) -> DraftSchedule
    set_status(user_id, draft_id, to: DraftStatus, *, reason: str | None = None) -> DraftSchedule   # checks DRAFT_TRANSITIONS, conditional on current status
    accept_atomic(user_id, draft_id, rows: list[dict]) -> int   # one transaction; InvalidStateTransition if not open or expired
class ConstraintRepository(ABC): list_active(user_id) -> list[BehavioralConstraint]; add(user_id, c) -> BehavioralConstraint; delete(user_id, constraint_id) -> bool
class ProfileRepository(ABC): get_or_create(user_id) -> UserProfile; update(user_id, **fields) -> UserProfile; list_user_ids() -> list[str]
class SuggestionRepository(ABC): add(user_id, s) -> Suggestion; list(user_id, *, status: SuggestionStatus | None = None) -> list[Suggestion]; get(user_id, suggestion_id) -> Suggestion | None; set_status(user_id, suggestion_id, to: SuggestionStatus) -> Suggestion; exists_today(user_id, trigger: str, day: date) -> bool
class UsageRepository(ABC): record(event: LLMUsageEvent) -> None; summary(user_id, *, since: datetime | None = None) -> dict   # {"local_calls","cloud_calls","input_tokens","output_tokens","pii_redactions"}

class LLMRequest(BaseModel): task: str; role: Literal["primary","fast","cloud"]; prompt: str; system_prompt: str = ""; conversation_history: list[dict] | None = None; response_schema: type[BaseModel] | None = None (arbitrary_types_allowed); timeout_s: float = 60.0; user_id: str | None = None; session_id: str | None = None
class LLMProvider(ABC):
    name: str; location: LLMLocation
    is_available() -> bool                                  # sync, cheap
    async complete(req: LLMRequest) -> str | BaseModel      # schema instance when req.response_schema is set
    def stream(req: LLMRequest) -> AsyncIterator[tuple[str, str]]   # (event_type, token); response_schema must be None
class RetryPolicy(ABC): should_retry(error: Exception, attempt: int) -> bool; delay(attempt: int) -> float
```

- [ ] **Step 1: Write failing tests** in `tests/test_domain.py`:
  - `test_draft_transition__pending_to_accepted__ok`; `test_draft_transition__accepted_to_accepted__raises_invalid_state_transition`; `test_draft_transition__modified_to_modified__ok`.
  - `test_task__zero_duration__rejected`; `test_task__difficulty_out_of_range__rejected`.
  - `test_ports__are_abstract`: instantiating `TaskRepository()`, `LLMProvider()`, `EventListener()` raises `TypeError`.
  - `test_event_bus__failing_listener__others_still_run`; `test_event_bus__only_subscribed_type_delivered`; `test_null_event_bus__publish__no_error`.
  - `tests/test_layering.py::test_domain__imports_nothing_from_outer_layers`: walk `app/domain/*.py` with `ast`; assert no import starts with `app.api`, `app.services`, `app.orchestrator`, `app.infrastructure`, `app.core`, `fastapi`, `supabase`, `litellm`, `anthropic`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** the three modules. **Step 4:** new tests + full suite → PASS.
- [ ] **Step 5: Commit** `feat(domain): entities, events, and abstract ports`.

### Task 5: Repositories — InMemory and Supabase families with shared contract tests

**Files:**
- Create: `app/infrastructure/__init__.py`, `app/infrastructure/persistence/__init__.py`, `app/infrastructure/persistence/in_memory.py`, `app/infrastructure/persistence/supabase_repos.py`, `app/infrastructure/persistence/factory.py`
- Modify: `tests/fakes.py` (add `.in_()`, `.gte()`, `.lte()`, `.lt()`, `.neq()` filters and `rpc(name, params)` to `FakeSupabase` — see Task 6 for the `accept_draft` rpc fake)
- Test: `tests/test_repository_contracts.py`

**Interfaces — Consumes:** Task 4 ports/entities, Task 1 errors.
**Produces:**
```python
# in_memory.py:  InMemoryTaskRepository, InMemoryTaskEventRepository, InMemoryDraftRepository,
#                InMemoryConstraintRepository, InMemoryProfileRepository, InMemorySuggestionRepository, InMemoryUsageRepository
#                InMemoryStore (shared dict-of-lists the InMemory repos are constructed over, so draft accept can write tasks)
# supabase_repos.py: Supabase* counterparts, each __init__(self, client)  — client is the supabase-py Client (or FakeSupabase)
# factory.py
class Repositories(BaseModel-like dataclass): tasks, task_events, drafts, constraints, profiles, suggestions, usage
class RepositoryFactory:
    @staticmethod def supabase(client) -> Repositories
    @staticmethod def in_memory() -> Repositories
```
Table mapping (Supabase family): tasks → `user_tasks`; task events → `task_events`; drafts → `draft_schedules`; constraints → `behavioral_constraints`; profiles → `user_profiles`; suggestions → `proactive_suggestions`; usage → `llm_usage_events`. Supabase calls are sync: wrap each in `asyncio.to_thread`. Any transport exception → `PersistenceError` (chained). `SupabaseDraftRepository.accept_atomic` calls `client.rpc("accept_draft", {"p_draft_id", "p_user_id", "p_rows"}).execute()`; an error whose message contains `DRAFT_NOT_OPEN` → `InvalidStateTransition`.

- [ ] **Step 1: Write the contract suite** `tests/test_repository_contracts.py`: one `@pytest.fixture(params=["in_memory", "supabase_fake"])` named `repos` returning `Repositories` (the supabase variant is built over `FakeSupabase`). Every test runs against both (this is the Liskov evidence):
  - tasks: `test_tasks__list_pending__only_this_users_pending`; `test_tasks__update_status__expected_mismatch__raises_stale_version`; `test_tasks__update_status__unknown_task__raises_not_found`; `test_tasks__reschedule__sets_times_and_keeps_row`; `test_tasks__list_scheduled_between__window_filter`.
  - events: `test_task_events__append_then_list__ordered_by_time`; `test_task_events__list__filters_type_and_since`; `test_task_events__other_user__invisible`.
  - drafts: `test_drafts__create_get__roundtrip`; `test_drafts__get_open__returns_modified_draft` (regression: `modified` drafts were unfindable); `test_drafts__set_status__illegal__raises_invalid_state_transition`; `test_drafts__other_user_get__none`; `test_drafts__accept_atomic__writes_rows_and_marks_accepted`; `test_drafts__accept_atomic__second_call__raises_invalid_state_transition_and_no_duplicates` (Review Focus 1); `test_drafts__accept_atomic__expired__raises`.
  - profiles: `test_profiles__get_or_create__defaults` (`autonomy_mode == ASK_FIRST`, `wake_time == 07:00`); `test_profiles__update__persists`.
  - suggestions: `test_suggestions__add_list_by_status`; `test_suggestions__exists_today__true_after_add`.
  - usage: `test_usage__summary__counts_local_and_cloud`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** both families and the factory; extend `FakeSupabase`. **Step 4:** contract suite + full suite → PASS.
- [ ] **Step 5: Commit** `feat(persistence): repository layer with InMemory and Supabase families`.

### Task 6: Atomic, idempotent draft accept (State pattern + one transaction)

**Files:**
- Create: `app/services/planning/__init__.py`, `app/services/planning/task_rows.py`, `app/services/draft_service.py`
- Modify: `app/services/analytical/control_policy.py` (`_persist_fused_tasks` ~lines 338–480: extract the row-building into `task_rows.py` and call it), `app/services/draft_actions.py` (`accept_draft_and_persist` delegates to `DraftService`), `app/services/draft_store.py` (`get_pending_draft` also returns `modified`; `accept_draft`/`reject_draft` updates add a status condition), `app/api/v1/endpoints/drafts.py` (accept/reject map `InvalidStateTransition` → 409 via the global handler; remove `status_code=500` on retryable failure), `tests/fakes.py` (`rpc("accept_draft")` fake)
- Test: `tests/test_draft_service.py`

**Interfaces — Produces:**
```python
# app/services/planning/task_rows.py
def build_task_rows(user_id: str, chunks: list, *, schedule: dict | None, horizon_start: str | None) -> tuple[str, list[dict]]   # (plan_id, rows) — pure, no I/O; exact row shape _persist_fused_tasks builds today
# app/services/draft_service.py
class DraftService:
    def __init__(self, drafts: DraftRepository, events: EventBus): ...
    async def accept(self, user_id: str, draft_id: str) -> int          # tasks written; publishes DraftAccepted; InvalidStateTransition on repeat; NotFoundError if absent; ValidationError("Draft holds nothing schedulable.") if no parseable tasks
    async def reject(self, user_id: str, draft_id: str, reason: str | None = None) -> DraftSchedule
```
`_persist_fused_tasks` keeps its signature and behaviour (v1 `/accept-schedule` still uses it) but now calls `build_task_rows`. `accept_draft_and_persist(draft_store, user_id, draft, supabase)` keeps its signature and return contract (`n>0` / `0` / `None`) but: builds a `SupabaseDraftRepository(supabase)`, calls `DraftService.accept`, returns `0` on `ValidationError`, and **re-raises `InvalidStateTransition`** (callers: `drafts.py` lets it become 409; the orchestrator `draft_action` node in `app/orchestrator/graph.py` catches it and replies "That plan is already locked in." without re-persisting).

The fake rpc in `tests/fakes.py` must mirror the SQL in Task 7 exactly: find the draft by id + user; if status not in (`pending`,`modified`) or `expires_at` in the past → raise `Exception("DRAFT_NOT_OPEN")`; delete that user's `user_tasks` rows with status `pending`; insert `p_rows`; set draft status `accepted`; return a result whose `.data` is the row count.

- [ ] **Step 1: Write failing tests** `tests/test_draft_service.py` (InMemory repos):
  - `test_accept__open_draft__persists_and_publishes_draft_accepted`.
  - `test_accept__twice__second_raises_and_tasks_not_duplicated` (Review Focus 1).
  - `test_accept__concurrent_double_click__exactly_one_succeeds`: `asyncio.gather(svc.accept(...), svc.accept(...), return_exceptions=True)` → one int, one `InvalidStateTransition`; task count equals draft task count.
  - `test_accept__missing_draft__raises_not_found`; `test_accept__empty_draft__raises_validation_error`.
  - `test_accept__preserves_completed_tasks`: a completed task for the same user survives accept.
  - `test_reject__accepted_draft__raises_invalid_state_transition`.
  - `test_build_task_rows__schedule_map__sets_wall_clock_times` and `test_build_task_rows__same_output_as_legacy` (feed the same chunks to the old path via FakeSupabase and compare inserted rows minus `plan_id`).
  - `test_accept_draft_and_persist__already_accepted__raises_invalid_state_transition` (legacy entry point).
  - In `tests/test_draft_endpoints.py` add `test_accept_endpoint__second_call__409_envelope`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** InMemory `accept_atomic` uses an `asyncio.Lock` so the concurrent test is meaningful. **Step 4:** new tests + full suite → PASS (tests patching `control_policy._persist_fused_tasks` keep working because the name remains).
- [ ] **Step 5: Commit** `fix(drafts): accept is atomic and idempotent; draft lifecycle is a state machine`.

### Task 7: Schema migration

**Files:**
- Create: `supabase/migrations/20261114000000_baseline_legacy_tables.sql`, `supabase/migrations/20261114000001_capstone_lld.sql`, `docs/capstone/_schema_notes.md`
- Modify: `docs/PROJECT_STATUS.md` (Storage Map + Known Issue 8 marked fixed), `.claude/CLAUDE.md` is gitignored — skip
- Test: `tests/test_migrations.py`

**Baseline file:** the contents of `app/db/migrations/001…008` (the tables the CLI set never created: `behavioral_constraints`, `habit_trackers`, `pending_calendar_updates`, `task_materials`, `user_plan_updates`, `ingested_documents`, base `user_tasks`), each statement made idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`). Its timestamp sorts last-but-one on purpose: on the live DB these are no-ops; on a fresh DB the whole CLI set now builds the full schema. Verify ordering works on a fresh DB by reading the earlier CLI files for references to these tables; if any earlier file `ALTER`s one of them, rename the baseline to `20260101000000_…` instead and say so in `_schema_notes.md`.

**Capstone file, in this order, all idempotent:**
1. `user_profiles(user_id text primary key, display_name text, timezone text not null default 'Asia/Kolkata', wake_time time not null default '07:00', autonomy_mode text not null default 'ask_first' check (autonomy_mode in ('suggest_only','ask_first','auto_low_risk')), created_at timestamptz default now(), updated_at timestamptz default now())`.
2. Cleanup then backfill: delete rows with `user_id is null or user_id = ''` from every user table; `insert into user_profiles(user_id) select distinct user_id from <each user table> on conflict do nothing`.
3. `goals(id text, user_id text references user_profiles on delete cascade, title text not null, deadline timestamptz, status text not null default 'active', created_at timestamptz default now(), primary key (user_id, id))`; backfill from distinct `(user_id, goal_id)` in `user_tasks` and `draft_schedules` with `title = goal_id`.
4. `task_events(id uuid pk default gen_random_uuid(), user_id text not null references user_profiles on delete cascade, task_id text not null, event_type text not null check (event_type in ('scheduled','completed','skipped','missed','rescheduled','deadline_missed')), occurred_at timestamptz not null default now(), scheduled_start timestamptz, scheduled_end timestamptz, metadata jsonb not null default '{}')`; index `(user_id, occurred_at desc)`, `(user_id, task_id)`.
5. `proactive_suggestions(id uuid pk, user_id text not null references user_profiles on delete cascade, trigger text not null, kind text not null, title text not null, body text not null, action jsonb not null default '{}', status text not null default 'pending' check (status in ('pending','accepted','dismissed','expired','auto_applied')), consent_behavior text not null check (consent_behavior in ('allow','ask','deny')), consent_reason text not null default '', created_at timestamptz not null default now(), expires_at timestamptz, resolved_at timestamptz)`; index `(user_id, status, created_at desc)`.
6. `llm_usage_events(id uuid pk, user_id text, session_id text, task text not null, role text not null, provider text not null, model text not null, location text not null check (location in ('local','cloud')), input_tokens int not null default 0, output_tokens int not null default 0, cache_read_tokens int not null default 0, latency_ms int not null default 0, pii_redactions int not null default 0, created_at timestamptz not null default now())`; index `(user_id, created_at desc)`.
7. FKs `user_id → user_profiles(user_id) on delete cascade` on: `user_tasks, draft_schedules, behavioral_constraints, habit_trackers, user_memories, chat_sessions, chat_messages, task_materials, ingested_documents, pending_calendar_updates, user_plan_updates, user_preferences, task_workspaces, task_completion_signals` — each `alter column user_id set not null` first. Use `do $$ … if not exists (select 1 from pg_constraint where conname = …) … $$` guards.
8. `user_tasks`: dedupe `(user_id, task_id)` keeping the newest row, then `unique (user_id, task_id)`; index `(user_id, goal_id)`; CHECKs `duration_minutes > 0`, `difficulty_weight between 0 and 1`, `scheduled_end is null or scheduled_start is null or scheduled_end > scheduled_start`; status CHECK replaced with `('pending','completed','skipped','missed','pacing_pushed')` (`pacing_pushed` stays valid until plan 2 migrates the Skip button). Before adding each CHECK, null-out or delete violating rows and record the rule in `_schema_notes.md`.
9. `user_memories`: CHECK `confidence between 0 and 1`, `strength between 0 and 1`. `habit_trackers`: CHECK `ef >= 1.3`. `draft_schedules`: add `expires_at timestamptz`, status CHECK `('pending','modified','accepted','rejected','expired','superseded')`.
10. `task_materials`: delete orphans, then FK `(user_id, source_id) → ingested_documents(user_id, source_id) on delete cascade`.
11. Drop dead tables: `pending_action_items, extracted_problems, task_completion_criteria, user_calendar_anchors, conversation_messages, conversation_sessions` (`drop table if exists … cascade`). First `grep -rn '"<name>"' app/` for each and keep any that has a live reference (report it).
12. RLS: `alter table … enable row level security` on every user table + policy `owner_all` `using (user_id = auth.uid()::text) with check (user_id = auth.uid()::text)`. (Service role bypasses RLS; this documents intent for the auth seam.)
13. Function:
```sql
create or replace function accept_draft(p_draft_id uuid, p_user_id text, p_rows jsonb)
returns integer language plpgsql as $$
declare v_status text; v_expires timestamptz; v_count integer;
begin
  select status, expires_at into v_status, v_expires
    from draft_schedules where id = p_draft_id and user_id = p_user_id for update;
  if not found then raise exception 'DRAFT_NOT_FOUND'; end if;
  if v_status not in ('pending','modified') or (v_expires is not null and v_expires < now()) then
    raise exception 'DRAFT_NOT_OPEN';
  end if;
  delete from user_tasks where user_id = p_user_id and status = 'pending';
  insert into user_tasks select * from jsonb_populate_recordset(null::user_tasks, p_rows);
  get diagnostics v_count = row_count;
  update draft_schedules set status = 'accepted' where id = p_draft_id;
  return v_count;
end $$;
```
`jsonb_populate_recordset` leaves omitted columns NULL, which would break `id`/`created_at` defaults: have the function insert with an explicit column list built from the keys `build_task_rows` emits (read Task 6's `task_rows.py`), not `select *`. `SupabaseDraftRepository.accept_atomic` maps `DRAFT_NOT_FOUND` → `NotFoundError`.

- [ ] **Step 1: Write failing tests** `tests/test_migrations.py` (text-level, offline): `test_migrations__capstone_file__creates_required_tables` (regex for the 5 new tables + `accept_draft`); `test_migrations__every_user_table__has_rls_enabled`; `test_migrations__statements_are_idempotent` (no bare `create table ` / `create index ` without `if not exists`); `test_migrations__accept_draft_columns__match_build_task_rows` (the column list in the function ⊇ keys of a row from `build_task_rows`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Write** both SQL files and `_schema_notes.md` (table → entity mapping, every cleanup rule, dropped tables). **Step 4:** tests + full suite → PASS.
- [ ] **Step 5: Commit** `feat(db): capstone schema — profiles, goals, events, suggestions, usage, FKs, RLS, atomic accept`.
- [ ] **Step 6 (orchestrator, not a subagent): apply to the live database.** Needs Madhav: confirm the cleanup rules in `_schema_notes.md`, then run via `supabase db push` (needs the DB password / `supabase link`) or paste into the dashboard SQL editor. Verify with a `select accept_draft(...)` on a throwaway draft and `select count(*) from user_profiles`.

### Task 8: LLM provider strategy, decorators, fallback chain

**Files:**
- Create: `app/infrastructure/llm/__init__.py`, `app/infrastructure/llm/local.py`, `app/infrastructure/llm/gemini.py`, `app/infrastructure/llm/retry.py`, `app/infrastructure/llm/decorators.py`, `app/infrastructure/llm/chain.py`, `app/infrastructure/llm/factory.py`
- Modify: `app/core/model_router.py` (`route_llm_call` body delegates to the chain; signature unchanged), `app/models/brain/litellm_conf.py` (pass `timeout=` to every `litellm.acompletion`, ~lines 168, 242, 401), `app/core/config.py` (`LLM_TIMEOUT_SECONDS = 60`, `LLM_STREAM_TIMEOUT_SECONDS = 120`)
- Test: `tests/test_llm_providers.py`

**Interfaces — Consumes:** `LLMProvider`, `LLMRequest`, `RetryPolicy`, `LLMUsageEvent`, `LLMCallCompleted`, `EventBus` (Task 4); LLM errors (Task 1).
**Produces:**
```python
# local.py   class LocalLMStudioProvider(LLMProvider): name="local"; location=LOCAL
#            is_available(): not cfg.GEMINI_PRIMARY and not cfg.CLAUDE_PRIMARY and bool(model for role)
#            complete/stream: adapt litellm_conf.hybrid_route_query(model_override=<role model>)
# gemini.py  class GeminiProvider(LLMProvider): name="gemini"; location=CLOUD; is_available(): bool(cfg.GEMINI_API_KEY); adapts gemini_primary_route
# retry.py   class ExponentialBackoff(RetryPolicy): __init__(base=0.5, cap=8.0, jitter=0.25, max_attempts=3)
#               should_retry: True only for LLMTimeoutError / LLMUnavailableError and attempt < max_attempts
#            class NoRetry(RetryPolicy)
# decorators.py
#   class RetryingProvider(LLMProvider): __init__(inner, policy, sleep=asyncio.sleep)
#   class RedactingProvider(LLMProvider): __init__(inner, hooks)   # runs the PreCloudLLM hook on prompt, system_prompt and every conversation_history content; counts redactions; only wraps CLOUD providers
#   class MeteredProvider(LLMProvider): __init__(inner, bus: EventBus, model_for_role)   # times the call, publishes LLMCallCompleted
#   class TimeoutProvider(LLMProvider): __init__(inner)   # asyncio.wait_for(req.timeout_s) → LLMTimeoutError
# chain.py   class FallbackChainProvider(LLMProvider): __init__(providers: list[LLMProvider])
#               complete: first available provider; on LLMError try the next; all failed → LLMUnavailableError("No language model is reachable right now.")
#               stream: fall over only if the failure happens before the first token; after that re-raise
# factory.py class LLMProviderFactory: @staticmethod build(cfg, hooks, bus) -> FallbackChainProvider
#               order: [local, claude (Task 9, when present), gemini]; each cloud provider wrapped Redacting→Retrying→Timeout→Metered; local wrapped Timeout→Metered
#            def get_chain() / set_chain(chain)   # process-wide accessor used by route_llm_call; tests set a fake
```
Error translation inside the adapters: `asyncio.TimeoutError`/litellm timeout → `LLMTimeoutError`; connection/5xx/429 → `LLMUnavailableError`; Pydantic `ValidationError`/`JSONDecodeError` on structured output → `LLMOutputParseError`; **anything else (e.g. `TypeError`, `KeyError`) propagates unchanged** — a local code bug must not silently ship the prompt to the cloud. `LLMOutputParseError` does fall over to the next provider (today's behaviour).

`route_llm_call` keeps its exact signature and return types. New body: validate `stream`/`response_schema` exclusivity; build `LLMRequest` (role from `MODEL_ROUTING`); cloud preference (`force_cloud` kwarg, `force_cloud_var`, `GEMINI_PRIMARY`, `CLAUDE_PRIMARY`) is expressed by passing `skip_local=True` to the chain (`chain.complete(req, skip_local=...)`); role `CLOUD` always skips local. If no cloud provider is available after local fails → `RuntimeError` with the existing message text (an existing test pins it).

- [ ] **Step 1: Write failing tests** `tests/test_llm_providers.py` with a `FakeProvider(LLMProvider)` (scripted results/exceptions, call counter):
  - `test_chain__first_available_answers__second_not_called`.
  - `test_chain__timeout_on_first__second_answers` (Review Focus 3, using `TimeoutProvider` over a provider that awaits `asyncio.Event().wait()` with `timeout_s=0.05`).
  - `test_chain__parse_error_on_first__falls_over`.
  - `test_chain__programming_error__propagates_and_second_not_called` (`TypeError`).
  - `test_chain__all_fail__raises_llm_unavailable`.
  - `test_chain__skip_local__local_not_called`.
  - `test_chain__stream_fails_before_first_token__falls_over`; `test_chain__stream_fails_midway__raises`.
  - `test_retrying__transient_then_success__retries_with_backoff` (inject a fake `sleep`, assert delays grow and are ≤ cap·(1+jitter)); `test_retrying__parse_error__not_retried`; `test_retrying__gives_up_after_max_attempts`.
  - `test_redacting__email_in_history_and_system_prompt__redacted` (extends today's prompt-only gate); `test_redacting__counts_redactions`.
  - `test_metered__publishes_llm_call_completed_with_location_and_latency`.
  - `test_route_llm_call__signature_unchanged` (`inspect.signature` equals the current parameter list) and `test_route_llm_call__delegates_to_chain` (set a fake chain).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** new tests + full suite → PASS. `tests/test_model_router.py` patches `litellm_conf` functions — the adapters call those same functions by attribute lookup at call time (`_llm.hybrid_route_query(...)`), so the patches still bite.
- [ ] **Step 5: Commit** `feat(llm): provider strategy with timeout, retry, redaction, metering and fallback chain`.

### Task 9: `ClaudeProvider` — Claude as the cloud tier, built for low token spend

**Files:**
- Create: `app/infrastructure/llm/claude.py`
- Modify: `app/core/config.py` (`ANTHROPIC_API_KEY`, `CLAUDE_MODEL = "claude-opus-5-5"`, `CLAUDE_PRIMARY` bool env, `CLAUDE_EFFORT_BY_ROLE = {"fast": "low", "primary": "medium", "cloud": "medium"}`), `app/infrastructure/llm/factory.py` (insert Claude between local and Gemini when the key is set), `app/main.py` (startup log line: which providers are live; `CLAUDE_PRIMARY` honoured), `requirements.txt` + `pyproject.toml` (`anthropic>=1.0`), `.env.example` if present
- Test: `tests/test_claude_provider.py`, `scripts/smoke_claude.py`

**First step for the implementer:** invoke the `claude-api` skill; read `python/claude-api/README.md`, `python/claude-api/tool-use.md` (structured outputs section), `python/claude-api/streaming.md`, `shared/prompt-caching.md`, `shared/error-codes.md`. Use only names documented there.

**Decisions (token efficiency is a hard requirement from Madhav):**
- One module-level `AsyncAnthropic` client (`max_retries=0` — our `RetryingProvider` owns retries so calls are never double-retried; `timeout=req.timeout_s`).
- `model="claude-opus-5-5"`; `output_config={"effort": CLAUDE_EFFORT_BY_ROLE[req.role]}`; do not send `thinking`, `temperature`, `top_p`, `budget_tokens`, or forced `tool_choice` (all 400 on this model).
- `max_tokens`: 1024 for role `fast` with a schema, 4096 for role `fast` without, 8000 for `primary`/`cloud`. On `stop_reason == "max_tokens"` raise `LLMOutputParseError` (truncated JSON must not be parsed).
- **Prompt caching:** `system` is sent as a single text block with `cache_control: {"type": "ephemeral"}`; the system prompt must be byte-stable — nothing volatile (dates, user names, memory context) goes in it; volatile context goes in the user message. Add `usage.cache_read_input_tokens` to `LLMUsageEvent.cache_read_tokens`.
- **Structured output:** when `req.response_schema` is set, use the SDK's documented structured-output path (`output_config.format` / `messages.parse`) so the reply is schema-valid by construction; return the schema instance. No fence-stripping, no repair loop.
- **Refusal fallback:** enabled by default with the server-side `fallbacks` default form and its beta flag exactly as the skill documents; if `stop_reason == "refusal"` after that → `LLMUnavailableError` (chain moves to Gemini).
- **Streaming:** `stream()` yields `("token", text)` for text deltas only (matches what `hybrid_route_query(stream=True)` yields — check its event names and mirror them).
- **Error mapping** (most specific first, per `shared/error-codes.md`): `APITimeoutError` → `LLMTimeoutError`; `RateLimitError`, `APIConnectionError`, status ≥ 500 → `LLMUnavailableError`; `AuthenticationError` / `PermissionDeniedError` → `LLMUnavailableError` **and** flip an internal `_disabled = True` so `is_available()` returns False for the rest of the process (an expired key must cost one failed call, not one per turn — Review Focus 4); `BadRequestError` → propagate (it is our bug).
- `is_available()`: `bool(cfg.ANTHROPIC_API_KEY) and not self._disabled`.
- Conversation history: map existing `{"role","content"}` dicts straight through; drop any `system`-role entries into the system block only if they are static, otherwise prepend to the user message.

- [ ] **Step 1: Write failing tests** `tests/test_claude_provider.py` — inject a fake client object (constructor arg `client=`) recording kwargs; no network:
  - `test_claude__fast_role__low_effort_and_small_max_tokens`.
  - `test_claude__system_prompt__sent_as_cached_block`.
  - `test_claude__forbidden_params__never_sent` (`thinking`, `temperature`, `tool_choice` absent from kwargs).
  - `test_claude__schema_request__returns_schema_instance`.
  - `test_claude__max_tokens_stop__raises_output_parse_error`.
  - `test_claude__auth_error__disables_provider_and_raises_unavailable` then `is_available() is False` (Review Focus 4).
  - `test_claude__rate_limit__raises_unavailable_and_stays_enabled`.
  - `test_claude__refusal__raises_unavailable`.
  - `test_claude__usage__reports_cache_read_tokens`.
  - `test_factory__with_anthropic_key__order_is_local_claude_gemini`; `test_factory__claude_primary__local_skipped`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** tests + full suite → PASS.
- [ ] **Step 5: Live smoke (costs well under one cent):** `scripts/smoke_claude.py` loads `.env`, sends `"Reply with the single word: ready"` at effort `low`, `max_tokens=16`, then one schema'd call (`class Ping(BaseModel): ok: bool`), prints provider, model, token usage and latency. Run `.venv/bin/python scripts/smoke_claude.py`. Expected: two successful calls. If the key is rejected as an API key, retry it as `ANTHROPIC_AUTH_TOKEN` (the key in `.env` has an unusual `sk-ant-usr-` prefix); report which works and make `config.py` read both.
- [ ] **Step 6: Commit** `feat(llm): ClaudeProvider — cached, low-effort, schema-valid cloud tier`.

### Task 10: Usage ledger, composition root, and wiring

**Files:**
- Create: `app/services/usage_ledger.py`, `app/container.py`
- Modify: `app/main.py` (lifespan builds `AppContainer`, stores it at `app.state.container`; replaces `_StubDBClient` use for the new repos with the InMemory family when Supabase is down — the legacy `app.state.db_client` stays for un-migrated routes), `app/core/runtime.py` (add `set_container` / `get_container`), `app/api/deps.py` (`get_container(request) -> AppContainer`), `app/api/v1/endpoints/drafts.py` (accept/reject use `container.draft_service`)
- Test: `tests/test_container.py`, `tests/test_usage_ledger.py`

**Interfaces — Produces:**
```python
# app/services/usage_ledger.py
class UsageLedger(EventListener):
    def __init__(self, usage: UsageRepository): ...
    async def handle(self, event: DomainEvent) -> None      # LLMCallCompleted → usage.record
    async def receipt(self, user_id: str, since: datetime | None = None) -> dict   # {"on_device_calls","cloud_calls","pii_redactions","input_tokens","output_tokens"}
# app/container.py
@dataclass
class AppContainer:
    repos: Repositories; events: EventBus; llm: FallbackChainProvider
    draft_service: DraftService; usage_ledger: UsageLedger
    @classmethod
    def build(cls, *, supabase_client: Any | None, cfg, hooks) -> "AppContainer"   # supabase family if client else in-memory; subscribes UsageLedger to LLMCallCompleted; calls llm.factory.set_chain
```
User/session attribution for usage: a `ContextVar` pair `current_user_var` / `current_session_var` in `app/core/model_router.py`, set by the chat stream endpoint next to where `force_cloud_var` is set today, read when building `LLMRequest`.

Also in this task — **one turn at a time per conversation** (spec §10b): `app/services/turn_locks.py` with `def thread_lock(thread_id: str) -> asyncio.Lock` (a process-wide dict of locks); the v2 chat stream handler in `app/api/v1/endpoints/chat.py` holds `thread_lock(make_thread_id(user_id, session_id))` around the graph pump, so two tabs on one session run their turns in order instead of overwriting each other's checkpoint.

- [ ] **Step 1: Write failing tests:** `test_thread_lock__same_thread__second_turn_waits_for_first` and `test_thread_lock__different_threads__run_concurrently` (in `tests/test_turn_locks.py`); `test_container__no_supabase__uses_in_memory_family`; `test_container__with_fake_supabase__uses_supabase_family`; `test_container__wires_usage_ledger_to_bus` (publish `LLMCallCompleted` → `repos.usage.summary` reflects it); `test_usage_ledger__receipt__splits_local_and_cloud`; `test_layering__services_and_orchestrator__do_not_import_infrastructure` in `tests/test_layering.py` (ast walk of `app/services/draft_service.py`, `app/services/usage_ledger.py`, `app/domain/` — the allow-list of files that must be clean; it grows in later plans); `test_layering__only_container_builds_repositories` (grep: `RepositoryFactory` referenced only in `app/container.py` and tests).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** tests + full suite → PASS.
- [ ] **Step 5: Start the server once** (`uvicorn app.main:app --port 8000`, then stop): startup log names the live providers and the repository family; `GET /health` → 200.
- [ ] **Step 6: Commit** `feat(core): AppContainer composition root and usage ledger`.

### Task 11: Live verification of Plan 1 (orchestrator)

- [ ] Full suite green; count reported.
- [ ] Migration applied (Task 7 Step 6) and `scripts/smoke_claude.py` passes.
- [ ] One live turn through `/api/v1/chat/v2/stream` with `CLAUDE_PRIMARY=true`: brain dump → draft → accept → second accept returns 409 → `llm_usage_events` has rows with `provider = 'claude'` and non-zero `cache_read_tokens` on the second turn.
- [ ] Update `docs/PROJECT_STATUS.md` (test count, provider chain, new layers) and commit.
