# Ravi: API design, database schema, errors and concurrency

You present slide 14 (API design), slide 15 (database schema) and slide 16 (errors, edge cases and concurrency).
Your two design patterns are **Repository** and **Adapter**.
Everything here comes from docs 05, 07, 08, 09 and 00, and every name was checked in the code.

## Read in this order

| # | What | Sections | Minutes |
|---|---|---|---|
| 1 | This file | all | 10 |
| 2 | `docs/capstone/00-team-study-guide.md` | §5: D5, D6, D7, D11, D12, D27 first, then D3, D14, D24, D25, D26. §10: C7, C8, C9 | 15 |
| 3 | `docs/capstone/07-api-design.md` | §2.1, §2.2, §2.5, §2.6, §3 | 8 |
| 4 | `docs/capstone/08-database-schema.md` | §3.3, §3.4, §3.5, §5, §6.1, §6.2, §10 | 10 |
| 5 | `docs/capstone/09-error-handling.md` | §1, §4.1, §4.2 | 7 |
| 6 | `docs/capstone/05-design-patterns.md` | §0 (Repository, Adapter), §2, §11 | 5 |

Total: about 55 minutes.

## What to say

**Slide 14: API design.**
Jarvis has exactly one way to talk to it: `POST /api/v1/chat/stream`.
It answers with server-sent events (SSE): one normal HTTP response that sends small messages, called frames, one after another.
There are ten frame types, for example `phase`, `message`, `memory_extracted` and `complete`, and each one is a Pydantic model in `app/schemas/chat_stream.py`.
A golden test records eleven real turns and fails if one byte of the output changes by accident.
Everything else is plain REST: drafts, tasks, memories, the You page, anticipation and documents, about fifty operations, each with a typed response model.
A state change is a `POST` to a verb, like `/drafts/{id}/accept`, because accepting is a move in a state machine, not a field edit.
Every error, over HTTP or inside the stream, has one shape: `code`, `message`, `details`, `request_id`.
Bad input is 422, a state conflict is 409, a service that is down is 503, and exception text never reaches the client.

**Slide 15: Database schema.**
The data lives in one Supabase Postgres database with 19 tables.
`user_profiles` is the root: every other table has a `user_id` that is a foreign key to it, and every table has row-level security.
Tasks use two tables on purpose: `user_tasks` holds only the current state, and `task_events` is an append-only history.
Learning (PEARL, the focus profile, the streak) reads only the history, never the status column.
The key piece is the SQL function `accept_draft()`, which runs as one transaction.
It locks the user's profile row, then the draft, checks the draft is still open, replaces the pending tasks, marks the draft accepted and supersedes the user's other open drafts.
Memories carry a 384-number pgvector embedding, and `match_memories()` finds the closest ones for one user in one query.

**Slide 16: Errors, edge cases and concurrency.**
All errors come from one class tree rooted at `JarvisError`; each class carries its own code and HTTP status.
The headline case is a double click on Accept.
Both clicks pass the quick check in Python, because the draft is still pending.
Inside Postgres the first call locks the profile row, and the second call waits.
The first commits; the second then sees `accepted`, raises `DRAFT_NOT_OPEN`, and the API answers 409.
The tasks are written once, and a concurrency test pins this.
Other edges: a stale `expected_version` gives 409 `stale_version` with the latest draft, bad model output goes to the next provider, two tabs on one chat take turns, and a database outage is a 503, never an empty list.

## API: the points to know

| Point | Where in code | Why |
|---|---|---|
| One chat endpoint with typed SSE frames | `app/api/v1/endpoints/chat.py` (routing only, calls `ChatTurnService.open_turn`); `app/schemas/chat_stream.py` (`SseEvent`, `SSE_EVENTS`, `SseEncoder.frame`) | One turn path, one frame catalogue, one place to test. Before, there were two chat architectures side by side (ADR-9). |
| One error envelope | `app/api/error_handlers.py` (`register_error_handlers`, `RequestIdMiddleware`); `app/schemas/errors.py` (`ErrorResponse`) | The client parses one shape. `request_id` matches the `X-Request-ID` header, so a client error can be found in the server log. |
| State changes are `POST` to a verb | `app/api/v1/endpoints/drafts.py` (the route function is also called `accept_draft`) | Accept has rules: an atomic write and superseding other drafts. A `PATCH` of `status` would hide those rules. |
| Status codes of accept | `DraftService.accept` and `check_version` in `app/services/draft_service.py`; `accept_on_time` in `app/services/draft_actions.py` | Each failure has its own code, so the UI can react correctly (list below). |
| 404, not 403, for another user's row | Every repository method takes `user_id` first (`app/domain/ports.py`) | A 403 would confirm the row exists. Another user's data must look exactly like missing data. |
| Request limits | `ChatStreamRequest` (`user_prompt` at most 8,000 chars, `MAX_CHAT_PROMPT_CHARS`); `within_upload_limit` in `app/schemas/upload.py` (10 MB decoded); `BodyLimitMiddleware` in `app/api/request_limits.py` (413 at about 14 MB); `CurrentUser` in `app/api/deps.py` (1 to 128 chars) | A huge request is refused before it can start a turn, spend a model call or use memory. The input is never echoed back. |

**Status codes of `POST /api/v1/drafts/{id}/accept`:**
- **200** `status: "accepted"`, with `task_count`. Also 200 `status: "empty"` when nothing could be scheduled, and `moved: true` when passed times were re-solved from now.
- **404** `not_found`: no such draft, or another user's draft.
- **409** `invalid_state_transition`: the draft is closed (second accept, rejected, superseded, expired).
- **409** `stale_version`: the draft changed since the read named by `expected_version`; `details.draft` holds the latest draft.
- **409** `draft_outdated`: every slot in the plan has already started.
- **409** `conflict`: the plan reuses the id of a task the user already finished.
- **422** `schedule_infeasible`: a late accept no longer fits when re-solved from now; the draft stays open.
- **503** `llm_unavailable` (habits could not be read for the re-solve) or `persistence_unavailable` (database down; the draft stays open).

## Database: the points to know

| Point | Where | Why |
|---|---|---|
| Main tables: `user_profiles` (root, PK `user_id`), `goals` (PK `(user_id, id)`), `user_tasks` (unique `(user_id, task_id)`), `task_events`, `draft_schedules`, `user_memories`, `proactive_suggestions`, `llm_usage_events` | `supabase/migrations/20261114000001_capstone_lld.sql`; doc 08 §2 and §3 | Every one of the other 18 tables has a `user_id` foreign key to `user_profiles`. One owner for every row makes isolation simple. |
| `task_events` (append-only) next to `user_tasks` (current state) | `task_events` is created in `20261114000001_capstone_lld.sql`; doc 08 §3.4; `TaskService` in `app/services/task_service.py` | Re-slotting a task overwrites its status, which would erase the evidence PEARL learns from. Each outcome is a new row instead (ADR-11). |
| No foreign key from `task_events.task_id` to `user_tasks` | doc 08 §5 | `accept_draft()` deletes and re-inserts pending tasks. A foreign key would either delete the history or block the accept. The history must outlive the row. |
| `accept_draft()`: one transaction, lock order user then draft | `supabase/migrations/20261115000007_user_tasks_not_before.sql` (search for `for no key update`, then `for update`); doc 08 §6.1 | One accept per user at a time. User first, because the function also updates the user's other drafts; draft first could deadlock. |
| RLS on every table, and `user_id` on every query | policy `owner_all` in `20261114000001_capstone_lld.sql`; doc 08 §1 | The backend uses the service-role key, which skips RLS. So the `user_id` filter in each repository is the real guard. RLS records the intent and locks out the `anon` role. |
| pgvector `match_memories()` | `supabase/migrations/20261115000002_memory_lifecycle_and_vectors.sql`; called by `SupabaseMemoryRepository.nearest` | One query filters by user, status and type and orders by cosine distance. Forgotten memories never come back, because there is no second store to keep in step. |
| Deliberate departures from 3NF (third normal form: no repeated or derived data) | doc 08 §10 | `user_id` repeated on child rows (`chat_messages`, `task_materials`) for RLS and the `user_id` rule. JSON columns (`draft_schedules.tasks` and `.schedule`, `task_events.metadata`, `proactive_suggestions.action`) for data read and written whole. Arrays like `user_tasks.topic_keywords`. A stored derived value: memory `strength` with `strength_as_of`. |

**The `accept_draft()` steps, in order:** (1) lock the `user_profiles` row with `FOR NO KEY UPDATE`; (2) lock the draft row with `FOR UPDATE`; (3) not found gives `DRAFT_NOT_FOUND`, not `pending`/`modified` or expired gives `DRAFT_NOT_OPEN`; (4) delete the pending tasks and the missed tasks this plan re-slots; (5) insert the new rows, with `user_id` forced to the caller; (6) mark this draft `accepted`; (7) mark the user's other open drafts `superseded`; (8) return the count.
Why `NO KEY UPDATE`: inserts into child tables take a `KEY SHARE` lock on the profile row, and `NO KEY UPDATE` does not block them.
Why a row lock and not an advisory lock: a hashed advisory key can collide across users.

## Errors and concurrency: the points to know

**The `JarvisError` hierarchy** (`app/domain/errors.py`):

| Class | Code | HTTP |
|---|---|---|
| `JarvisError` (base) | `internal_error` | 500 |
| `ValidationError` (a `DomainError`) | `validation_error` | 422 |
| `PayloadTooLargeError` (child of `ValidationError`) | `payload_too_large` | 413 |
| `NotFoundError` | `not_found` | 404 |
| `ConflictError`, and its children `InvalidStateTransition`, `StaleVersionError`, `DraftOutdatedError` | `conflict`, `invalid_state_transition`, `stale_version`, `draft_outdated` | 409 |
| `ScheduleInfeasibleError` | `schedule_infeasible` | 422 |
| `PersistenceError`, `EmbeddingUnavailableError`, `DocumentReadTimeoutError` (each an `InfrastructureError`) | `persistence_unavailable`, `embedding_unavailable`, `document_read_timeout` | 503 |
| `LLMError` and its children `LLMTimeoutError`, `LLMUnavailableError`, `LLMOutputParseError`, `LLMRefusedError` | `llm_error`, `llm_timeout`, `llm_unavailable`, `llm_output_parse`, `llm_refused` | 503 |

Any other exception becomes 500 `internal_error` with a fixed message, and the cause is logged with the request id (`_handle_unhandled`). No route raises `HTTPException`.

**The double click on Accept, step by step:**
1. Click 1 and click 2 both reach `DraftService.accept`.
2. Both pass the quick check `DraftSchedule.transition`, because the draft is still `pending`.
3. Both call `accept_draft()` in Postgres through `SupabaseDraftRepository.accept_atomic`.
4. Call 1 locks the profile row, then the draft row. Call 2 waits on the profile row.
5. Call 1 writes the tasks, marks the draft accepted, supersedes other drafts, and commits.
6. Call 2 gets the lock, reads status `accepted`, and raises `DRAFT_NOT_OPEN`.
7. `accept_atomic` maps it to `InvalidStateTransition`. Click 1 gets 200; click 2 gets 409. Tasks are written once.

If the second click comes after the first has finished, the quick check stops it in Python first: still 409.
Tests: `tests/test_draft_service.py::test_accept__concurrent_double_click__exactly_one_succeeds`, `tests/test_draft_endpoints.py::test_accept_endpoint__second_call__409_envelope`.

**Compare-and-set (CAS) writes.** A CAS write is "update this row only if its status is still one I expect", so the check and the write are one database step.
Tasks use `update_status(..., expected=...)` in `SupabaseTaskRepository`; suggestions and memories do the same.
The allowed "from" states come from the same transition tables the domain uses.
A lost race raises `StaleVersionError`. `TaskService._move` re-reads and retries up to 3 times while the move is still legal.
Example: a double click on Complete gives 200 twice but only one event (`tests/test_task_service.py::test_complete__double_click__second_returns_none_and_one_event`).

**The stale version 409.** `DraftSchedule.version` is the first 16 hex characters of a SHA-256 of the draft's tasks and schedule. It is computed, not stored.
`GET /drafts/{id}` returns it, and the chat `complete` frame carries it as `draft_version`.
The client sends it back as `expected_version`. If the draft was edited since, `check_version` raises `StaleVersionError`: 409 `stale_version`, the latest draft in `details.draft`, nothing written.

**When the database or a model is down.**
- Database: any transport failure becomes `PersistenceError` in `_SupabaseRepository._run`, so 503 `persistence_unavailable`, never an empty list or a fake 404. `require_supabase` checks first.
- `ConnectRetryTransport` (`app/db/supabase_py.py`) retries once, but only when the request provably did no work: a failed connect, or a dropped connection on a read. A write is never retried.
- No database at startup: the container uses the in-memory repositories, but routes that need data still answer 503.
- Model: a timeout or an unreachable cloud model is retried with backoff, then the chain moves to the next provider. Bad output (`LLMOutputParseError`) is not retried; the next provider is tried.
- Every provider fails: REST answers 503 `llm_unavailable`; chat gives an honest degraded reply, never raw model output.

## Your two patterns: Repository and Adapter

**Repository**
- **The problem.** Before the capstone, endpoints wrote database queries directly. Some forgot the `user_id` filter, so one user could read another user's data. Tests also needed a real database.
- **The pattern in one line.** A class that hides the database behind plain methods like `get`, `add`, `list_pending`.
- **Everyday comparison.** A librarian. You ask for a book by name; you never walk into the storage room.
- **Where.** Ports (abstract classes) in `app/domain/ports.py`: `TaskRepository`, `DraftRepository`, `MemoryRepository` and more. Two families: `SupabaseTaskRepository` and friends in `app/infrastructure/persistence/supabase_repos.py`, `InMemoryTaskRepository` and friends in `in_memory.py`. `RepositoryFactory.supabase` and `RepositoryFactory.in_memory` in `factory.py` build a whole family at once.
- **SOLID: Dependency Inversion.** Services depend on the port, not on Supabase. `TaskService` receives its repositories in its constructor, and only the container builds them (`tests/test_layering.py::test_layering__only_container_builds_repositories`).
- **SOLID: Liskov substitution.** Either family can replace the other. This is shown, not claimed: one contract suite runs every test against both families (`tests/test_repository_contracts.py`, fixture `params=["in_memory", "supabase_fake"]`).
- **Why not the simpler way.** Querying the database in each service means every service knows Supabase, tests cannot run offline, and the `user_id` filter is easy to forget. Active Record (`task.save()`) would make the entity import the driver. A generic `Repository[T]` with `update` would allow writes the domain forbids (tasks only change status; task events are append-only).
- **What to admit.** The Supabase family is tested offline against `FakeSupabase` (`tests/fakes.py`), not a real database. Six files still make raw table calls (sessions, pending calendar, workspace cache, user preferences). `TaskRepository` mixes reads and writes.

**Adapter**
- **The problem.** Outside libraries (supabase-py, the Anthropic SDK, LiteLLM, the sentence model) have their own shapes and error types. Our ports must not change to fit them.
- **The pattern in one line.** A class that makes someone else's library fit the interface we want.
- **Everyday comparison.** A travel plug adapter. The socket does not change, your charger does not change; the adapter sits between them.
- **Where.** `SupabaseTaskRepository` and the other `Supabase*Repository` classes adapt supabase-py to our repository ports; `_SupabaseRepository._run` runs the call on a worker thread and turns any failure into `PersistenceError`. `ClaudeProvider` (`app/infrastructure/llm/claude.py`) adapts the Anthropic SDK to `LLMProvider`. `MiniLMEmbedder` (`app/infrastructure/embedding.py`) adapts the sentence model to `TextEmbedder` and raises `EmbeddingUnavailableError`.
- **SOLID: Dependency Inversion.** The domain owns the interface (`LLMProvider`, `TextEmbedder` and the repositories, all in `app/domain/ports.py`); the library is made to fit it, not the other way round.
- **SOLID: Open/Closed.** A new model vendor or a new store is a new adapter class for an existing port; nothing above the port changes.
- **Why not the simpler way.** Calling supabase-py or LiteLLM from services was the old shape. Library errors would leak upward, and `tests/test_layering.py::test_layering__services__do_not_import_the_supabase_driver` would fail.
- **What to admit.** The raw-table leftovers above skip the adapter. The workspace still makes one blocking Supabase read on the event loop.

## Where the code is

| Idea | File | What to point at |
|---|---|---|
| The chat endpoint | `app/api/v1/endpoints/chat.py` | A short router that only parses and calls `ChatTurnService.open_turn` |
| Frames | `app/schemas/chat_stream.py` | `SseEvent` (10 values), `ChatStreamRequest`, `ChatCompletePayload`, `SseEncoder.frame` |
| Error tree | `app/domain/errors.py` | `code` and `http_status` on each class |
| Envelope | `app/api/error_handlers.py`, `app/schemas/errors.py` | `register_error_handlers`, `_handle_unhandled`, `ErrorResponse` |
| Identity seam | `app/api/deps.py` | `CurrentUser`, `require_supabase` |
| Request limits | `app/api/request_limits.py`, `app/schemas/upload.py` | `BodyLimitMiddleware`, `within_upload_limit` |
| Accept logic | `app/services/draft_service.py` | `DraftService.accept`, `check_version` |
| Atomic accept in SQL | `supabase/migrations/20261115000007_user_tasks_not_before.sql` | `for no key update`, then `for update`, then `DRAFT_NOT_OPEN` |
| SQL error to domain error | `app/infrastructure/persistence/supabase_repos.py` | `SupabaseDraftRepository.accept_atomic` |
| CAS write | `app/infrastructure/persistence/supabase_repos.py` | `SupabaseTaskRepository.update_status` with `expected` |
| Ports | `app/domain/ports.py` | `TaskRepository`, `DraftRepository`, `LLMProvider`, `TextEmbedder` |
| Two families | `app/infrastructure/persistence/factory.py` | `RepositoryFactory.supabase`, `RepositoryFactory.in_memory` |
| Contract suite | `tests/test_repository_contracts.py` | the `repos` fixture; `test_drafts__accept_atomic__concurrent_calls__exactly_one_succeeds` |
| Tables and RLS | `supabase/migrations/20261114000001_capstone_lld.sql` | `task_events` table, `owner_all` policy |
| Vector search | `supabase/migrations/20261115000002_memory_lifecycle_and_vectors.sql` | `match_memories` |

## Questions you may get

1. **Why SSE and not WebSockets?** No reason is written down; this is our reading. A turn is one request followed by a one-way stream of frames, which is exactly what SSE over a normal `POST` gives. WebSockets add two-way messaging we do not need, and errors before the stream starts can use the same JSON envelope.
2. **Why only one chat endpoint?** Two chat architectures side by side were the biggest design debt (ADR-9). One endpoint means one turn path, one frame catalogue and one place to test.
3. **Why is accept a `POST /drafts/{id}/accept`, not a `PATCH` of the status?** Accept is a move in a state machine with rules: an atomic write and superseding other drafts. It is not a field update, so each move is its own verb.
4. **Why 404 and not 403 for another user's row?** A 403 would confirm that the row exists. Another user's data must look exactly like missing data.
5. **Why not just a status column for tasks?** The status is only the current state. Re-slotting a missed task changes it, which would erase the evidence PEARL learns from. `task_events` keeps every outcome (ADR-11).
6. **Why pgvector in Postgres, not a separate vector database?** Forget and dismiss must stick. In Postgres one query filters by user, status and type and orders by similarity. A separate store would have to stay in step with every forget (ADR-14). ChromaDB is still used, only for document chunks.
7. **Why JSON columns in `draft_schedules`?** A draft is a proposal that is reviewed and replaced as a whole. It becomes real rows in `user_tasks` only on accept.
8. **What happens if two accepts arrive together?** Both pass the quick check in Python. In Postgres the first call locks the profile row and the draft; the second waits. The second then reads `accepted`, raises `DRAFT_NOT_OPEN`, and gets a 409; tasks are written once.
9. **Why lock the profile row before the draft row?** The function also updates the user's other drafts, so locking the draft first could let two accepts deadlock. It also serialises accepts of two different drafts of one user. A row lock cannot collide across users the way a hashed advisory lock can.
10. **What if the model returns bad JSON?** Claude uses structured outputs, so its replies fit the schema. A local reply is cleaned and checked by Pydantic; a failure is `LLMOutputParseError`, not retried, and the next provider is tried. If all fail: REST gives 503, chat gives an honest degraded reply.
11. **What if the network drops?** Database: a failed connect, or a dropped connection on a read, is retried once; a write never is; then 503 `persistence_unavailable`. Model: a timeout is retried, then the next provider is tried. Browser: the cleanup is shielded, so the turn lock is freed at once; the user's message is saved, the reply is not.
12. **Which SOLID principle does Repository use?** Dependency Inversion: services depend on the abstract port, and only the container picks Supabase or in-memory. It also shows Liskov: both families pass the same contract suite.

## Say these honestly

1. **No real authentication.** `CurrentUser` reads `user_id` from the query string. It is the seam a JWT would replace. Some routes still take `user_id` in the body or as a plain query parameter.
2. **The version check has a window.** It runs in the service just before the SQL call, so an edit that lands between them is not caught. An accept sent without `expected_version` is not checked at all.
3. **The Supabase family is tested offline against a fake.** `FakeSupabase` mirrors the `accept_draft` rules. The real function runs only in the live check, `scripts/live_e2e.py`.
4. **RLS is on, but the backend bypasses it.** The service-role key skips RLS, so the `user_id` filter in each repository is the real guard.
5. **One server process.** Turn locks and similar guards live in memory. Data stays correct with two processes because the accept transaction, CAS writes and unique keys are in the database.

If asked for numbers: the slides say 48 operations on 42 paths and 19 migrations. The code today has 49 operations on 43 paths (`GET /documents/{source_id}/file` was added later) and 20 files in `supabase/migrations/`. There are 19 tables.
