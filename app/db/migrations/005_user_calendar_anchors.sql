-- Migration: User calendar anchors for validity placeholder resolution (Recurring Habits Plan)
-- Resolves placeholders like "finals", "semester_end" when exam schedules are uploaded

CREATE TABLE IF NOT EXISTS user_calendar_anchors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    anchor_key TEXT NOT NULL,
    resolved_date TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, anchor_key)
);

CREATE INDEX IF NOT EXISTS idx_user_calendar_anchors_user
    ON user_calendar_anchors (user_id);
