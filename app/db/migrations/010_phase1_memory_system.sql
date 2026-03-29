-- Migration 010: Phase 1 Architecture Reset — Memory System
-- Creates Tier 2 (Recall Memory) and Tier 3 (Archival Memory) tables
-- Date: 2026-03-29

-- ==========================================================
-- TIER 3: Archival Memory (Long-term semantic memory with SM-2 decay)
-- ==========================================================

CREATE TABLE IF NOT EXISTS user_memories (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           TEXT NOT NULL,
    memory_type       TEXT NOT NULL CHECK (memory_type IN (
                          'fact', 'preference', 'behavioral_pattern',
                          'temporal_event', 'goal', 'feedback', 'constraint'
                      )),
    content           TEXT NOT NULL,
    source            TEXT DEFAULT 'conversation',   -- 'conversation' | 'behavior' | 'ingestion' | 'contradiction'
    source_id         UUID,                          -- Links to conversation_messages.id or user_tasks.id

    -- SM-2 inspired scoring fields
    confidence        FLOAT DEFAULT 0.5,             -- 0.0 to 1.0, increases with reinforcement
    strength          FLOAT DEFAULT 1.0,             -- current memory strength (decays over time)
    stability         FLOAT DEFAULT 1.0,             -- reinforcement count (higher = slower decay, capped at 20)

    -- Lifecycle
    last_accessed     TIMESTAMPTZ DEFAULT now(),
    last_reinforced   TIMESTAMPTZ DEFAULT now(),
    superseded_by     UUID REFERENCES user_memories(id),  -- contradiction chain
    expires_at        TIMESTAMPTZ,                   -- for temporal_event type

    -- PEARL: for behavioral_pattern type
    observation_count INTEGER DEFAULT 1,
    applied_as        TEXT,                           -- 'hard_constraint' | 'soft_preference' | NULL

    -- Embedding (pre-computed at storage time for similarity search)
    embedding         FLOAT[],                       -- 384 dimensions (all-MiniLM-L6-v2)

    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- Index: fast lookup by user + type
CREATE INDEX IF NOT EXISTS idx_user_memories_user_type
    ON user_memories(user_id, memory_type);

-- Index: active memories only (not superseded, strength above threshold)
CREATE INDEX IF NOT EXISTS idx_user_memories_user_active
    ON user_memories(user_id)
    WHERE superseded_by IS NULL AND strength > 0.1;

-- ==========================================================
-- TIER 2: Recall Memory (Conversation sessions + messages)
-- These supplement the existing chat_sessions/chat_messages tables.
-- During Phase 1, both old and new tables coexist.
-- ==========================================================

CREATE TABLE IF NOT EXISTS conversation_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    started_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    summary         TEXT,                            -- LLM-generated 2-3 sentence summary
    goals_discussed TEXT[],                          -- extracted goal IDs referenced
    mood_signal     TEXT CHECK (mood_signal IS NULL OR mood_signal IN (
                        'positive', 'neutral', 'stressed', 'frustrated'
                    )),
    message_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_user
    ON conversation_sessions(user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    session_id      UUID NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    intent_detected TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
    ON conversation_messages(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_user
    ON conversation_messages(user_id, created_at DESC);
