-- F2: POST /api/v1/tasks/{task_id}/complete 500s whenever the caller supplies
-- `actual_duration_minutes`. The field is declared on TaskCompleteRequest
-- (app/api/v1/endpoints/tasks.py:29-33), copied into the update payload
-- (:134-136) and written to user_tasks (:482), but no migration ever created
-- the column, so PostgREST rejects the UPDATE with PGRST204:
--   "Could not find the 'actual_duration_minutes' column of 'user_tasks'"
--
-- Nullable on purpose: the field is optional, historical rows have no value,
-- and a NULL means "the user never told us how long it actually took" — which
-- is different from zero.

alter table public.user_tasks
  add column if not exists actual_duration_minutes integer;

comment on column public.user_tasks.actual_duration_minutes is
  'Minutes the user actually spent on the task, reported at completion. '
  'NULL = not reported. Feeds future pacing / DKT calibration.';
