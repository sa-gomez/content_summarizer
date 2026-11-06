CREATE EXTENSION IF NOT EXISTS vector;

-- Raw source text: immutable, the irreplaceable data.
CREATE TABLE documents (
  id                BIGSERIAL PRIMARY KEY,
  source_type       TEXT NOT NULL,             -- youtube | podcast | ...
  external_id       TEXT NOT NULL,
  source_url        TEXT,
  title             TEXT,
  author            TEXT,
  published_at      TIMESTAMPTZ,
  language          TEXT,
  duration_s        INTEGER,
  transcript_method TEXT,                      -- youtube-captions | whisper-small | ...
  text              TEXT NOT NULL,
  content_hash      TEXT NOT NULL,
  metadata          JSONB NOT NULL DEFAULT '{}',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_type, external_id)
);
CREATE INDEX documents_metadata_gin ON documents USING gin (metadata);

-- Optional timestamped segments for deep links.
CREATE TABLE segments (
  document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal     INTEGER NOT NULL,
  start_s     REAL NOT NULL,
  text        TEXT NOT NULL,
  PRIMARY KEY (document_id, ordinal)
);

-- Derived, regenerable. Many per document.
CREATE TABLE summaries (
  id             BIGSERIAL PRIMARY KEY,
  document_id    BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  kind           TEXT NOT NULL,                -- micro | short | medium
  target_words   INTEGER,
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  text           TEXT NOT NULL,
  metadata       JSONB NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (document_id, kind, model, prompt_version)
);

-- Search units over documents and summaries (keyword + vector).
CREATE TABLE chunks (
  id          BIGSERIAL PRIMARY KEY,
  document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  summary_id  BIGINT REFERENCES summaries(id) ON DELETE CASCADE,  -- NULL = chunk of raw text
  ordinal     INTEGER NOT NULL,
  text        TEXT NOT NULL,
  embedding   vector(384),                     -- BAAI/bge-small-en-v1.5
  tsv         TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_document ON chunks (document_id);
CREATE INDEX chunks_summary ON chunks (summary_id);

-- Postgres-backed work queue.
CREATE TABLE jobs (
  id         BIGSERIAL PRIMARY KEY,
  type       TEXT NOT NULL,                    -- summarize | embed
  payload    JSONB NOT NULL,
  dedupe_key TEXT UNIQUE,
  status     TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  attempts   INTEGER NOT NULL DEFAULT 0,
  run_after  TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_by  TEXT,
  locked_at  TIMESTAMPTZ,
  error      TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim ON jobs (status, run_after);
