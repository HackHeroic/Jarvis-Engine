-- Capstone LLD schema.
--
-- Adds the user root (user_profiles) and the tables the domain layer persists
-- (goals, task_events, proactive_suggestions, llm_usage_events); ties every user
-- table to user_profiles with a cascading foreign key; tightens the legacy tables
-- with the CHECKs the domain entities already enforce; drops dead tables; enables
-- row-level security; and adds accept_draft(), the one-transaction draft accept.
--
-- Every statement is idempotent: a second run changes nothing and deletes nothing.
-- Rationale, the entity mapping and the full key/relationship list are in
-- docs/capstone/_schema_notes.md.

-- ============================================================================
-- 0. WIPE -- DESTRUCTIVE, FIRST APPLICATION ONLY
-- ============================================================================
-- Owner decision (2026-11-14): every row in the live database is test data, so the
-- database is wiped and reseeded instead of cleaned row by row. That is why the
-- constraints below are added plainly, with no NOT VALID, no per-rule cleanup and
-- no backfill.
--
-- Guard: user_profiles is created by this migration. If it already exists, the
-- migration has run before and real rows may exist, so nothing is truncated.
do $$
declare
    v_tables text;
begin
    if to_regclass('public.user_profiles') is not null then
        raise notice 'capstone wipe skipped: user_profiles exists, so this migration already ran';
        return;
    end if;

    select string_agg(format('public.%I', t), ', ')
      into v_tables
      from unnest(array[
               'user_tasks', 'draft_schedules', 'behavioral_constraints', 'habit_trackers',
               'user_memories', 'chat_sessions', 'chat_messages', 'task_materials',
               'ingested_documents', 'pending_calendar_updates', 'user_plan_updates',
               'user_preferences', 'task_workspaces', 'task_completion_signals'
           ]) as t
     where to_regclass(format('public.%I', t)) is not null;

    if v_tables is not null then
        execute 'truncate table ' || v_tables || ' restart identity cascade';
    end if;
end $$;

-- ============================================================================
-- 1. New tables
-- ============================================================================

-- The user root. Every other user table references it (section 5).
create table if not exists public.user_profiles (
    user_id       text primary key,
    display_name  text,
    timezone      text not null default 'Asia/Kolkata',
    wake_time     time not null default '07:00',
    autonomy_mode text not null default 'ask_first'
                  constraint user_profiles_autonomy_mode_check
                  check (autonomy_mode in ('suggest_only', 'ask_first', 'auto_low_risk')),
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint user_profiles_user_id_not_blank check (btrim(user_id) <> '')
);

comment on table public.user_profiles is
    'One row per user (entity UserProfile). Rows are created on demand by the '
    'ensure_user_profile trigger on every user table.';

-- Goals are identified per user: two users may both have a goal called "thesis".
create table if not exists public.goals (
    user_id    text not null
               constraint goals_user_id_fkey
               references public.user_profiles (user_id) on delete cascade,
    id         text not null,
    title      text not null,
    deadline   timestamptz,
    status     text not null default 'active',
    created_at timestamptz not null default now(),
    primary key (user_id, id)
);

comment on table public.goals is
    'What a user works towards (entity Goal). Tasks, drafts and deadline updates '
    'reference it by (user_id, goal_id); the ensure_goal trigger creates a row with '
    'title = goal_id the first time a goal_id appears.';

-- Append-only task history. task_id is a soft reference to user_tasks(user_id, task_id):
-- accepting a draft deletes and re-inserts pending tasks, and the history must survive that.
create table if not exists public.task_events (
    id              uuid primary key default gen_random_uuid(),
    user_id         text not null
                    constraint task_events_user_id_fkey
                    references public.user_profiles (user_id) on delete cascade,
    task_id         text not null,
    event_type      text not null
                    constraint task_events_event_type_check
                    check (event_type in ('scheduled', 'completed', 'skipped', 'missed',
                                          'rescheduled', 'deadline_missed')),
    occurred_at     timestamptz not null default now(),
    scheduled_start timestamptz,
    scheduled_end   timestamptz,
    metadata        jsonb not null default '{}'
);

create index if not exists idx_task_events_user_occurred
    on public.task_events (user_id, occurred_at desc);
create index if not exists idx_task_events_user_task
    on public.task_events (user_id, task_id);

comment on table public.task_events is
    'Append-only record of what happened to a task (entity TaskEvent).';

create table if not exists public.proactive_suggestions (
    id               uuid primary key default gen_random_uuid(),
    user_id          text not null
                     constraint proactive_suggestions_user_id_fkey
                     references public.user_profiles (user_id) on delete cascade,
    trigger          text not null,
    kind             text not null,
    title            text not null,
    body             text not null,
    action           jsonb not null default '{}',
    status           text not null default 'pending'
                     constraint proactive_suggestions_status_check
                     check (status in ('pending', 'accepted', 'dismissed', 'expired', 'auto_applied')),
    consent_behavior text not null
                     constraint proactive_suggestions_consent_behavior_check
                     check (consent_behavior in ('allow', 'ask', 'deny')),
    consent_reason   text not null default '',
    created_at       timestamptz not null default now(),
    expires_at       timestamptz,
    resolved_at      timestamptz
);

create index if not exists idx_proactive_suggestions_user_status_created
    on public.proactive_suggestions (user_id, status, created_at desc);

comment on table public.proactive_suggestions is
    'Proposals Jarvis makes on its own, with the consent decision that governs them '
    '(entity Suggestion).';

-- user_id is optional: some calls serve no user. Deleting a user keeps the cost
-- record but forgets whose it was.
create table if not exists public.llm_usage_events (
    id                uuid primary key default gen_random_uuid(),
    user_id           text
                      constraint llm_usage_events_user_id_fkey
                      references public.user_profiles (user_id) on delete set null,
    session_id        text,
    task              text not null,
    role              text not null,
    provider          text not null,
    model             text not null,
    location          text not null
                      constraint llm_usage_events_location_check
                      check (location in ('local', 'cloud')),
    input_tokens      int not null default 0,
    output_tokens     int not null default 0,
    cache_read_tokens int not null default 0,
    latency_ms        int not null default 0,
    pii_redactions    int not null default 0,
    created_at        timestamptz not null default now()
);

create index if not exists idx_llm_usage_events_user_created
    on public.llm_usage_events (user_id, created_at desc);

comment on table public.llm_usage_events is
    'One completed language-model call, for cost and privacy accounting (entity LLMUsageEvent).';

-- ============================================================================
-- 2. Columns on legacy tables
-- ============================================================================

-- BehavioralConstraint.source: whether the user said it or Jarvis inferred it.
alter table public.behavioral_constraints
    add column if not exists source text not null default 'said';

-- DraftSchedule.horizon_start is optional. It stays TEXT: the app writes an ISO
-- string and parses the same string back (build_task_rows); timestamptz would hand
-- back a reformatted value and read a naive one as UTC.
alter table public.draft_schedules alter column horizon_start drop not null;
alter table public.draft_schedules add column if not exists expires_at timestamptz;

-- The ingestion pipeline writes s3_key when the S3 upload succeeds; no migration had it.
alter table public.ingested_documents add column if not exists s3_key text;

-- '' is not a user. Without this default an insert that omits user_id fails NOT NULL
-- instead of creating a blank profile.
alter table public.pending_calendar_updates alter column user_id drop default;

-- ============================================================================
-- 3. Dead tables
-- ============================================================================
-- No code in app/ reads or writes any of these (grep evidence in _schema_notes.md).
drop table if exists
    public.pending_action_items,
    public.extracted_problems,
    public.task_completion_criteria,
    public.user_calendar_anchors,
    public.conversation_messages,
    public.conversation_sessions
    cascade;

-- ============================================================================
-- 4. Trigger functions
-- ============================================================================

-- Creates the user's profile the first time any of their rows is inserted, so the
-- user_id foreign keys hold without every legacy insert path creating a profile first.
create or replace function public.ensure_user_profile()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.user_id is not null then
        insert into public.user_profiles (user_id) values (new.user_id)
        on conflict do nothing;
    end if;
    return new;
end $$;

-- Same idea for goals: the first row that names a goal_id creates the goal, titled
-- with its id. goals has its own ensure_user_profile trigger, so the order in which
-- the two triggers fire on one table does not matter.
create or replace function public.ensure_goal()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.goal_id is not null then
        insert into public.goals (user_id, id, title) values (new.user_id, new.goal_id, new.goal_id)
        on conflict do nothing;
    end if;
    return new;
end $$;

-- The ingestion pipeline links a document to tasks before it records the document,
-- so a task_materials row may arrive first. This creates the document row it points
-- to; the pipeline's upsert on (user_id, source_id) then fills in the details.
create or replace function public.ensure_ingested_document()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    insert into public.ingested_documents (user_id, source_id) values (new.user_id, new.source_id)
    on conflict do nothing;
    return new;
end $$;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    new.updated_at := now();
    return new;
end $$;

-- ============================================================================
-- 5. Every user table belongs to a user_profiles row
-- ============================================================================
-- For each table: user_id NOT NULL, a cascading FK to user_profiles, and the
-- ensure_user_profile trigger. The new tables declare their FK inline (section 1).

alter table public.user_tasks alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_tasks'::regclass
                      and conname = 'user_tasks_user_id_fkey') then
        alter table public.user_tasks add constraint user_tasks_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.draft_schedules alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.draft_schedules'::regclass
                      and conname = 'draft_schedules_user_id_fkey') then
        alter table public.draft_schedules add constraint draft_schedules_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.behavioral_constraints alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.behavioral_constraints'::regclass
                      and conname = 'behavioral_constraints_user_id_fkey') then
        alter table public.behavioral_constraints add constraint behavioral_constraints_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.habit_trackers alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.habit_trackers'::regclass
                      and conname = 'habit_trackers_user_id_fkey') then
        alter table public.habit_trackers add constraint habit_trackers_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.user_memories alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_memories'::regclass
                      and conname = 'user_memories_user_id_fkey') then
        alter table public.user_memories add constraint user_memories_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.chat_sessions alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.chat_sessions'::regclass
                      and conname = 'chat_sessions_user_id_fkey') then
        alter table public.chat_sessions add constraint chat_sessions_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.chat_messages alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.chat_messages'::regclass
                      and conname = 'chat_messages_user_id_fkey') then
        alter table public.chat_messages add constraint chat_messages_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.task_materials alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.task_materials'::regclass
                      and conname = 'task_materials_user_id_fkey') then
        alter table public.task_materials add constraint task_materials_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.ingested_documents alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.ingested_documents'::regclass
                      and conname = 'ingested_documents_user_id_fkey') then
        alter table public.ingested_documents add constraint ingested_documents_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.pending_calendar_updates alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.pending_calendar_updates'::regclass
                      and conname = 'pending_calendar_updates_user_id_fkey') then
        alter table public.pending_calendar_updates add constraint pending_calendar_updates_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.user_plan_updates alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_plan_updates'::regclass
                      and conname = 'user_plan_updates_user_id_fkey') then
        alter table public.user_plan_updates add constraint user_plan_updates_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.user_preferences alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_preferences'::regclass
                      and conname = 'user_preferences_user_id_fkey') then
        alter table public.user_preferences add constraint user_preferences_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.task_workspaces alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.task_workspaces'::regclass
                      and conname = 'task_workspaces_user_id_fkey') then
        alter table public.task_workspaces add constraint task_workspaces_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

alter table public.task_completion_signals alter column user_id set not null;
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.task_completion_signals'::regclass
                      and conname = 'task_completion_signals_user_id_fkey') then
        alter table public.task_completion_signals add constraint task_completion_signals_user_id_fkey
            foreign key (user_id) references public.user_profiles (user_id) on delete cascade;
    end if;
end $$;

create or replace trigger ensure_user_profile before insert on public.goals
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.task_events
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.proactive_suggestions
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.llm_usage_events
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.user_tasks
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.draft_schedules
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.behavioral_constraints
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.habit_trackers
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.user_memories
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.chat_sessions
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.chat_messages
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.task_materials
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.ingested_documents
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.pending_calendar_updates
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.user_plan_updates
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.user_preferences
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.task_workspaces
    for each row execute function public.ensure_user_profile();
create or replace trigger ensure_user_profile before insert on public.task_completion_signals
    for each row execute function public.ensure_user_profile();

-- ============================================================================
-- 6. goal_id columns reference goals
-- ============================================================================
-- A goal_id is optional, so deleting a goal clears it on the rows that named it
-- (ON DELETE SET NULL (goal_id), Postgres 15+) and keeps the rows.

do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_tasks'::regclass
                      and conname = 'user_tasks_goal_fkey') then
        alter table public.user_tasks add constraint user_tasks_goal_fkey
            foreign key (user_id, goal_id) references public.goals (user_id, id)
            on delete set null (goal_id);
    end if;
end $$;

do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.draft_schedules'::regclass
                      and conname = 'draft_schedules_goal_fkey') then
        alter table public.draft_schedules add constraint draft_schedules_goal_fkey
            foreign key (user_id, goal_id) references public.goals (user_id, id)
            on delete set null (goal_id);
    end if;
end $$;

do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_plan_updates'::regclass
                      and conname = 'user_plan_updates_goal_fkey') then
        alter table public.user_plan_updates add constraint user_plan_updates_goal_fkey
            foreign key (user_id, goal_id) references public.goals (user_id, id)
            on delete set null (goal_id);
    end if;
end $$;

create or replace trigger ensure_goal before insert on public.user_tasks
    for each row execute function public.ensure_goal();
create or replace trigger ensure_goal before insert on public.draft_schedules
    for each row execute function public.ensure_goal();
create or replace trigger ensure_goal before insert on public.user_plan_updates
    for each row execute function public.ensure_goal();

create index if not exists idx_user_tasks_user_goal on public.user_tasks (user_id, goal_id);
create index if not exists idx_draft_schedules_user_goal on public.draft_schedules (user_id, goal_id);
-- user_plan_updates (user_id, goal_id) is already indexed by idx_user_plan_updates_goal.

-- ============================================================================
-- 7. user_tasks
-- ============================================================================

-- task_id identifies a task within a user: every endpoint addresses it that way.
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_tasks'::regclass
                      and conname = 'user_tasks_user_id_task_id_key') then
        alter table public.user_tasks add constraint user_tasks_user_id_task_id_key
            unique (user_id, task_id);
    end if;
end $$;

-- Every insert path (build_task_rows, via accept_draft or _persist_fused_tasks)
-- supplies both from a validated TaskChunk, and the Task entity requires both.
alter table public.user_tasks
    alter column duration_minutes set default 25,
    alter column duration_minutes set not null,
    alter column difficulty_weight set default 0.5,
    alter column difficulty_weight set not null;

do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_tasks'::regclass
                      and conname = 'user_tasks_duration_minutes_positive') then
        alter table public.user_tasks add constraint user_tasks_duration_minutes_positive
            check (duration_minutes > 0);
    end if;
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_tasks'::regclass
                      and conname = 'user_tasks_difficulty_weight_range') then
        alter table public.user_tasks add constraint user_tasks_difficulty_weight_range
            check (difficulty_weight between 0 and 1);
    end if;
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_tasks'::regclass
                      and conname = 'user_tasks_scheduled_window_check') then
        alter table public.user_tasks add constraint user_tasks_scheduled_window_check
            check (scheduled_end is null or scheduled_start is null or scheduled_end > scheduled_start);
    end if;
end $$;

-- SupabaseTaskRepository.list_scheduled_between filters on (user_id, scheduled_start)
-- across every status, so the partial pending-only idx_user_tasks_scheduled cannot
-- serve it.
create index if not exists idx_user_tasks_user_scheduled_start
    on public.user_tasks (user_id, scheduled_start);

-- TaskStatus plus pacing_pushed, which the Skip endpoint still writes until plan 2
-- migrates it (the repositories read it as skipped). in_progress, cancelled and
-- rolled_forward are gone: no code in app/ writes them.
alter table public.user_tasks drop constraint if exists user_tasks_status_check;
alter table public.user_tasks add constraint user_tasks_status_check
    check (status in ('pending', 'completed', 'skipped', 'missed', 'pacing_pushed'));

-- ============================================================================
-- 8. Range and status CHECKs on the other legacy tables
-- ============================================================================

do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_memories'::regclass
                      and conname = 'user_memories_confidence_range') then
        alter table public.user_memories add constraint user_memories_confidence_range
            check (confidence between 0 and 1);
    end if;
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_memories'::regclass
                      and conname = 'user_memories_strength_range') then
        alter table public.user_memories add constraint user_memories_strength_range
            check (strength between 0 and 1);
    end if;
    -- SM-2's ease-factor floor (sm2_engine clamps to it).
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.habit_trackers'::regclass
                      and conname = 'habit_trackers_ef_floor') then
        alter table public.habit_trackers add constraint habit_trackers_ef_floor
            check (ef >= 1.3);
    end if;
end $$;

alter table public.behavioral_constraints drop constraint if exists behavioral_constraints_source_check;
alter table public.behavioral_constraints add constraint behavioral_constraints_source_check
    check (source in ('said', 'inferred'));

-- DraftStatus, including superseded.
alter table public.draft_schedules drop constraint if exists draft_schedules_status_check;
alter table public.draft_schedules add constraint draft_schedules_status_check
    check (status in ('pending', 'modified', 'accepted', 'rejected', 'expired', 'superseded'));

-- ============================================================================
-- 9. task_materials link real documents
-- ============================================================================

-- The key the FK points at. 008 declares it inline; this only matters on a database
-- where the table was created some other way.
do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.ingested_documents'::regclass
                      and conname = 'ingested_documents_user_id_source_id_key') then
        alter table public.ingested_documents add constraint ingested_documents_user_id_source_id_key
            unique (user_id, source_id);
    end if;
end $$;

do $$ begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.task_materials'::regclass
                      and conname = 'task_materials_document_fkey') then
        alter table public.task_materials add constraint task_materials_document_fkey
            foreign key (user_id, source_id) references public.ingested_documents (user_id, source_id)
            on delete cascade;
    end if;
end $$;

create or replace trigger ensure_ingested_document before insert on public.task_materials
    for each row execute function public.ensure_ingested_document();

create index if not exists idx_task_materials_user_source
    on public.task_materials (user_id, source_id);

-- ============================================================================
-- 10. updated_at maintenance (only tables that already have the column)
-- ============================================================================

create or replace trigger set_updated_at before update on public.user_profiles
    for each row execute function public.set_updated_at();
create or replace trigger set_updated_at before update on public.user_preferences
    for each row execute function public.set_updated_at();
create or replace trigger set_updated_at before update on public.user_memories
    for each row execute function public.set_updated_at();
create or replace trigger set_updated_at before update on public.habit_trackers
    for each row execute function public.set_updated_at();
create or replace trigger set_updated_at before update on public.chat_sessions
    for each row execute function public.set_updated_at();

-- ============================================================================
-- 11. Row-level security
-- ============================================================================
-- A signed-in user sees and writes only their own rows. The service role bypasses
-- RLS, so a backend using the SECRET key is unaffected; a backend using the
-- publishable (anon) key would see nothing. (select auth.uid()) is evaluated once
-- per statement rather than once per row.

alter table public.user_profiles enable row level security;
drop policy if exists owner_all on public.user_profiles;
create policy owner_all on public.user_profiles for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.goals enable row level security;
drop policy if exists owner_all on public.goals;
create policy owner_all on public.goals for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.task_events enable row level security;
drop policy if exists owner_all on public.task_events;
create policy owner_all on public.task_events for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.proactive_suggestions enable row level security;
drop policy if exists owner_all on public.proactive_suggestions;
create policy owner_all on public.proactive_suggestions for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.llm_usage_events enable row level security;
drop policy if exists owner_all on public.llm_usage_events;
create policy owner_all on public.llm_usage_events for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.user_tasks enable row level security;
drop policy if exists owner_all on public.user_tasks;
create policy owner_all on public.user_tasks for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.draft_schedules enable row level security;
drop policy if exists owner_all on public.draft_schedules;
create policy owner_all on public.draft_schedules for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.behavioral_constraints enable row level security;
drop policy if exists owner_all on public.behavioral_constraints;
create policy owner_all on public.behavioral_constraints for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.habit_trackers enable row level security;
drop policy if exists owner_all on public.habit_trackers;
create policy owner_all on public.habit_trackers for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.user_memories enable row level security;
drop policy if exists owner_all on public.user_memories;
create policy owner_all on public.user_memories for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.chat_sessions enable row level security;
drop policy if exists owner_all on public.chat_sessions;
create policy owner_all on public.chat_sessions for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.chat_messages enable row level security;
drop policy if exists owner_all on public.chat_messages;
create policy owner_all on public.chat_messages for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.task_materials enable row level security;
drop policy if exists owner_all on public.task_materials;
create policy owner_all on public.task_materials for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.ingested_documents enable row level security;
drop policy if exists owner_all on public.ingested_documents;
create policy owner_all on public.ingested_documents for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.pending_calendar_updates enable row level security;
drop policy if exists owner_all on public.pending_calendar_updates;
create policy owner_all on public.pending_calendar_updates for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.user_plan_updates enable row level security;
drop policy if exists owner_all on public.user_plan_updates;
create policy owner_all on public.user_plan_updates for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.user_preferences enable row level security;
drop policy if exists owner_all on public.user_preferences;
create policy owner_all on public.user_preferences for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.task_workspaces enable row level security;
drop policy if exists owner_all on public.task_workspaces;
create policy owner_all on public.task_workspaces for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

alter table public.task_completion_signals enable row level security;
drop policy if exists owner_all on public.task_completion_signals;
create policy owner_all on public.task_completion_signals for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);

-- ============================================================================
-- 12. accept_draft: accept a draft and write its tasks in one transaction
-- ============================================================================
-- Called by SupabaseDraftRepository.accept_atomic; tests/fakes.py mirrors it.
-- Accepts for one user run one at a time, so of two concurrent accepts exactly one
-- succeeds and the other raises DRAFT_NOT_OPEN, whether they target the same draft
-- or two different ones. Replaces the user's pending tasks (completed, skipped and
-- missed ones stay), inserts p_rows, marks the draft accepted and the user's other
-- open drafts superseded, and returns how many tasks it inserted. Any error rolls
-- all of it back.
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
        'scheduled_start', 'scheduled_end', 'created_at'
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

    delete from public.user_tasks where user_id = p_user_id and status = 'pending';

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
    'Accept an open draft and replace the user''s pending tasks with p_rows, atomically; '
    'the user''s other open drafts become superseded. Raises DRAFT_NOT_FOUND or '
    'DRAFT_NOT_OPEN; returns the number of tasks inserted.';

-- Only the backend (service_role) and signed-in users may call it. Functions are
-- executable by PUBLIC by default, and Supabase also grants anon explicitly.
revoke execute on function public.accept_draft(uuid, text, jsonb) from public, anon;
grant execute on function public.accept_draft(uuid, text, jsonb) to service_role, authenticated;
