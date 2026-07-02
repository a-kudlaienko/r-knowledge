-- 001_init.sql — initial schema for storage.mode = shared_postgresql.
--
-- Mirrors the SQLite schema in knowledge/db.py with these adaptations:
--   * BLOB embeddings → pgvector vector(384) in side tables
--   * FTS5 contentless + triggers → tsvector GENERATED column + GIN
--   * projects.root_path UNIQUE → partial unique on git_remote_normalized
--     IS NOT NULL OR root_path WHERE the remote is NULL (so the same repo
--     cloned at different paths on different laptops collapses to one row).
--
-- Idempotent: every CREATE uses IF NOT EXISTS. Re-running this script on
-- an existing database is a no-op.
--
-- See todo/01-postgresql-shared-mode.md for design rationale.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- meta: schema/version markers, embedding model name, etc.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- projects: one row per repo (canonical key = git_remote_normalized).
--
-- git_remote_normalized: derived from `git remote get-url origin` with
-- normalization (strip credentials, strip trailing .git, lowercase host,
-- normalize ssh -> https). NULL only when the project has no .git remote
-- (loose directory) — in that case root_path is the canonical key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id                      BIGSERIAL PRIMARY KEY,
    name                    TEXT      NOT NULL,
    root_path               TEXT      NOT NULL,
    git_remote              TEXT,
    git_remote_normalized   TEXT,
    created_at              DOUBLE PRECISION NOT NULL,
    last_build              DOUBLE PRECISION,
    last_update             DOUBLE PRECISION,
    file_count              INTEGER   NOT NULL DEFAULT 0,
    chunk_count             INTEGER   NOT NULL DEFAULT 0
);

-- Partial unique indexes — exactly one identifier strategy active per row.
-- Two laptops cloning the same git repo at different paths collapse onto
-- the same row via git_remote_normalized; truly path-only projects (no
-- .git) fall back to root_path uniqueness.
CREATE UNIQUE INDEX IF NOT EXISTS projects_git_uniq
    ON projects (git_remote_normalized)
    WHERE git_remote_normalized IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS projects_path_uniq
    ON projects (root_path)
    WHERE git_remote_normalized IS NULL;

-- ---------------------------------------------------------------------------
-- files
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS files (
    id            BIGSERIAL PRIMARY KEY,
    project_id    BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rel_path      TEXT      NOT NULL,
    content_hash  TEXT      NOT NULL,
    mtime         DOUBLE PRECISION NOT NULL,
    size          BIGINT    NOT NULL,
    lang          TEXT      NOT NULL,
    last_scanned  DOUBLE PRECISION NOT NULL,
    UNIQUE (project_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id);

-- ---------------------------------------------------------------------------
-- chunks: schema parity with SQLite, plus a generated tsvector column.
--
-- search_vector is the PG replacement for the FTS5 chunks_fts table. STORED
-- so the GIN index can be hit directly without recomputing on every read.
-- The 'english' configuration gives reasonable code-symbol tokenization;
-- swap to 'simple' if accent/stopword handling causes false negatives.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_id         BIGINT    NOT NULL REFERENCES files(id)    ON DELETE CASCADE,
    parent_id       BIGINT    REFERENCES chunks(id)            ON DELETE CASCADE,
    sibling_order   INTEGER,
    kind            TEXT      NOT NULL,
    name            TEXT,
    qualified_name  TEXT,
    start_line      INTEGER   NOT NULL,
    end_line        INTEGER   NOT NULL,
    start_byte      INTEGER   NOT NULL,
    end_byte        INTEGER   NOT NULL,
    char_count      INTEGER   NOT NULL,
    content_hash    TEXT      NOT NULL,
    stored_text     TEXT      NOT NULL,
    embedded_text   TEXT      NOT NULL,
    metadata        TEXT,
    search_vector   tsvector  GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(name, '')           || ' ' ||
            coalesce(qualified_name, '') || ' ' ||
            stored_text)
    ) STORED
);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id);
CREATE INDEX IF NOT EXISTS idx_chunks_file    ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_parent  ON chunks(parent_id, sibling_order);
CREATE INDEX IF NOT EXISTS idx_chunks_hash    ON chunks(content_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_name
    ON chunks(project_id, name)
    WHERE name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_qname
    ON chunks(project_id, qualified_name)
    WHERE qualified_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_search_gin
    ON chunks USING GIN (search_vector);

-- ---------------------------------------------------------------------------
-- chunk_embeddings: side table mirrors SQLite vec0 layout. Keeps chunks
-- rows narrow (a 384-element vector inline doubles row width and forces
-- TOAST on every read). FK cascade replaces the explicit-cleanup quirk
-- documented for SQLite vec0.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id  BIGINT    PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    embedding vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunk_embeddings_hnsw
    ON chunk_embeddings USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- file_edges: dependency graph (imports, requires, includes).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS file_edges (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_file_id  BIGINT    NOT NULL REFERENCES files(id)    ON DELETE CASCADE,
    target_file_id  BIGINT    REFERENCES files(id)             ON DELETE CASCADE,
    kind            TEXT      NOT NULL,
    raw             TEXT      NOT NULL,
    symbol          TEXT,
    line            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_file_edges_src     ON file_edges(source_file_id);
CREATE INDEX IF NOT EXISTS idx_file_edges_tgt
    ON file_edges(target_file_id)
    WHERE target_file_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_file_edges_project ON file_edges(project_id);

-- ---------------------------------------------------------------------------
-- project_variables: Jinja/Terraform variable substitutions.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_variables (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scope       TEXT      NOT NULL,
    name        TEXT      NOT NULL,
    value       TEXT      NOT NULL,
    source      TEXT      NOT NULL DEFAULT 'manual',
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL,
    UNIQUE (project_id, scope, name)
);
ALTER TABLE project_variables
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
CREATE INDEX IF NOT EXISTS idx_project_variables
    ON project_variables(project_id, scope);

-- ---------------------------------------------------------------------------
-- query_cache: per-project, per-HEAD answer cache. NOT migrated from
-- SQLite — local + short-TTL; warms naturally on first ask call.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_cache (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    query_hash  TEXT      NOT NULL,
    head_sha    TEXT      NOT NULL,
    result_json TEXT      NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    expires_at  DOUBLE PRECISION NOT NULL,
    UNIQUE (project_id, query_hash, head_sha)
);
CREATE INDEX IF NOT EXISTS idx_query_cache_exp ON query_cache(expires_at);

-- ---------------------------------------------------------------------------
-- history: work-summary RAG memory.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS history (
    id            BIGSERIAL PRIMARY KEY,
    project_id    BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at    DOUBLE PRECISION NOT NULL,
    short_summary TEXT      NOT NULL,
    long_summary  TEXT      NOT NULL,
    session_id    TEXT,
    tags          TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_project_time
    ON history(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS history_embeddings (
    history_id BIGINT    PRIMARY KEY REFERENCES history(id) ON DELETE CASCADE,
    embedding  vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS history_embeddings_hnsw
    ON history_embeddings USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- decisions: durable record of non-obvious choices.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id            BIGSERIAL PRIMARY KEY,
    project_id    BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at    DOUBLE PRECISION NOT NULL,
    topic         TEXT      NOT NULL,
    decision      TEXT      NOT NULL,
    rationale     TEXT,
    files_touched TEXT,
    session_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_proj_time
    ON decisions(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS decision_embeddings (
    decision_id BIGINT    PRIMARY KEY REFERENCES decisions(id) ON DELETE CASCADE,
    embedding   vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_embeddings_hnsw
    ON decision_embeddings USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- migration_log: forensics for sqlite -> PG migrate runs (Phase 2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS migration_log (
    id                 BIGSERIAL PRIMARY KEY,
    project_id         BIGINT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source             TEXT      NOT NULL,
    sqlite_project_id  BIGINT,
    migrated_at        DOUBLE PRECISION NOT NULL,
    note               TEXT
);
