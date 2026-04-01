-- Migration: Apply missing user_tasks columns (007 + 013)
-- Fixes: "column user_tasks.status does not exist" error

-- From migration 007: status and TaskChunk fields
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS duration_minutes INT DEFAULT 25;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS difficulty_weight FLOAT DEFAULT 0.5;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS dependencies JSONB DEFAULT '[]';
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS deadline_hint TEXT;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS goal_id TEXT;

-- From migration 013: schedule timing fields
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS scheduled_start TIMESTAMPTZ;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS scheduled_end TIMESTAMPTZ;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS completion_criteria TEXT;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS implementation_intention JSONB;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS topic_keywords TEXT[] DEFAULT '{}';

-- Add check constraint (allow all statuses the code uses)
-- Drop old constraint if exists, then add new one
DO $$
BEGIN
    ALTER TABLE user_tasks DROP CONSTRAINT IF EXISTS user_tasks_status_check;
    ALTER TABLE user_tasks ADD CONSTRAINT user_tasks_status_check
        CHECK (status IN ('pending', 'in_progress', 'completed', 'skipped', 'cancelled', 'rolled_forward'));
EXCEPTION WHEN OTHERS THEN
    NULL;
END $$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_user_tasks_status ON user_tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_user_tasks_scheduled ON user_tasks(user_id, scheduled_start) WHERE status = 'pending';
