-- The app writes two statuses the check constraint never allowed:
--   'missed'        — missed_deadlines.detect_and_mark_missed (anti-guilt overdue marking)
--   'pacing_pushed' — POST /tasks/{id}/skip (anti-guilt skip; never 'overdue'/'failed')
-- Both writes were failing 23514 and being warning-swallowed, so overdue
-- marking and skip have never persisted against the live schema.
alter table public.user_tasks drop constraint if exists user_tasks_status_check;
alter table public.user_tasks add constraint user_tasks_status_check
    check (status in ('pending', 'in_progress', 'completed', 'skipped',
                      'cancelled', 'rolled_forward', 'missed', 'pacing_pushed'));
