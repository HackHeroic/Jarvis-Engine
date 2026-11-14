# Capstone schema notes

These notes cover the two migrations that complete the Supabase schema:

- `supabase/migrations/20260101000000_baseline_legacy_tables.sql`
- `supabase/migrations/20261114000001_capstone_lld.sql`

Plan 2's status change, `20261115000000_skip_writes_skipped.sql`, has its own section below ("Plan 2: Skip writes `skipped`").

They record why each choice was made. They are also the source for the ER diagram: every key, relationship and constraint below was checked by introspecting a database built from `supabase/migrations/` alone (PostgreSQL 18, local).

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
| **Contains** | `app/db/migrations/001`–`004` and `006`–`008`: `pending_calendar_updates`, `behavioral_constraints`, `habit_trackers`, base `user_tasks`, `task_materials`, `user_plan_updates`, `ingested_documents`, plus the `user_tasks` columns from `006`/`007`. Also the three `user_tasks` columns from `014` (`quality_score`, `reschedule_count`, `completed_at`). No CLI file ever added those three, yet the Skip endpoint wrote `reschedule_count` (until plan 2) and `mastery_tracker` reads all three. |
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
| `goals` | `Goal` | the `ensure_goal` trigger (there is no repository yet) |
| `user_tasks` | `Task` | `accept_draft()` via `SupabaseDraftRepository.accept_atomic`; `SupabaseTaskRepository`; v1 `_persist_fused_tasks`; `/tasks` endpoints |
| `task_events` | `TaskEvent` | `SupabaseTaskEventRepository` |
| `draft_schedules` | `DraftSchedule` | `SupabaseDraftRepository`, `DraftStore` |
| `behavioral_constraints` | `BehavioralConstraint` | `SupabaseConstraintRepository`, `behavioral_store` |
| `proactive_suggestions` | `Suggestion` | `SupabaseSuggestionRepository` |
| `llm_usage_events` | `LLMUsageEvent` | `SupabaseUsageRepository` |

`Pattern` is computed and never stored. The other user tables have no domain entity yet:

| Table | Written by |
|---|---|
| `habit_trackers` | SM-2 (`sm2_engine`) |
| `user_memories` | memory store and PEARL |
| `chat_sessions`, `chat_messages` | `chat_history` |
| `task_materials` | `task_material_linker` |
| `ingested_documents` | ingestion orchestrator |
| `pending_calendar_updates` | calendar extraction |
| `user_plan_updates` | goal deadlines |
| `user_preferences` | learning style |
| `task_workspaces` | workspace cache |
| `task_completion_signals` | completion telemetry |

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
| `habit_trackers` | `habit_trackers_ef_floor` | `ef >= 1.3` (the SM-2 floor `sm2_engine` clamps to) |

`tests/test_migrations.py` pins the four status/type CHECKs to the domain enums, so the database and the entities cannot drift apart.

### Decisions behind the constraints

**`user_tasks` status.** `grep` over `app/` finds exactly these writes:
- `pending` (`build_task_rows`)
- `completed` (`/tasks/{id}/complete`)
- `pacing_pushed` (`/tasks/{id}/skip`; since plan 2 it writes `skipped`, see below)
- `missed` (`missed_deadlines`)
- `TaskStatus` values (repository `update_status`)

No code writes `in_progress`, `cancelled` or `rolled_forward`. The frontend's `updateTask` could send a free-form status through `PATCH /tasks/{id}`, but nothing calls `updateTask`. Those three statuses are therefore not allowed. One mismatch remains: `TaskUpdateRequest.status` still describes `rolled_forward`, and such a PATCH now fails the CHECK.

**`duration_minutes` / `difficulty_weight` NOT NULL.** Both live insert paths, `accept_draft` and `_persist_fused_tasks`, build their rows with `build_task_rows` from validated `TaskChunk`s, which always carry both values. The `Task` entity also requires both, and `accepted_task_rows` rejects a row without them before the RPC is sent.

**`draft_schedules.horizon_start` stays TEXT and becomes nullable.** The app writes an ISO string, and `build_task_rows` parses that same string back. A `timestamptz` column would hand back a reformatted string and would read a naive value as UTC.

**`ingested_documents.s3_key text` is new.** The ingestion pipeline writes it whenever the S3 upload succeeds, but no migration created it.

**`task_completion_signals` keeps its `quality` column.**

## Plan 2: Skip writes `skipped`

`supabase/migrations/20261115000000_skip_writes_skipped.sql` retires `pacing_pushed`.

- **Why.** Complete and Skip now go through `TaskService` (`app/services/task_service.py`). Each outcome is a compare-and-set on `user_tasks.status` plus one append-only `task_events` row, and Skip writes `skipped`. Nothing writes `pacing_pushed` any more.
- **What it does.** First it runs `update user_tasks set status = 'skipped' where status = 'pacing_pushed'`. Then it replaces `user_tasks_status_check` with `pending, completed, skipped, missed`, the `TaskStatus` values.
- **Old reads.** `_mapping._LEGACY_TASK_STATUSES` stays, so a stray `pacing_pushed` row still reads as `skipped`.
- **No status by PATCH.** `PATCH /tasks/{id}` answers 422 to any `status`. It would move the status with no event, so status changes go through Complete and Skip only. Nothing in the frontend sends one (`updateTask` has no callers).
- **What Skip no longer writes.** The `reschedule_count` column and the "needs re-decomposition" memory. Later plan 2 tasks replace both from the `skipped` events: a repeated-skip detector (Task 6) and a per-task event count (Task 12).
- **Missed tasks.** A missed slot is inferred, not asked about. `SlotPassedSweep` marks a pending task `missed`, with a `missed` event, once `scheduled_end + SLOT_GRACE_MINUTES` (30) has passed with no outcome recorded since the slot began. A passed deadline gives status `missed` with a `deadline_missed` event.
- **Re-runs.** The file is idempotent: a re-run maps nothing and re-creates the same CHECK. `tests/test_migrations.py` pins the mapping, its order before the CHECK, and the final CHECK values.

## Triggers

| Function | Fires | Purpose |
|---|---|---|
| `ensure_user_profile()` | `BEFORE INSERT` on the 18 tables that reference `user_profiles` | `insert into user_profiles(user_id) … on conflict do nothing`. The first row a user writes anywhere creates their profile, so the FKs hold without every legacy insert path creating a profile first. A NULL `user_id` (allowed only on `llm_usage_events`) is skipped. |
| `ensure_goal()` | `BEFORE INSERT` on `user_tasks`, `draft_schedules`, `user_plan_updates` | When `goal_id` is set, inserts `(user_id, goal_id, title = goal_id)` into `goals`, `on conflict do nothing`. `goals` has its own `ensure_user_profile` trigger, so the firing order on one table does not matter. The brief named the first two tables; `user_plan_updates` also carries a `goal_id` (read from `user_tasks`) and gets the same FK, so it gets the same trigger. |
| `ensure_ingested_document()` | `BEFORE INSERT` on `task_materials` | The ingestion orchestrator links a document to tasks *before* it upserts the `ingested_documents` row. Without this trigger, `task_materials_document_fkey` would reject every link, and the linker swallows errors, so linking would fail silently. The trigger inserts the `(user_id, source_id)` row; the pipeline's later upsert on `(user_id, source_id)` fills in file name, topics, chunk count and `s3_key`. |
| `set_updated_at()` | `BEFORE UPDATE` on the tables that already had `updated_at`: `user_profiles`, `user_preferences`, `user_memories`, `habit_trackers`, `chat_sessions` | Keeps `updated_at` current. On `chat_sessions` this means renaming, pinning or archiving a session now also bumps `updated_at`, and the session list is ordered by it. |

All four functions are `SECURITY INVOKER` with `search_path = public`. When a signed-in user inserts their own rows under RLS, the profile and goal inserts pass the same `owner_all` check.

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
5. Deletes that user's `status = 'pending'` tasks.
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

`grep -rn '<name>' app/ tests/` finds no reads or writes for any of the six tables below. The only hit in code is a comment in `app/services/documents/registry.py:27` naming two of them. All six are dropped with `drop table if exists … cascade`, and none was kept.

| Table | Origin |
|---|---|
| `pending_action_items` | `011` / CLI `20260329000002`; action proposals are returned in the response and never stored |
| `extracted_problems` | `011` / CLI `20260329000002` |
| `task_completion_criteria` | `011` / CLI `20260329000002` |
| `user_calendar_anchors` | `005`; never ported to the CLI set |
| `conversation_messages` | `010` / CLI `20260329000001` |
| `conversation_sessions` | `010` / CLI `20260329000001` |

## Known gaps (not schema bugs; the app needs a follow-up)

1. **Re-planning a goal can collide on `task_id`.** The planner's task ids are positional (`task_1…`), namespaced by a goal slug (`learn_dsa_task_1`).
   - Suppose a user completes `learn_dsa_task_1` and later re-plans the same goal. The new draft carries `learn_dsa_task_1` again.
   - `accept_draft` deletes only *pending* rows, so the insert hits `user_tasks_user_id_task_id_key` and the whole accept rolls back, surfacing as `PersistenceError`.
   - Without the key, the duplicate rows would make every `/tasks/{task_id}` call ambiguous. That is why the key stays.
   - The fix belongs in the planner: mint per-plan-unique task ids.
2. **`goal_id` on tasks and on drafts can disagree.**
   - `build_task_rows` takes a task's `goal_id` as the text before the *first* underscore (`learn_dsa_task_1` → `learn`).
   - `draft_schedules.goal_id` is the full slug (`learn_dsa`).
   - So one plan can create two goals.
3. **The ingestion order.** `task_materials` is written before `ingested_documents`; the `ensure_ingested_document` trigger papers over this.
