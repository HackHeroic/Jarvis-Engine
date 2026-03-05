-- Migration: Autonomous Extraction Pipeline tables
-- Run this in Supabase SQL Editor to create pending_calendar_updates and behavioral_constraints

-- Pending calendar updates (awaiting user approval)
CREATE TABLE IF NOT EXISTS pending_calendar_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT DEFAULT '',
    extracted_slots JSONB NOT NULL DEFAULT '[]',
    source_summary TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_calendar_user_status
    ON pending_calendar_updates (user_id, status);

-- Behavioral constraints (Strategy Hub L7)
CREATE TABLE IF NOT EXISTS behavioral_constraints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT,
    raw_text TEXT NOT NULL,
    constraint_type TEXT NOT NULL DEFAULT 'preference',
    structured_override JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_behavioral_user
    ON behavioral_constraints (user_id);
