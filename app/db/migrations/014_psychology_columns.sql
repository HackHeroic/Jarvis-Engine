-- Migration 014: Add psychology framework columns to user_tasks
-- quality_score: SM-2 quality rating (0-5) on task completion
-- reschedule_count: number of times task was pushed (slippery deadlines)
-- completed_at: timestamp of completion (for streak/trend calculations)
-- Also extend status CHECK to include 'pacing_pushed' and 'skipped'

ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS quality_score INT;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS reschedule_count INT DEFAULT 0;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

-- Extend status constraint to allow pacing_pushed and skipped
-- Drop old constraint first (safe — IF EXISTS not supported, use DO block)
DO $$
BEGIN
    ALTER TABLE user_tasks DROP CONSTRAINT IF EXISTS user_tasks_status_check;
EXCEPTION WHEN undefined_object THEN
    NULL;
END $$;

ALTER TABLE user_tasks ADD CONSTRAINT user_tasks_status_check
    CHECK (status IN ('pending', 'completed', 'cancelled', 'pacing_pushed', 'skipped'));
