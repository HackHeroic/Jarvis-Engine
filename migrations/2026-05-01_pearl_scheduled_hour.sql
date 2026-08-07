-- Optional: materialised scheduled_hour for PEARL TimeOfDay detector.
-- The detector also derives this from scheduled_start at query time, so
-- this migration is a performance optimisation, not a correctness fix.

alter table public.user_tasks
  add column if not exists scheduled_hour smallint;

create index if not exists user_tasks_scheduled_hour_idx
  on public.user_tasks (user_id, scheduled_hour)
  where scheduled_hour is not null;

-- Backfill from scheduled_start where present
update public.user_tasks
   set scheduled_hour = extract(hour from scheduled_start)::smallint
 where scheduled_start is not null
   and scheduled_hour is null;
