-- Migration 011: Phase 1 Architecture Reset — Missing Tables
-- Creates tables that are used by existing code but had no migration.
-- Date: 2026-03-29

-- ==========================================================
-- Draft Schedules (used by app/services/draft_store.py)
-- Supabase-backed draft persistence for schedule negotiation
-- ==========================================================

CREATE TABLE IF NOT EXISTS draft_schedules (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          TEXT NOT NULL,
    goal_id          TEXT,
    tasks            JSONB NOT NULL DEFAULT '[]'::jsonb,  -- Array of DraftTask objects
    horizon_start    TEXT NOT NULL,                        -- ISO datetime string
    status           TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'accepted', 'rejected', 'modified', 'expired')),
    rejection_reason TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ DEFAULT (now() + interval '24 hours')
);

CREATE INDEX IF NOT EXISTS idx_draft_schedules_user_status
    ON draft_schedules(user_id, status);

-- ==========================================================
-- User Preferences (used by app/services/user_preferences.py)
-- ==========================================================

CREATE TABLE IF NOT EXISTS user_preferences (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL UNIQUE,
    learning_style  TEXT DEFAULT 'interactive'
                        CHECK (learning_style IN ('watcher', 'reader', 'interactive')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_preferences_user
    ON user_preferences(user_id);

-- ==========================================================
-- Pending Action Items (used by app/api/v1/endpoints/drafts.py)
-- ==========================================================

CREATE TABLE IF NOT EXISTS pending_action_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    title           TEXT,
    content         TEXT NOT NULL,
    proposed_at     TIMESTAMPTZ DEFAULT now(),
    status          TEXT DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected', 'completed')),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pending_action_items_user
    ON pending_action_items(user_id, status);

-- ==========================================================
-- Task Completion Signals (used by app/api/v1/endpoints/tasks.py)
-- Captures quality ratings + completion data for future DKT/RL
-- ==========================================================

CREATE TABLE IF NOT EXISTS task_completion_signals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    signal_type     TEXT NOT NULL,    -- 'completed', 'skipped', 'quality_rating'
    quality         INTEGER,          -- 0-5 quality rating
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_completion_signals_user
    ON task_completion_signals(user_id, task_id);

-- ==========================================================
-- Document Integration Tables (Phase 1C — for future use)
-- ==========================================================

CREATE TABLE IF NOT EXISTS extracted_problems (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    problem_number  INTEGER NOT NULL,
    problem_text    TEXT NOT NULL,
    topic_tags      TEXT[] NOT NULL DEFAULT '{}',
    difficulty      FLOAT DEFAULT 0.5,
    expected_time   INTEGER DEFAULT 10,
    has_solution    BOOLEAN DEFAULT false,
    solution_text   TEXT,
    linked_task_id  TEXT,
    status          TEXT DEFAULT 'pending'
                        CHECK (status IN ('pending', 'completed', 'skipped')),
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extracted_problems_task
    ON extracted_problems(user_id, linked_task_id);

CREATE TABLE IF NOT EXISTS task_completion_criteria (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    criteria_text   TEXT NOT NULL,
    source          TEXT DEFAULT 'decomposition'
                        CHECK (source IN ('decomposition', 'uploaded_document', 'assignment', 'user_edit')),
    source_id       TEXT,
    is_required     BOOLEAN DEFAULT true,
    is_completed    BOOLEAN DEFAULT false,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_completion_criteria_task
    ON task_completion_criteria(user_id, task_id);
