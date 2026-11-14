-- PEARL's task fetch (app/services/memory/pearl.py, detect_patterns) selects
-- user_tasks.original_duration_minutes and user_tasks.original_deadline_hint, but no
-- migration ever created either column. PostgREST rejects the whole query
--   42703: column user_tasks.original_duration_minutes does not exist
-- so every PEARL detector failed on every turn. (It names only the first unknown
-- column; original_deadline_hint is missing too and would fail the query next.)
--
-- What reads them: detect_duration_preference compares duration_minutes with
-- original_duration_minutes ("user edits tasks shorter"); detect_deadline_buffer
-- compares deadline_hint with original_deadline_hint ("user extends deadlines").
-- What writes them: nothing yet. A duration or deadline edit (PATCH
-- /api/v1/tasks/{task_id}, the EDIT_TASK handler) should record the planned value
-- here before overwriting it; until it does, both detectors see no edits and stay
-- quiet instead of failing.
--
-- Nullable on purpose: NULL means "never edited".

alter table public.user_tasks
  add column if not exists original_duration_minutes integer
    constraint user_tasks_original_duration_minutes_check
    check (original_duration_minutes is null or original_duration_minutes > 0),
  add column if not exists original_deadline_hint text;

comment on column public.user_tasks.original_duration_minutes is
  'The planned duration_minutes before the user first edited it. NULL = never edited. '
  'Read by PEARL detect_duration_preference.';
comment on column public.user_tasks.original_deadline_hint is
  'The planned deadline_hint before the user first edited it. NULL = never edited. '
  'Read by PEARL detect_deadline_buffer.';
