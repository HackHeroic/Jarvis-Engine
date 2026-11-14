-- pacing_pushed is retired (capstone plan 2, decision 18).
--
-- The Skip endpoint now goes through TaskService, which writes status 'skipped' and
-- one 'skipped' task event, so nothing writes 'pacing_pushed' any more. Existing rows
-- become 'skipped' (the repositories already read them that way), then the value
-- leaves the CHECK. The repositories keep reading a stray 'pacing_pushed' as skipped
-- (_mapping._LEGACY_TASK_STATUSES), so a read never fails on an old row.
--
-- Idempotent: a re-run maps nothing and re-creates the same CHECK.

update public.user_tasks set status = 'skipped' where status = 'pacing_pushed';

alter table public.user_tasks drop constraint if exists user_tasks_status_check;
alter table public.user_tasks add constraint user_tasks_status_check
    check (status in ('pending', 'completed', 'skipped', 'missed'));
