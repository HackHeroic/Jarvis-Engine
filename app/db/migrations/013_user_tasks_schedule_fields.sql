-- Migration 013: Add schedule timing and task metadata columns to user_tasks
-- These fields allow the frontend to display scheduled start/end times
-- and task completion criteria without re-computing from the OR-Tools output.

ALTER TABLE user_tasks
    ADD COLUMN IF NOT EXISTS scheduled_start TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS scheduled_end TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completion_criteria TEXT,
    ADD COLUMN IF NOT EXISTS implementation_intention JSONB,
    ADD COLUMN IF NOT EXISTS topic_keywords TEXT[] DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_user_tasks_scheduled
    ON user_tasks (user_id, scheduled_start)
    WHERE status = 'pending';
