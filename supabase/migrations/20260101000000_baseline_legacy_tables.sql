-- Baseline: the legacy tables that only the hand-run set (app/db/migrations/) ever created.
--
-- Ports app/db/migrations/001-004 and 006-008, plus the three user_tasks columns of
-- 014, into the CLI set so that `supabase/migrations/` alone builds a fresh database.
-- 005 (user_calendar_anchors) is left out on purpose: the capstone migration drops
-- that table as dead. Statements are idempotent, so on the live database, where the
-- hand-run files already created all of this, every one is a no-op.
--
-- Why this sorts FIRST (20260101000000) and not last-but-one: earlier CLI files
-- already ALTER user_tasks (20260401000000, 20260501000001, 20260808000000,
-- 20260813000000), so on a fresh database user_tasks must exist before them. Nothing
-- here references a table that a later file creates.
--
-- Column definitions are copied as the hand-run files wrote them; later migrations
-- (CLI files and the capstone) tighten them.

-- 001: timetable extractions awaiting approval; behavioural constraints (Strategy Hub L7)
CREATE TABLE IF NOT EXISTS public.pending_calendar_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT DEFAULT '',
    extracted_slots JSONB NOT NULL DEFAULT '[]',
    source_summary TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_calendar_user_status
    ON public.pending_calendar_updates (user_id, status);

CREATE TABLE IF NOT EXISTS public.behavioral_constraints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT,
    raw_text TEXT NOT NULL,
    constraint_type TEXT NOT NULL DEFAULT 'preference',
    structured_override JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_behavioral_user
    ON public.behavioral_constraints (user_id);

-- 002: habit recurrence and validity windows
ALTER TABLE public.behavioral_constraints
    ADD COLUMN IF NOT EXISTS recurrence TEXT DEFAULT 'daily',
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS structured_semantics JSONB DEFAULT '{}';

ALTER TABLE public.pending_calendar_updates
    ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;

-- 003: SM-2 habit trackers
CREATE TABLE IF NOT EXISTS public.habit_trackers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    habit_id TEXT,
    constraint_id UUID REFERENCES public.behavioral_constraints(id) ON DELETE SET NULL,
    repetitions INT NOT NULL DEFAULT 0,
    quality_last INT,
    ef DECIMAL(5,3) NOT NULL DEFAULT 2.5,
    next_interval_days INT NOT NULL DEFAULT 1,
    last_done_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_habit_trackers_user
    ON public.habit_trackers (user_id);

CREATE INDEX IF NOT EXISTS idx_habit_trackers_last_done
    ON public.habit_trackers (user_id, last_done_at);

-- 004: base user_tasks; document <-> task links
CREATE TABLE IF NOT EXISTS public.user_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    plan_id TEXT,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    topic_keywords TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_tasks_user_plan
    ON public.user_tasks (user_id, plan_id);

CREATE TABLE IF NOT EXISTS public.task_materials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    document_topics TEXT[] DEFAULT '{}',
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, task_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_task_materials_user_task
    ON public.task_materials (user_id, task_id);

-- 006: goal-level deadlines; user_tasks.goal_id
CREATE TABLE IF NOT EXISTS public.user_plan_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    goal_id TEXT,
    source TEXT NOT NULL,
    deadline_date DATE,
    deadline_raw TEXT,
    context_snippet TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_plan_updates_user ON public.user_plan_updates (user_id);
CREATE INDEX IF NOT EXISTS idx_user_plan_updates_goal ON public.user_plan_updates (user_id, goal_id);

ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS goal_id TEXT;

-- 007: status and the full TaskChunk fields. The status CHECK here is the original one;
-- 20260401000000, 20260813000000 and the capstone each replace it.
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'completed', 'cancelled'));
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS duration_minutes INT DEFAULT 25;
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS difficulty_weight FLOAT DEFAULT 0.5;
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS dependencies JSONB DEFAULT '[]';
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS deadline_hint TEXT;

CREATE INDEX IF NOT EXISTS idx_user_tasks_status ON public.user_tasks (user_id, status);

-- 008: ingested documents (source_id links ChromaDB chunks and task_materials)
CREATE TABLE IF NOT EXISTS public.ingested_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    file_name TEXT,
    media_type TEXT,
    document_topics TEXT[] DEFAULT '{}',
    chunk_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (user_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_ingested_documents_user_id ON public.ingested_documents (user_id);

-- 014 (columns only; its status CHECK is superseded): completion quality, push count,
-- completion time. No CLI file ever added these, yet the Skip endpoint writes
-- reschedule_count and mastery_tracker reads all three.
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS quality_score INT;
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS reschedule_count INT DEFAULT 0;
ALTER TABLE public.user_tasks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
