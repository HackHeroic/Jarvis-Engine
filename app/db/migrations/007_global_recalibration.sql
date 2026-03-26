-- Migration: Global Recalibration - status and full TaskChunk persistence
-- Enables get_all_pending_tasks to reconstruct TaskChunks for multi-goal fusion

-- Add status for pending/completed/cancelled
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'completed', 'cancelled'));

-- Full TaskChunk fields for retrieval and fusion
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS duration_minutes INT DEFAULT 25;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS difficulty_weight FLOAT DEFAULT 0.5;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS dependencies JSONB DEFAULT '[]';
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS deadline_hint TEXT;

CREATE INDEX IF NOT EXISTS idx_user_tasks_status ON user_tasks(user_id, status);
