-- Migration: Goal-level deadline storage and user_tasks goal_id
-- user_plan_updates: stores deadlines from ingestion, chat, action items (linked by goal_id)
-- user_tasks: add goal_id for task-goal linkage when ingesting materials later

-- Goal-level deadline/context updates (schedules ephemeral, goals persistent)
CREATE TABLE IF NOT EXISTS user_plan_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    goal_id TEXT,
    source TEXT NOT NULL,
    deadline_date DATE,
    deadline_raw TEXT,
    context_snippet TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_plan_updates_user ON user_plan_updates(user_id);
CREATE INDEX IF NOT EXISTS idx_user_plan_updates_goal ON user_plan_updates(user_id, goal_id);

-- Add goal_id to user_tasks for ingestion linkage
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS goal_id TEXT;
