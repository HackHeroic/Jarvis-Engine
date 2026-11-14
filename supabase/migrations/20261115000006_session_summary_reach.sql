-- How far a conversation's summary reaches (capstone plan 2, final fix wave, Y7).
--
-- summary_through is where the verbatim history window starts. An idle summary also
-- covers the newest turns the window still shows word for word, so the summary reaches
-- further than the window's start. Without a record of that reach every later summary
-- re-sent those turns to the model, and the daily rescan could not tell, from the row
-- alone, a conversation whose newest turns are in no summary yet (its last summary a
-- compaction written after its last message) from one fully summarised.
--
--   summary_covered_through  created_at of the newest message the summary covers:
--                            never before summary_through; NULL for a summary written
--                            before it was tracked. Written by
--                            ConversationRepository.set_summary with the other three.
--
-- Writing it is not activity, like the other summary columns: the chat_sessions
-- trigger function is restated with it among the columns a summary write may change
-- while updated_at (the last activity) is kept.
--
-- Additive and idempotent: the column is added if missing, the CHECK dropped and
-- re-created, the function and trigger created or replaced.

alter table public.chat_sessions add column if not exists summary_covered_through timestamptz;

alter table public.chat_sessions drop constraint if exists chat_sessions_summary_covered_through_check;
alter table public.chat_sessions add constraint chat_sessions_summary_covered_through_check
    check (summary_covered_through is null or (summary_through is not null and summary_covered_through >= summary_through));

comment on column public.chat_sessions.summary_covered_through is
    'created_at of the newest message the summary covers; never before summary_through.';

create or replace function public.set_chat_session_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if (new.summary, new.summary_through, new.summary_updated_at, new.summary_covered_through)
           is distinct from
       (old.summary, old.summary_through, old.summary_updated_at, old.summary_covered_through)
       and (to_jsonb(new) - array['summary', 'summary_through', 'summary_updated_at',
                                  'summary_covered_through', 'updated_at'])
           = (to_jsonb(old) - array['summary', 'summary_through', 'summary_updated_at',
                                    'summary_covered_through', 'updated_at'])
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
