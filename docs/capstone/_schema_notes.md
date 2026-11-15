# Capstone schema notes

These notes cover the two migrations that complete the Supabase schema:

- `supabase/migrations/20260101000000_baseline_legacy_tables.sql`
- `supabase/migrations/20261114000001_capstone_lld.sql`

Plan 2's status change, `20261115000000_skip_writes_skipped.sql`, has its own section below ("Plan 2: Skip writes `skipped`"), and so do its memory change, `20261115000002_memory_lifecycle_and_vectors.sql` ("Plan 2: Memory lifecycle and vectors"), and its conversation summaries, `20261115000003_session_summaries.sql` ("Plan 2: Session summaries").

They record why each choice was made. They are also the source for the ER diagram: every key, relationship and constraint below was checked by introspecting a database built from `supabase/migrations/` alone (PostgreSQL 18, local).

**Refreshed at `e76bff9`.** No migration changed after `5c6ffd3`. The plan 3 restructure changed only the application side, and the notes on who reads and writes each table are updated for it: repositories now own tasks, rules, goals' deadline updates, habit trackers and drafts, and the legacy `DraftStore`, the v1 memory store and PEARL module, `_persist_fused_tasks` and the task edit and delete routes are gone. `docs/capstone/08-database-schema.md` is the full schema reference.

## Before you apply

1. **Check the backend key.** `SUPABASE_SERVICE_KEY` must hold the project's service-role (secret) key: `sb_secret_…`, or the legacy `service_role` JWT. The capstone enables row-level security on every user table, and only the service role bypasses it.
   - A publishable key (`sb_publishable_…`, kept in `SUPABASE_PUBLISHABLE_KEY`) acts as the `anon` role. With RLS on, the app would read no rows and every write would fail.
   - A local run confirmed that `anon` sees 0 rows and cannot execute `accept_draft`.
2. **This wipes every user table** (section 0 of the capstone). The owner decided on 2026-11-14 that all live rows are test data. Export a backup first if you want one.
   - **Pre-apply check:** on the live database, `select to_regclass('public.user_profiles');` must return NULL. Otherwise the guarded wipe is skipped, and constraints added over the old rows can fail.
3. **Apply it.** Use either path:
   - `supabase db push --include-all`. The baseline's version (`20260101000000`) is older than versions the remote database has already recorded, so the CLI refuses to insert it unless you pass `--include-all`. On the live database, every statement in the baseline is a no-op.
   - Or paste the baseline, then the capstone, into the dashboard SQL editor. Each paste runs as a single transaction.
4. **Verify.**
   - `select count(*) from user_profiles;` returns 0 straight after the wipe.
   - `select accept_draft(...)` on a throwaway draft returns the number of tasks it inserted.
   - `select tablename, rowsecurity from pg_tables where schemaname = 'public';` shows `true` for all 19 tables.

`ON DELETE SET NULL (goal_id)` needs PostgreSQL 15 or later. `supabase/config.toml` declares 17.

## Migration order

**The baseline is `20260101000000`, not the last-but-one timestamp.** Several earlier CLI files `ALTER user_tasks`: `20260401000000`, `20260501000001`, `20260808000000` and `20260813000000`. On a fresh database, `user_tasks` must already exist when they run, so the baseline has to sort first. It references no table that a later file creates.

| | Baseline |
|---|---|
| **Contains** | `app/db/migrations/001`–`004` and `006`–`008`: `pending_calendar_updates`, `behavioral_constraints`, `habit_trackers`, base `user_tasks`, `task_materials`, `user_plan_updates`, `ingested_documents`, plus the `user_tasks` columns from `006`/`007`. Also the three `user_tasks` columns from `014` (`quality_score`, `reschedule_count`, `completed_at`). No CLI file ever added those three, yet the Skip endpoint wrote `reschedule_count` (until plan 2) and `mastery_tracker` read `quality_score` (until plan 3 moved it onto task events). Nothing reads or writes any of the three now. |
| **Leaves out** | `005` (`user_calendar_anchors`, which is dead) and the status CHECK from `014` (superseded). |

**One older CLI file was edited:** `20260329000000_chat_sessions.sql` now says `CREATE INDEX IF NOT EXISTS` on its three indexes. Before the edit, a second application of the set failed on them. Supabase does not checksum applied migrations, so the edit has no effect on the live history.

**The result was checked locally.**
- A fresh database built from `supabase/migrations/` in filename order succeeds.
- Applying the whole set a second time also succeeds.

## The wipe (in place of per-rule cleanup)

Section 0 of the capstone truncates the 14 legacy user tables with `restart identity cascade`: `user_tasks`, `draft_schedules`, `behavioral_constraints`, `habit_trackers`, `user_memories`, `chat_sessions`, `chat_messages`, `task_materials`, `ingested_documents`, `pending_calendar_updates`, `user_plan_updates`, `user_preferences`, `task_workspaces` and `task_completion_signals`.

**It runs only on the first application.**
- `user_profiles` is created by this migration, so if `user_profiles` already exists the wipe is skipped with a NOTICE. A re-run therefore never deletes real rows; a local check confirmed it.
- If the first application fails part-way, the whole file rolls back, wipe included, because the CLI and the SQL editor run it as one transaction. Locally, a failed attempt left all legacy rows in place.

**Because the data is gone,** every constraint is added plainly: no `NOT VALID`, no row-by-row cleanup, no backfill. The `user_profiles` and `goals` backfills from the brief are skipped; triggers create those rows from now on (see Triggers). The cleanup rules the brief planned now live on as constraints, so bad rows are rejected at write time instead of repaired later:

| Planned cleanup rule | Now enforced by |
|---|---|
| Delete rows whose `user_id` is NULL or `''` | `user_id NOT NULL` on every user table, plus `user_profiles_user_id_not_blank` (`btrim(user_id) <> ''`). The trigger makes a blank id fail at the profile. The `''` default on `pending_calendar_updates.user_id` is dropped. |
| Dedupe `user_tasks (user_id, task_id)` | `user_tasks_user_id_task_id_key UNIQUE (user_id, task_id)` |
| Null out bad durations / weights / windows | `user_tasks_duration_minutes_positive`, `user_tasks_difficulty_weight_range`, `user_tasks_scheduled_window_check`; `duration_minutes` / `difficulty_weight` NOT NULL |
| Map legacy statuses | `user_tasks_status_check` (below) |
| Delete orphan `task_materials` | `task_materials_document_fkey` |

## Table → entity map

These are the tables the domain layer persists. Entities live in `app/domain/entities.py`; repositories in `app/infrastructure/persistence/supabase_repos.py`.

| Table | Entity | Written by |
|---|---|---|
| `user_profiles` | `UserProfile` | `SupabaseProfileRepository`; the `ensure_user_profile` trigger |
| `goals` | `Goal` | the `ensure_goal` trigger only; `SupabaseGoalRepository.get` reads it |
| `user_plan_updates` | `GoalDeadlineUpdate` | `SupabaseGoalRepository.add_deadline_update` (read by `latest_context`) |
| `user_tasks` | `Task` | `accept_draft()` via `SupabaseDraftRepository.accept_atomic` (inserts); `SupabaseTaskRepository` (status compare-and-set, `bring_back`) |
| `task_events` | `TaskEvent` | `SupabaseTaskEventRepository` |
| `draft_schedules` | `DraftSchedule` | `SupabaseDraftRepository`, reached only through `DraftService` |
| `behavioral_constraints` | `BehavioralConstraint` | `SupabaseConstraintRepository` (`behavioral_store` and the adopted-rule action call it) |
| `habit_trackers` | `HabitTracker` | `SupabaseHabitTrackerRepository`: `add_for_constraint` (one per rule, read then insert), `record_review` (SM-2 check-ins), `delete_for_constraint` (before the rule goes), all through `RuleCheckIns` (2026-11-15) |
| `proactive_suggestions` | `Suggestion` | `SupabaseSuggestionRepository` |
| `llm_usage_events` | `LLMUsageEvent` | `SupabaseUsageRepository` |
| `user_memories` | `Memory` | `SupabaseMemoryRepository` |
| `chat_sessions` | `ConversationSession` | `chat_history` and the sessions endpoints (sessions, titles, pins, archive, activity); `SupabaseConversationRepository.set_summary` (the summary columns only) |
| `chat_messages` | `ChatMessage` | `chat_history` (read by `SupabaseConversationRepository`) |
| `ingested_documents` | `IngestedDocument` | `SupabaseDocumentRepository`, reached through `DocumentService` (the ingestion pipeline keeps a document; the documents routes list and delete it) |
| `task_materials` | `TaskMaterialLink` | `SupabaseDocumentRepository.link`, called by `DocumentLinker` (both directions); `workspace_builder` still reads it directly for a task's chunks |

`Pattern` is computed and never stored. The other user tables have no domain entity, and the code that reaches them is on the raw-table allow-list in `tests/test_layering.py` (`_RAW_TABLE_CALLS_ALLOWED`):

| Table | Read or written by |
|---|---|
| `pending_calendar_updates` | calendar extraction (insert), the pending-calendar endpoints |
| `user_preferences` | the learning-style read (nothing writes it) |
| `task_workspaces` | workspace cache (`workspace_builder`) |
| `task_completion_signals` | nothing since plan 3: Complete's `completed` task event is the one outcome record |

## Keys and relationships

### Primary keys

- **Composite:**
  - `user_profiles (user_id)`: text, not blank.
  - `goals (user_id, id)`: a goal id is unique per user, not globally.
- **Single `id uuid default gen_random_uuid()`:** every other table.

### Alternate keys

| Table | Unique key |
|---|---|
| `user_tasks` | `(user_id, task_id)` |
| `ingested_documents` | `(user_id, source_id)` |
| `task_materials` | `(user_id, task_id, source_id)` |
| `task_workspaces` | `(user_id, task_id)` |
| `user_preferences` | `(user_id)`, which makes it 1:1 with `user_profiles` |

### Foreign keys

| Child | Columns | Parent | On delete |
|---|---|---|---|
| the other 17 user tables (all but `user_profiles` and `llm_usage_events`) | `user_id` | `user_profiles (user_id)` | CASCADE |
| `llm_usage_events` | `user_id` (nullable) | `user_profiles (user_id)` | SET NULL: the cost record stays, unattributed |
| `user_tasks` | `(user_id, goal_id)` | `goals (user_id, id)` | SET NULL (`goal_id`) |
| `draft_schedules` | `(user_id, goal_id)` | `goals (user_id, id)` | SET NULL (`goal_id`) |
| `user_plan_updates` | `(user_id, goal_id)` | `goals (user_id, id)` | SET NULL (`goal_id`) |
| `task_materials` | `(user_id, source_id)` | `ingested_documents (user_id, source_id)` | CASCADE |
| `chat_messages` | `session_id` | `chat_sessions (id)` | CASCADE (pre-existing) |
| `habit_trackers` | `constraint_id` | `behavioral_constraints (id)` | SET NULL (pre-existing) |
| `user_memories` | `superseded_by` | `user_memories (id)` | NO ACTION (pre-existing; deleting a user still cascades cleanly, checked locally) |

**Why `goal_id` links use SET NULL.** `goal_id` is optional on all three tables, so deleting a goal clears the link and keeps the tasks, drafts and deadlines. A goal FK that includes `user_id` also means a row can never point at another user's goal.

**Soft references (deliberately no FK).** These columns point at other rows without a database constraint:

| Column | Points at | Why there is no FK |
|---|---|---|
| `task_id` in `task_events`, `task_materials`, `task_workspaces`, `task_completion_signals` | `user_tasks (user_id, task_id)` | Accepting a draft deletes and re-inserts every pending task. An FK would either cascade-delete the history, links and caches, or block the accept. |
| `user_tasks.plan_id` | the plan id `build_task_rows` mints | There is no plans table. |
| `user_tasks.dependencies` | task_ids, as a JSON array | A JSON array cannot carry an FK. |
| `draft_schedules.tasks` / `schedule` | proposed chunks and slots | Stored as JSON. |
| `user_memories.source_id` | a message or a task | Polymorphic. |
| `user_memories.source_session_id`, `source_message_id` | `chat_sessions (id)`, `chat_messages (id)` | A memory learned in a conversation that was never saved must still be stored, and a key on the id alone would not check that the session is the same user's. Both families check only that the value is a UUID (`20261115000002`). |
| `habit_trackers.habit_id` | a habit | Free text. |
| `llm_usage_events.session_id` | a chat session | Sessions can be ephemeral when persisting them fails. |

### Indexes the capstone adds

| Index | Serves |
|---|---|
| `task_events (user_id, occurred_at desc)`, `(user_id, task_id)` | event history and per-task lookups |
| `proactive_suggestions (user_id, status, created_at desc)` | the suggestion inbox |
| `llm_usage_events (user_id, created_at desc)` | usage summaries |
| `user_tasks (user_id, goal_id)`, `draft_schedules (user_id, goal_id)` | the goal FKs |
| `task_materials (user_id, source_id)` | the document FK |
| `user_tasks (user_id, scheduled_start)` (full) | `SupabaseTaskRepository.list_scheduled_between`, which reads every status. The older `idx_user_tasks_scheduled` is partial (`where status = 'pending'`) and cannot serve it. |

## CHECK and NOT NULL constraints

| Table | Constraint | Rule |
|---|---|---|
| `user_profiles` | `user_profiles_autonomy_mode_check` | `autonomy_mode in ('suggest_only','ask_first','auto_low_risk')` |
| `user_profiles` | `user_profiles_user_id_not_blank` | `btrim(user_id) <> ''` |
| `task_events` | `task_events_event_type_check` | `TaskEventType` values |
| `proactive_suggestions` | `proactive_suggestions_status_check`, `…_consent_behavior_check` | `SuggestionStatus` values; `allow / ask / deny` |
| `llm_usage_events` | `llm_usage_events_location_check` | `local / cloud` |
| `user_tasks` | `user_tasks_status_check` | `pending, completed, skipped, missed` (`pacing_pushed` until `20261115000000`) |
| `user_tasks` | `user_tasks_duration_minutes_positive` | `duration_minutes > 0` (NOT NULL, default 25) |
| `user_tasks` | `user_tasks_difficulty_weight_range` | `0 ≤ difficulty_weight ≤ 1` (NOT NULL, default 0.5) |
| `user_tasks` | `user_tasks_scheduled_window_check` | end after start when both are set |
| `draft_schedules` | `draft_schedules_status_check` | `DraftStatus` values, including `superseded` |
| `behavioral_constraints` | `behavioral_constraints_source_check` | `source in ('said','inferred')`, NOT NULL, default `said` |
| `user_memories` | `user_memories_confidence_range`, `user_memories_strength_range` | both between 0 and 1 |
| `user_memories` | `user_memories_status_check`, `user_memories_provenance_check` | `MemoryStatus` values; `said / inferred` (`20261115000002`) |
| `user_memories` | `user_memories_observation_count_check`, `user_memories_stability_range` | `observation_count >= 1`; `0.01 ≤ stability ≤ 20` (`20261115000002`) |
| `user_memories` | NOT NULL | `confidence`, `strength`, `stability`, `observation_count`, `source`, `created_at`: legacy NULLs take the column default first (`20261115000002`) |
| `habit_trackers` | `habit_trackers_ef_floor` | `ef >= 1.3` (the SM-2 floor `sm2_engine` clamps to) |

`tests/test_migrations.py` pins the four status/type CHECKs to the domain enums, so the database and the entities cannot drift apart.

### Decisions behind the constraints

**`user_tasks` status.** When the capstone migration was written, `grep` over `app/` found exactly these writes:
- `pending` (`build_task_rows`)
- `completed` (`/tasks/{id}/complete`)
- `pacing_pushed` (`/tasks/{id}/skip`; since plan 2 it writes `skipped`, see below)
- `missed` (`missed_deadlines`)
- `TaskStatus` values (repository `update_status`)

No code wrote `in_progress`, `cancelled` or `rolled_forward`, so those three statuses are not allowed. At `e76bff9` every status write goes through `TaskRepository` (`update_status`, `bring_back`) or `accept_draft()`, with `TaskStatus` values only. Plan 3 deleted `PATCH /tasks/{id}` and its `TaskUpdateRequest`, the last place that described `rolled_forward`, so no route can send a free-form status.

**`duration_minutes` / `difficulty_weight` NOT NULL.** The one insert path, `accept_draft()`, takes rows that `build_task_rows` builds from validated `TaskChunk`s, which always carry both values. (The v1 `_persist_fused_tasks` path did the same; plan 3 deleted it.) The `Task` entity also requires both, and `accepted_task_rows` (`app/infrastructure/persistence/_mapping.py:207`) rejects a row without them before the RPC is sent.

**`draft_schedules.horizon_start` stays TEXT and becomes nullable.** The app writes an ISO string, and `build_task_rows` parses that same string back. A `timestamptz` column would hand back a reformatted string and would read a naive value as UTC.

**`ingested_documents.s3_key text` is new, and unused.** It was meant for an S3 upload that never ran (`boto3` was never installed), and no migration created it. The uploaded file is now kept in Supabase Storage (bucket `jarvis-documents`) at a key derived from the row itself, `{user_id}/{source_id}`, so nothing writes or reads `s3_key`; the column stays (no migration drops it) and is always null.

**`task_completion_signals` keeps its `quality` column.** Since plan 3 nothing writes the table at all; it is kept, not dropped.

## Plan 2: Skip writes `skipped`

`supabase/migrations/20261115000000_skip_writes_skipped.sql` retires `pacing_pushed`.

- **Why.** Complete and Skip now go through `TaskService` (`app/services/task_service.py`). Each outcome is a compare-and-set on `user_tasks.status` plus one append-only `task_events` row, and Skip writes `skipped`. Nothing writes `pacing_pushed` any more.
- **What it does.** First it runs `update user_tasks set status = 'skipped' where status = 'pacing_pushed'`. Then it replaces `user_tasks_status_check` with `pending, completed, skipped, missed`, the `TaskStatus` values.
- **Old reads.** `_mapping._LEGACY_TASK_STATUSES` stays, so a stray `pacing_pushed` row still reads as `skipped`.
- **No status by PATCH.** Plan 2 made `PATCH /tasks/{id}` answer 422 to any `status`, because it would move the status with no event. Plan 3 deleted the route, with `DELETE /tasks/{id}` and `DELETE /tasks/`, so status changes go through `TaskService` only: Complete, Skip, the passed-slot sweep, the deadline scan and "bring them back".
- **What Skip no longer writes.** The `reschedule_count` column and the "needs re-decomposition" memory. Later plan 2 tasks replace both from the `skipped` events: a repeated-skip detector (Task 6) and a per-task event count (Task 12).
- **Missed tasks.** A missed slot is inferred, not asked about. `SlotPassedSweep` marks a pending task `missed`, with a `missed` event, once `scheduled_end + SLOT_GRACE_MINUTES` (30) has passed with no outcome recorded since the slot began. A passed deadline gives status `missed` with a `deadline_missed` event.
- **Re-runs.** The file is idempotent: a re-run maps nothing and re-creates the same CHECK. `tests/test_migrations.py` pins the mapping, its order before the CHECK, and the final CHECK values.

## Plan 2: Memory lifecycle and vectors

`supabase/migrations/20261115000002_memory_lifecycle_and_vectors.sql` makes `user_memories` the storage of the `Memory` entity (`app/domain/entities.py`), behind `MemoryRepository` (`SupabaseMemoryRepository` / `InMemoryMemoryRepository`, one contract suite).

**Apply note (historical).** The file had to go live together with plan 2's Task 7: the legacy extractor (`MemoryStore.find_similar_memory`) read `embedding` as a float array, and after this migration PostgREST returns a vector as a string. Plan 3 deleted that extractor and `MemoryStore`; only `SupabaseMemoryRepository` touches the table now.

**What it does.**
1. `create extension if not exists vector with schema extensions`, then `set local search_path = public, extensions`. Every later statement names the type unqualified (`vector(384)`), so the file works wherever pgvector lives: in `extensions`, or in `public` on a database that installed it there before (where the `create extension` does nothing). `SET LOCAL` needs the transaction the CLI and the SQL editor run each file in.
2. Adds the lifecycle columns, each guarded by `add column if not exists`:

   | Column | Type | Entity field |
   |---|---|---|
   | `status` | `text not null default 'active'`, CHECK `MemoryStatus` | `status` |
   | `provenance` | `text not null default 'said'`, CHECK `said / inferred` | `provenance` |
   | `pattern_key` | `text` | `pattern_key` |
   | `source_session_id` | `uuid`, no foreign key (a soft reference) | `source_session_id` |
   | `source_message_id` | `uuid`, no foreign key (a soft reference) | `source_message_id` |
   | `last_confirmed_at` | `timestamptz` | `last_confirmed_at` |
   | `strength_as_of` | `timestamptz not null default now()` | `strength_as_of` |

3. Backfills `provenance = 'inferred'` where `source = 'behavior'` (PEARL's rows). A re-run touches no row.
4. Makes the legacy nullable columns `NOT NULL`: `confidence` (0.5), `strength` (1.0), `stability` (1.0), `observation_count` (1), `source` (`conversation`) and `created_at` (the row's `updated_at`, else now). The NULLs take those values first. It then brings legacy rows inside the entity's ranges (`observation_count` to at least 1, `stability` clamped to [0.01, 20]) and adds the two range CHECKs.
5. Turns `embedding` from `float[]` into `vector(384)`. Only a one-dimensional array of exactly 384 numbers, none of them NULL, NaN or infinite, is cast. Everything else becomes NULL, because pgvector would refuse it and fail the migration. The checks are ordered `WHEN`s of one `CASE`, since `array_position` fails on a multi-dimensional array. The type change runs only while the column is still an array.
6. Adds a unique partial index `idx_user_memories_user_pattern_key (user_id, pattern_key) where pattern_key is not null`, and `idx_user_memories_user_status (user_id, status)`. There is no ANN index: a user holds tens to hundreds of memories, scanned exactly behind the `user_id` filter.
7. Adds `match_memories(p_user_id, p_query, p_statuses, p_types, p_limit, p_min_similarity)`, below.
8. Revokes `execute` on it from `public` and `anon`, and grants it to `service_role` and `authenticated`.

**Column mapping.**
- The entity's `origin` (conversation, behavior, …) is the legacy `source` column. The new `provenance` column says whether the user said it.
- The legacy `source_id`, `last_accessed`, `last_reinforced` and `updated_at` columns have no entity field.
- Every read names its columns (`_mapping.MEMORY_COLUMNS`) and never selects `embedding`, which is 384 floats a row. Inserts and updates still get the written row back, embedding included: postgrest-py cannot narrow a write's returned columns.
- A legacy row (written by the v1 path before plan 3 deleted it) with NULL in a column the entity has a default for reads with that default; a missing `created_at` reads as `updated_at`, then `strength_as_of` (`_mapping.memory_from_row`).

**What both repository families check before writing.** The checks live in `_mapping`, so the families cannot disagree:
- An embedding, and the query of `nearest`, must be exactly 384 finite numbers with a non-zero norm, or the call raises `ValidationError`. The column rejects any other length, and pgvector's cosine distance to a zero vector is NaN, which Postgres sorts above every number, so such a query would return every row.
- `source_session_id`, `source_message_id` and `superseded_by` must be UUIDs (`ValidationError`).
- `nearest`'s `limit` must not be negative (`ValidationError`).
- An id already in use, by any user, raises `ConflictError` with `{"memory_id": …}`, as `user_memories_pkey` does. A `pattern_key` the user already holds raises `ConflictError` with `{"pattern_key": …}`, as `idx_user_memories_user_pattern_key` does. Any other database error stays `PersistenceError`.

**`match_memories`.** It is `language sql stable`, with `set search_path = public, extensions` so the `<=>` operator resolves.
- It keeps the user's rows whose status is in `p_statuses`, whose type is in `p_types` (NULL means any type), and that have an embedding and are not superseded.
- `similarity = 1 - (embedding <=> p_query)`. Only rows with similarity `>= p_min_similarity` are kept, ordered by distance and limited to `p_limit`.
- It returns every `user_memories` column except `embedding`, plus `similarity double precision`.
- `SupabaseMemoryRepository.nearest` calls it; supabase-py sends `p_query` as a JSON array, which PostgREST reads as the vector's text form.

**Checked against PostgreSQL 18 + pgvector (local, throwaway database).**
- Built on top of the rest of the set, over legacy rows: a 384-long array became a vector; a 2-long one became NULL; the `behavior` row read `inferred`; `stability` 25 became 20 and `observation_count` 0 became 1.
- Arrays holding NaN, -Infinity or a NULL element, a 2-dimensional array and an empty one all became NULL without failing the migration. A row with every legacy column NULL got the defaults, and all six columns are now `NOT NULL`.
- The same results, and the same function, with pgvector installed beforehand in `public` instead of `extensions`.
- A second application touched no row (`updated_at` unchanged). Applying the whole set again also succeeds.
- Ranking, the type filter, `p_limit`, `p_min_similarity` and the status filter behave as above. A user with no memories gets no rows, whatever the statuses.
- A duplicate `(user_id, pattern_key)` fails on `idx_user_memories_user_pattern_key`, and the same key for another user is accepted. `anon` gets `permission denied for function match_memories`.

**Fake vs SQL.** `FakeSupabase` mirrors `match_memories` with cosine similarity over the stored lists and the same filters. It also enforces `user_memories_pkey` and the unique `(user_id, pattern_key)` index on `insert` and `update`, in that order, raising the database's messages. The repositories validate embeddings before either store sees them.

## Plan 2: Session summaries

`supabase/migrations/20261115000003_session_summaries.sql` gives each conversation a running summary of its older turns, read and written through `ConversationRepository` (`SupabaseConversationRepository` / `InMemoryConversationRepository`, one contract suite). The repository reads `chat_sessions` and `chat_messages` (which `app/services/chat_history.py` still writes) and writes only the summary columns: the three below, and `summary_covered_through` from `20261115000006_session_summary_reach.sql`. Entities: `ConversationSession`, `ChatMessage`.

**Apply note.** Apply it with the code that reads it: `SupabaseConversationRepository` names the summary columns in every session read, so until it is applied each of those reads fails. The chat turn then falls back to the plain newest-ten window, with no summary and no "since we last spoke", and logs a warning each turn. The migration never wipes or rewrites a row.

| Column | Type | Entity field |
|---|---|---|
| `summary` | `text`, CHECK `summary is null or char_length(summary) <= 4000` | `summary` |
| `summary_through` | `timestamptz`; CHECK `(summary is null) = (summary_through is null)` | `summary_through` |
| `summary_updated_at` | `timestamptz` | `summary_updated_at` |

- `summary` is the model's paragraph, then one `- ` line per open thread (at most 1200 + 5 x 200 characters, well under the CHECK). It is stored only after the summary filter (nothing close to a forgotten or dismissed memory, nothing that blames); a summary found holding such a sentence later is written back without it.
- `summary_through` is where the verbatim window starts: the history shows only the messages created after it, so a summary that lags only makes the window longer. Compaction sets it to the last message it summarised. An idle summary covers every message but still stops `summary_through` before the newest 8 (or leaves it where it was, with 8 or fewer; on a first summary, at the session's start), so a user who comes back sees those word for word.
- **`set_summary` is a compare-and-set** in the UPDATE's own filter (`summary_through <= through`, or `is null`): a slower writer never moves the window's start back or replaces a summary that reaches further. Equal is allowed, so an idle summary that keeps the window's start can land. Zero rows means another writer won.
- `summary_updated_at` is when the summary was written. The sweep's daily rescan picks a session only when it was active after that (`summary_updated_at is null or < updated_at`), so a session summarised once idle is not summarised again; the trigger below keeps `updated_at` for the summary write itself.
- **`updated_at` stays the conversation's last activity.** The capstone's `set_updated_at()` stamped `now()` on every update, so a summary write would have read as activity: the idle summary's 30 minutes would restart, and "since we last spoke" would count from the write instead of the last message. `chat_sessions` now fires `set_chat_session_updated_at()` instead (same trigger name, `create or replace trigger`). It keeps `old.updated_at` when the update changes the summary columns and nothing else (compared as `to_jsonb(row)` minus those columns and `updated_at`); every other update, including the message-save bump that sets only `updated_at`, still stamps `now()`.
- RLS and `owner_all` on `chat_sessions` come from the capstone; the file restates them idempotently.
- No new index: the reads use `idx_chat_sessions_user_id (user_id, updated_at desc)`, `idx_chat_messages_session (session_id, created_at)` and `idx_chat_messages_user (user_id, created_at desc)`. Every read is bounded and newest first (PostgREST's 1000-row cap can only drop the oldest).

**Checked against PostgreSQL 18 (local, throwaway database).** The whole set, then the whole set again, applies cleanly. A summary-only update left `updated_at` unchanged; an update setting only `updated_at` (a new message) and one moving it back both ended at `now()`; a 4001-character summary and a summary cleared without `summary_through` were rejected by their CHECKs; the trigger on `chat_sessions` is `set_chat_session_updated_at`, and RLS is on.

**Fake vs SQL.** `FakeSupabase` has no triggers, so `updated_at` simply stays as written; the repositories never send it with a summary.

## Triggers

| Function | Fires | Purpose |
|---|---|---|
| `ensure_user_profile()` | `BEFORE INSERT` on the 18 tables that reference `user_profiles` | `insert into user_profiles(user_id) … on conflict do nothing`. The first row a user writes anywhere creates their profile, so the FKs hold without every legacy insert path creating a profile first. A NULL `user_id` (allowed only on `llm_usage_events`) is skipped. |
| `ensure_goal()` | `BEFORE INSERT` on `user_tasks`, `draft_schedules`, `user_plan_updates` | When `goal_id` is set, inserts `(user_id, goal_id, title = goal_id)` into `goals`, `on conflict do nothing`. `goals` has its own `ensure_user_profile` trigger, so the firing order on one table does not matter. The brief named the first two tables; `user_plan_updates` also carries a `goal_id` (read from `user_tasks`) and gets the same FK, so it gets the same trigger. |
| `ensure_ingested_document()` | `BEFORE INSERT` on `task_materials` | Written when the ingestion orchestrator linked a document to tasks *before* it upserted the `ingested_documents` row; without it, `task_materials_document_fkey` would have rejected every link. `DocumentService` now stores the record first and links after, so the trigger finds the row already there; it stays as a guard for any link written first. |
| `set_updated_at()` | `BEFORE UPDATE` on the tables that already had `updated_at`: `user_profiles`, `user_preferences`, `user_memories`, `habit_trackers` (and `chat_sessions` until plan 2) | Keeps `updated_at` current. |
| `set_chat_session_updated_at()` | `BEFORE UPDATE` on `chat_sessions` (plan 2, `20261115000003`) | As `set_updated_at()`, except that an update changing only the summary columns keeps `updated_at`: a summary is not activity. Renaming, pinning or archiving a session still bumps `updated_at`, and the session list is ordered by it. |

All five functions are `SECURITY INVOKER` with `search_path = public`. When a signed-in user inserts their own rows under RLS, the profile and goal inserts pass the same `owner_all` check.

## Row-level security

All 19 public tables have RLS enabled with one policy, `owner_all`: `using (user_id = (select auth.uid())::text) with check (same)`.

Wrapping `auth.uid()` in `(select …)` makes Postgres evaluate it once per statement instead of once per row (the form Supabase's linter recommends).

**The auth seam.** A policy matches a signed-in user only when the app's `user_id` equals that user's Supabase auth UUID (`auth.uid()`). Today the backend uses free-form ids such as `demo` and connects as the service role, which bypasses RLS, so the policies take effect only once:
- clients sign in through Supabase Auth, and
- `user_id`s are the auth UUIDs.

Until then the policies record the intent and lock out `anon`.

Checked locally:
- A signed-in user sees only their own rows.
- A forged insert for another user is rejected.
- `anon` sees nothing.
- `service_role` sees everything.

## `accept_draft(p_draft_id uuid, p_user_id text, p_rows jsonb) returns integer`

It runs as one transaction:

1. Locks the user's `user_profiles` row (`for no key update`), so a user's accepts run one at a time. See "Serialisation" below.
2. Locks the draft row (`select … for update`, matched on id *and* user).
3. Raises `DRAFT_NOT_FOUND` if the draft is missing or belongs to someone else.
4. Raises `DRAFT_NOT_OPEN` unless the status is `pending` or `modified` and the draft has not expired.
5. Deletes that user's `status = 'pending'` tasks, plus their `status = 'missed'` tasks whose `task_id` appears in `p_rows` (v2, `20261115000001_accept_draft_reslots_missed.sql`: a passed slot comes back under its own id). Other missed, completed and skipped rows keep their ids.
6. Inserts `p_rows`.
7. Marks the draft `accepted`.
8. Marks every other open draft of the user (`pending` / `modified`) `superseded`.
9. Returns the number of tasks inserted.

Any error rolls back all of it.

**Privileges.** `execute` is revoked from `public` and `anon` and granted to `service_role` and `authenticated`. The function is `SECURITY INVOKER`, so a signed-in caller is also bound by RLS.

### Serialisation

**Why the draft lock alone is not enough.** Two accepts of two *different* open drafts of one user lock different rows. Both pass the status check, and both plans end up pending. That race was reproduced locally against the first version of this function.

**Why the profile row and not an advisory lock.** A draft always has a profile row, because `draft_schedules.user_id` is a NOT NULL FK to it. So when there is no profile row to lock there is no draft either, and the call ends in `DRAFT_NOT_FOUND` having written nothing. The lock is therefore correct in every case. It is also exact per user, whereas a `hashtext()` advisory key can collide across users.

**Why `NO KEY UPDATE`.** Every insert into a table that references `user_profiles` takes `KEY SHARE` on the profile row. `NO KEY UPDATE` does not block those inserts; `UPDATE` would.

**Why the user lock comes before the draft lock.** The function also updates the user's other drafts. Suppose the user lock came second:
- Session A holds draft A and the user lock.
- Session B holds draft B and waits for the user lock.
- A then waits on draft B to supersede it.

That is a deadlock. Locally, with the locks in that order and a 1 s pause, session B died with `deadlock detected`, which would reach the API as a 503. With the user lock first, B waits and then gets `DRAFT_NOT_OPEN`.

### Explicit column list

The function does not use `select *` from `jsonb_populate_recordset`, because that would set `id`, `created_at`, `topic_keywords` and every other omitted column to NULL. Instead, `v_columns` lists every key `build_task_rows` emits (`tests/test_migrations.py` pins this):
- For each row it keeps only those keys, then inserts only the columns the row actually carries.
- An omitted column therefore takes its real table default.
- `user_id` is always overwritten with `p_user_id`, so a row cannot be written for another user.

### Behaviour checked against PostgreSQL 18

Rows were produced by `build_task_rows` + `accepted_task_rows`.

| Case | Result |
|---|---|
| Happy path | Returns 3; the completed task survives; ids, `created_at` and the `topic_keywords` default are filled in; the goal is created; the user's other open draft becomes `superseded`. |
| Second call on the same draft, or accepting a superseded draft | `DRAFT_NOT_OPEN` |
| Unknown draft, another user's draft, or a user with no profile | `DRAFT_NOT_FOUND` |
| Expired draft | `DRAFT_NOT_OPEN` |
| One bad row in `p_rows` | Whole call rolled back: pending tasks, the draft's status and the other draft's status are all untouched. |
| Two sessions accept two different open drafts of one user at once | The second waits for the first. Only the first plan's tasks are pending, the other draft is `superseded`, the second session gets `DRAFT_NOT_OPEN`, and so does accepting that draft again afterwards. |
| `anon` calls it | `permission denied for function accept_draft` |
| v2: a plan re-slots one of two missed tasks (and the user also has a skipped one) | That row is pending with the new slot; the other missed row, the skipped row and another user's missed row with the same `task_id` are untouched. Re-using the skipped id is still a unique violation and writes nothing. `p_rows` null deletes only pending rows. |

### Fake vs SQL

`tests/fakes.py` (`FakeSupabase._accept_draft`) and the InMemory repository mirror the function's contract, but differ in these ways:

| | SQL `accept_draft` | Fake / InMemory |
|---|---|---|
| `id` key in a row | Stripped; the row gets a fresh `gen_random_uuid()` | Kept as given |
| Other unknown keys | Stripped | Kept (fake) |
| `user_id` in a row | Forced to `p_user_id` | Kept as given (`accepted_task_rows` already stamps it) |
| Constraints (unique `(user_id, task_id)`, CHECKs, NOT NULL) | Enforced; one bad row rolls back the call | Not enforced |
| The user's other open drafts | Marked `superseded` | Left open, so accepting one later succeeds in the fake but raises `DRAFT_NOT_OPEN` in SQL |
| Serialisation | Per user (profile row lock) | Global lock (fake) / store lock (InMemory) |

None of these differences matters for well-formed rows from `build_task_rows`, except supersession.

## Dropped tables

`grep -rn '<name>' app/ tests/` finds no reads or writes for any of the six tables below. Apart from `tests/test_migrations.py`, which lists them as dropped, the only hit is a comment in `app/services/documents/registry.py:27` naming two of them. All six are dropped with `drop table if exists … cascade`, and none was kept.

| Table | Origin |
|---|---|
| `pending_action_items` | `011` / CLI `20260329000002`; action proposals are returned in the response and never stored |
| `extracted_problems` | `011` / CLI `20260329000002` |
| `task_completion_criteria` | `011` / CLI `20260329000002` |
| `user_calendar_anchors` | `005`; never ported to the CLI set |
| `conversation_messages` | `010` / CLI `20260329000001` |
| `conversation_sessions` | `010` / CLI `20260329000001` |

## Known gaps (not schema bugs; the app needs a follow-up)

1. **Re-planning a goal no longer collides on `task_id` (closed).** Each planning run mints a run token, so new ids are `{goal_id}_{run}_{task_id}` (`mint_run_token` and `namespace_chunk`, `app/services/planning/goal_ids.py:37, 51`).
   - A plan that still reuses a completed or skipped task's id hits `user_tasks_user_id_task_id_key` (`accept_draft` deletes only *pending* rows and the re-slotted missed ones). The whole accept rolls back, and the repository maps it to `ConflictError` (`task_id_clash`, 409), not `PersistenceError`.
   - Without the key, duplicate rows would make every `/tasks/{task_id}` call ambiguous. That is why the key stays.
2. **`goal_id` on tasks and on drafts can disagree, for chunks without a `goal_id`.**
   - `build_task_rows` takes a task's `goal_id` from the chunk. Only a chunk without one falls back to the text before the *first* underscore (`learn_dsa_task_1` → `learn`; `app/services/planning/task_rows.py:192`).
   - `draft_schedules.goal_id` is the full slug (`learn_dsa`).
   - So an older plan can create two goals.
3. **The ingestion order.** `task_materials` is written before `ingested_documents`; the `ensure_ingested_document` trigger papers over this.
