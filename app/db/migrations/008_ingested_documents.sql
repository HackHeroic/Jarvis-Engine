-- 008: Ingested Documents tracking table
-- Tracks all documents ingested into ChromaDB for document management UI.
-- source_id links to ChromaDB chunk metadata and task_materials table.

CREATE TABLE IF NOT EXISTS ingested_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  file_name TEXT,
  media_type TEXT,
  document_topics TEXT[] DEFAULT '{}',
  chunk_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (user_id, source_id)
);

-- Index for fast user-scoped queries
CREATE INDEX IF NOT EXISTS idx_ingested_documents_user_id ON ingested_documents (user_id);
