-- Conversations that remember (capstone plan 2, Task 9; decision 9).
--
-- A conversation keeps a running summary of its older turns: compaction folds them in
-- once more than 16 are unsummarized, and an idle conversation gets the rest folded in
-- too, for "since we last spoke". Written by ConversationRepository.set_summary.
--
--   summary             the summary text; NULL until the first one is written.
--   summary_through     created_at of the last message the summary covers: the history
--                       window starts after it. Set together with summary, always.
--   summary_updated_at  when the summary was last written.
--
-- updated_at stays the conversation's last activity. The capstone's set_updated_at
-- trigger stamped it on every update, so writing a summary would have read as activity:
-- the idle check would restart, and "since then" would count from the summary's write
-- instead of the last message. chat_sessions gets its own trigger function, which keeps
-- updated_at for a write that changes the summary columns and nothing else; any other
-- update (a new message bumping updated_at, a title) still stamps now().
--
-- Additive and idempotent: columns are added if missing, each CHECK is dropped and
-- re-created, the function and trigger are created or replaced, and the owner-only
-- policy (first set by 20261114000001) is restated the same way.

alter table public.chat_sessions add column if not exists summary text;
alter table public.chat_sessions add column if not exists summary_through timestamptz;
alter table public.chat_sessions add column if not exists summary_updated_at timestamptz;

alter table public.chat_sessions drop constraint if exists chat_sessions_summary_length_check;
alter table public.chat_sessions add constraint chat_sessions_summary_length_check
    check (summary is null or char_length(summary) <= 4000);

alter table public.chat_sessions drop constraint if exists chat_sessions_summary_through_check;
alter table public.chat_sessions add constraint chat_sessions_summary_through_check
    check ((summary is null) = (summary_through is null));

comment on column public.chat_sessions.summary is
    'Running summary of the conversation up to summary_through (at most 4000 characters).';
comment on column public.chat_sessions.summary_through is
    'created_at of the last message the summary covers; the history window starts after it.';
comment on column public.chat_sessions.summary_updated_at is
    'When the summary was last written. Writing it leaves updated_at (last activity) alone.';

create or replace function public.set_chat_session_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if (new.summary, new.summary_through, new.summary_updated_at)
           is distinct from (old.summary, old.summary_through, old.summary_updated_at)
       and (to_jsonb(new) - array['summary', 'summary_through', 'summary_updated_at', 'updated_at'])
           = (to_jsonb(old) - array['summary', 'summary_through', 'summary_updated_at', 'updated_at'])
    then
        -- The summary changed and nothing else did: not activity.
        new.updated_at := old.updated_at;
    else
        new.updated_at := now();
    end if;
    return new;
end $$;

create or replace trigger set_updated_at before update on public.chat_sessions
    for each row execute function public.set_chat_session_updated_at();

alter table public.chat_sessions enable row level security;
drop policy if exists owner_all on public.chat_sessions;
create policy owner_all on public.chat_sessions for all
    using (user_id = (select auth.uid())::text) with check (user_id = (select auth.uid())::text);
