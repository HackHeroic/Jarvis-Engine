-- Accept persisted tasks TIMELESS: the draft never carried the solved
-- schedule, so scheduled_start/end stayed null and the UI defaulted every
-- task to 08:00. Store the solver's task_id -> {start_min, end_min} map.
alter table public.draft_schedules add column if not exists schedule jsonb;
