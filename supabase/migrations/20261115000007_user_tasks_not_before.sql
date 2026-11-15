-- A task's earliest start: user_tasks.not_before, and accept_draft v3 that writes it.
--
-- "Plan one hour tomorrow evening" decomposes into tasks that may not start before
-- tomorrow 18:00 on the user's clock (TaskChunk.not_before). The planner carries the
-- user's pending tasks into every new plan and re-solves them, so the bound must be
-- stored with the task, or the next plan pulls it earlier. NULL: no bound.
--
-- accept_draft is 20261115000001_accept_draft_reslots_missed.sql's, unchanged except
-- for 'not_before' at the end of its column list; the comment, revoke and grant are
-- repeated. FakeSupabase._accept_draft (tests/fakes.py) mirrors the column list.
-- Idempotent: add column if not exists, create or replace.

alter table public.user_tasks add column if not exists not_before timestamptz;

comment on column public.user_tasks.not_before is
    'Earliest instant the task may be scheduled (the user asked for it then, e.g. '
    '"tomorrow evening"); NULL when there is no such bound.';

create or replace function public.accept_draft(p_draft_id uuid, p_user_id text, p_rows jsonb)
returns integer
language plpgsql
set search_path = public
as $$
declare
    -- The user_tasks columns a row may set: every key build_task_rows emits
    -- (app/services/planning/task_rows.py). A key outside this list is dropped, and a
    -- column a row leaves out takes its table default (id, created_at, topic_keywords,
    -- ...), which `insert ... select * from jsonb_populate_recordset(...)` would set to NULL.
    v_columns constant text[] := array[
        'user_id', 'plan_id', 'task_id', 'goal_id', 'title', 'status',
        'duration_minutes', 'difficulty_weight', 'dependencies', 'deadline_hint',
        'completion_criteria', 'implementation_intention', 'topic_keywords',
        'scheduled_start', 'scheduled_end', 'created_at', 'not_before'
    ];
    v_status   text;
    v_expires  timestamptz;
    v_row      jsonb;
    v_names    text;
    v_inserted integer;
    v_count    integer := 0;
begin
    -- One accept at a time per user. Without this, two accepts of two different
    -- drafts lock different rows, both pass the status check, and both plans end up
    -- pending.
    -- * It locks the user's profile row rather than taking an advisory lock. A draft
    --   always has a profile row (draft_schedules.user_id is a NOT NULL FK to it), so
    --   when there is nothing to lock there is no draft either: the call ends in
    --   DRAFT_NOT_FOUND having written nothing. A row lock is also exact per user,
    --   where a hashtext() advisory key can collide across users.
    -- * NO KEY UPDATE, not UPDATE: inserts that reference user_profiles take KEY
    --   SHARE on this row, and NO KEY UPDATE does not block them.
    -- * Taken BEFORE the draft lock: this function also updates the user's other
    --   drafts, so draft-then-user ordering lets two accepts deadlock.
    perform 1
       from public.user_profiles
      where user_id = p_user_id
        for no key update;

    select status, expires_at into v_status, v_expires
      from public.draft_schedules
     where id = p_draft_id and user_id = p_user_id
       for update;
    if not found then
        raise exception 'DRAFT_NOT_FOUND';
    end if;
    if v_status not in ('pending', 'modified') or (v_expires is not null and v_expires < now()) then
        raise exception 'DRAFT_NOT_OPEN';
    end if;

    delete from public.user_tasks
     where user_id = p_user_id
       and (status = 'pending'
            or (status = 'missed'
                and task_id in (select value ->> 'task_id'
                                  from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)))));

    for v_row in select value from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) loop
        -- Keep only the listed keys (jsonb_populate_record would still parse an
        -- unlisted one such as "id"), and write the row for the draft's owner only.
        select jsonb_object_agg(key, value)
          into v_row
          from jsonb_each(v_row || jsonb_build_object('user_id', p_user_id))
         where key = any (v_columns);
        select string_agg(format('%I', col), ', ' order by ord)
          into v_names
          from unnest(v_columns) with ordinality as listed(col, ord)
         where v_row ? col;
        execute format(
            'insert into public.user_tasks (%1$s) '
            'select %1$s from jsonb_populate_record(null::public.user_tasks, $1)',
            v_names
        ) using v_row;
        get diagnostics v_inserted = row_count;
        v_count := v_count + v_inserted;
    end loop;

    update public.draft_schedules
       set status = 'accepted'
     where id = p_draft_id and user_id = p_user_id;

    -- The accepted plan replaces every other plan the user had open.
    update public.draft_schedules
       set status = 'superseded'
     where user_id = p_user_id
       and id <> p_draft_id
       and status in ('pending', 'modified');

    return v_count;
end $$;

comment on function public.accept_draft(uuid, text, jsonb) is
    'Accept an open draft and replace the user''s pending tasks, plus their missed tasks '
    'whose task_id p_rows re-slots, with p_rows, atomically; the user''s other open drafts '
    'become superseded. Raises DRAFT_NOT_FOUND or DRAFT_NOT_OPEN; returns the number of '
    'tasks inserted.';

-- Only the backend (service_role) and signed-in users may call it. Functions are
-- executable by PUBLIC by default, and Supabase also grants anon explicitly.
revoke execute on function public.accept_draft(uuid, text, jsonb) from public, anon;
grant execute on function public.accept_draft(uuid, text, jsonb) to service_role, authenticated;
