-- Migration: User tasks and task-material linking (Recurring Habits Plan)
-- user_tasks: persisted decomposition from PLAN_DAY
-- task_materials: links documents to tasks via embedding similarity

CREATE TABLE IF NOT EXISTS user_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    plan_id TEXT,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    topic_keywords TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_tasks_user_plan
    ON user_tasks (user_id, plan_id);

CREATE TABLE IF NOT EXISTS task_materials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    document_topics TEXT[] DEFAULT '{}',
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, task_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_task_materials_user_task
    ON task_materials (user_id, task_id);
