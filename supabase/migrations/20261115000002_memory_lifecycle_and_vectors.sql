-- Memory becomes a domain entity with a lifecycle, searched with pgvector
-- (capstone plan 2, Task 4; decisions 1 and 2).
--
-- user_memories gains the Memory entity's lifecycle columns (status, provenance,
-- pattern_key, the session and message it was learned from, when it was last
-- confirmed, the instant its strength was measured at); its embedding becomes a
-- pgvector column; and match_memories() filters by user, status and type and ranks
-- by cosine similarity in one query. The entity's origin stays in the legacy
-- `source` column, which the v1 memory path still writes.
--
-- Apply together with the code that stops reading `embedding` as a float array
-- (MemoryStore.find_similar_memory) -- see docs/capstone/_schema_notes.md.
--
-- Every statement is idempotent and additive: a second run changes nothing.

-- ============================================================================
-- 1. pgvector
-- ============================================================================

create extension if not exists vector with schema extensions;

-- ============================================================================
-- 2. Lifecycle columns
-- ============================================================================

alter table public.user_memories
    add column if not exists status text not null default 'active',
    add column if not exists provenance text not null default 'said',
    add column if not exists pattern_key text,
    add column if not exists source_session_id uuid
        references public.chat_sessions (id) on delete set null,
    add column if not exists source_message_id uuid
        references public.chat_messages (id) on delete set null,
    add column if not exists last_confirmed_at timestamptz,
    add column if not exists strength_as_of timestamptz not null default now();

-- MemoryStatus.
alter table public.user_memories drop constraint if exists user_memories_status_check;
alter table public.user_memories add constraint user_memories_status_check
    check (status in ('active', 'dismissed', 'forgotten', 'faded'));

-- MemorySource: the user said it, or Jarvis inferred it from behaviour.
alter table public.user_memories drop constraint if exists user_memories_provenance_check;
alter table public.user_memories add constraint user_memories_provenance_check
    check (provenance in ('said', 'inferred'));

-- ============================================================================
-- 3. Provenance of the rows already stored
-- ============================================================================
-- A memory PEARL wrote from behaviour was inferred; every other source is the
-- user's own words. The guard keeps a re-run from touching a row (and bumping its
-- updated_at through the set_updated_at trigger).

update public.user_memories set provenance = 'inferred'
 where source = 'behavior' and provenance <> 'inferred';

-- ============================================================================
-- 4. Range CHECKs the entity enforces
-- ============================================================================
-- Rows the legacy path wrote are brought inside the ranges first, so the CHECKs can
-- be added over them; a row already inside is never touched.

update public.user_memories set observation_count = 1 where observation_count < 1;
update public.user_memories set stability = least(greatest(stability, 0.01), 20)
 where stability < 0.01 or stability > 20;

alter table public.user_memories drop constraint if exists user_memories_observation_count_check;
alter table public.user_memories add constraint user_memories_observation_count_check
    check (observation_count >= 1);

alter table public.user_memories drop constraint if exists user_memories_stability_range;
alter table public.user_memories add constraint user_memories_stability_range
    check (stability between 0.01 and 20);

-- ============================================================================
-- 5. embedding becomes vector(384)
-- ============================================================================
-- all-MiniLM-L6-v2 vectors are 384 long. An array of any other length cannot be
-- compared with them, so it becomes NULL (the memory is kept; it is just not found
-- by similarity until it is embedded again). Runs only while the column is still an
-- array: on a vector, array_length() does not exist.

do $$ begin
    if exists (select 1 from information_schema.columns
                where table_schema = 'public'
                  and table_name = 'user_memories'
                  and column_name = 'embedding'
                  and data_type = 'ARRAY') then
        alter table public.user_memories
            alter column embedding type extensions.vector(384)
            using case
                when array_length(embedding, 1) = 384
                then embedding::extensions.vector(384)
            end;
    end if;
end $$;

-- ============================================================================
-- 6. Indexes
-- ============================================================================

-- One memory per fact per user: the same fact said or observed again is found by
-- its key, whatever its status (a forgotten tombstone keeps its key).
create unique index if not exists idx_user_memories_user_pattern_key
    on public.user_memories (user_id, pattern_key)
    where pattern_key is not null;

create index if not exists idx_user_memories_user_status
    on public.user_memories (user_id, status);

-- No ANN index: a user holds tens to hundreds of memories, scanned exactly behind
-- the user_id filter.

-- ============================================================================
-- 7. match_memories: similarity search in one filtered query
-- ============================================================================
-- Called by SupabaseMemoryRepository.nearest; tests/fakes.py mirrors it. Returns the
-- user's memories in p_statuses (and p_types; NULL means any type) that have an
-- embedding and are not superseded, scoring at least p_min_similarity, most similar
-- first, at most p_limit. Every column but the embedding comes back.

create or replace function public.match_memories(
    p_user_id text,
    p_query extensions.vector(384),
    p_statuses text[],
    p_types text[],
    p_limit int,
    p_min_similarity double precision
)
returns table (
    id uuid,
    user_id text,
    memory_type text,
    content text,
    source text,
    source_id uuid,
    confidence double precision,
    strength double precision,
    stability double precision,
    last_accessed timestamptz,
    last_reinforced timestamptz,
    superseded_by uuid,
    expires_at timestamptz,
    observation_count integer,
    applied_as text,
    created_at timestamptz,
    updated_at timestamptz,
    status text,
    provenance text,
    pattern_key text,
    source_session_id uuid,
    source_message_id uuid,
    last_confirmed_at timestamptz,
    strength_as_of timestamptz,
    similarity double precision
)
language sql stable
set search_path = public, extensions
as $$
    select m.id, m.user_id, m.memory_type, m.content, m.source, m.source_id,
           m.confidence, m.strength, m.stability, m.last_accessed, m.last_reinforced,
           m.superseded_by, m.expires_at, m.observation_count, m.applied_as,
           m.created_at, m.updated_at, m.status, m.provenance, m.pattern_key,
           m.source_session_id, m.source_message_id, m.last_confirmed_at,
           m.strength_as_of,
           1 - (m.embedding <=> p_query) as similarity
      from public.user_memories m
     where m.user_id = p_user_id
       and m.status = any (p_statuses)
       and (p_types is null or m.memory_type = any (p_types))
       and m.embedding is not null
       and m.superseded_by is null
       and 1 - (m.embedding <=> p_query) >= p_min_similarity
     order by m.embedding <=> p_query
     limit p_limit;
$$;

comment on function public.match_memories(text, extensions.vector(384), text[], text[], int, double precision) is
    'The user''s memories in p_statuses (and p_types, NULL = any) most similar to p_query: '
    'embedded, not superseded, similarity >= p_min_similarity, most similar first, at most '
    'p_limit; every column but embedding, plus similarity (1 - cosine distance).';

-- ============================================================================
-- 8. Privileges
-- ============================================================================
-- Only the backend (service_role) and signed-in users may call it. Functions are
-- executable by PUBLIC by default, and Supabase also grants anon explicitly.

revoke execute on function public.match_memories(text, extensions.vector(384), text[], text[], int, double precision) from public, anon;
grant execute on function public.match_memories(text, extensions.vector(384), text[], text[], int, double precision) to service_role, authenticated;
