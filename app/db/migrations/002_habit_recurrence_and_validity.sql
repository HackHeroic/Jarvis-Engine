-- Migration: Habit recurrence and validity (Recurring Habits Plan)
-- Extends behavioral_constraints with recurrence/validity; extends pending_calendar_updates with valid_until

ALTER TABLE behavioral_constraints
ADD COLUMN IF NOT EXISTS recurrence TEXT DEFAULT 'daily',
ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS structured_semantics JSONB DEFAULT '{}';

ALTER TABLE pending_calendar_updates
ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
