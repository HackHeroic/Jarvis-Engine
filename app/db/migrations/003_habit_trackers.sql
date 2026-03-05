-- Migration: SM-2 habit trackers (Recurring Habits Plan)
-- Tracks spaced repetition state: repetitions, EF, next_interval_days, last_done_at

CREATE TABLE IF NOT EXISTS habit_trackers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    habit_id TEXT,
    constraint_id UUID REFERENCES behavioral_constraints(id) ON DELETE SET NULL,
    repetitions INT NOT NULL DEFAULT 0,
    quality_last INT,
    ef DECIMAL(5,3) NOT NULL DEFAULT 2.5,
    next_interval_days INT NOT NULL DEFAULT 1,
    last_done_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_habit_trackers_user
    ON habit_trackers (user_id);

CREATE INDEX IF NOT EXISTS idx_habit_trackers_last_done
    ON habit_trackers (user_id, last_done_at);
