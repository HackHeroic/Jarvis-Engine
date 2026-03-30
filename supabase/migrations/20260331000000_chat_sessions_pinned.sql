-- Migration: Add is_pinned to chat_sessions
-- Allows users to pin conversations so they appear at the top of the list

ALTER TABLE chat_sessions
ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_chat_sessions_pinned
    ON chat_sessions (user_id, is_pinned DESC, updated_at DESC);
