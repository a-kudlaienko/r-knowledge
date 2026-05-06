"""SQLite connection + schema.

Uses APSW instead of the stdlib ``sqlite3`` module because macOS Homebrew /
python.org Python builds stdlib sqlite3 without loadable-extension support,
which would break ``sqlite-vec``. APSW always supports it and ships wheels
for every platform we care about.

One DB, many projects. All tables namespaced by ``project_id``. Vector index
lives in the ``sqlite-vec`` virtual table ``chunks_vec``; project scoping is a
plain JOIN on ``chunks.project_id``.

Schema bumps: change ``config.SCHEMA_VERSION``. A mismatch between stored and
compiled version forces a full rebuild (the CLI prints a clear message rather
than silently migrating — better UX for a local tool).
"""

from __future__ import annotations

from pathlib import Path

import apsw
import sqlite_vec

from . import config, paths

# Re-exported for type hints elsewhere — callers import ``Connection`` from
# this module, not from ``apsw`` directly, so the backend stays swappable.
Connection = apsw.Connection


def connect(db_path: Path | None = None) -> Connection:
    """Open the DB, load ``sqlite-vec``, turn on foreign keys + WAL.

    Side-effect: ``init_schema(conn)`` is called on first open so callers
    don't need to remember to bootstrap.
    """
    target = db_path or paths.db_path()
    conn = apsw.Connection(str(target))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


# Individual DDL statements — APSW has no ``executescript``; each statement
# is issued separately. Keeps the transaction semantics predictable.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        root_path   TEXT NOT NULL UNIQUE,
        git_remote  TEXT,
        created_at  REAL NOT NULL,
        last_build  REAL,
        last_update REAL,
        file_count  INTEGER NOT NULL DEFAULT 0,
        chunk_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        rel_path      TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        mtime         REAL NOT NULL,
        size          INTEGER NOT NULL,
        lang          TEXT NOT NULL,
        last_scanned  REAL NOT NULL,
        UNIQUE(project_id, rel_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id)",
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        parent_id      INTEGER REFERENCES chunks(id) ON DELETE CASCADE,
        sibling_order  INTEGER,
        kind           TEXT NOT NULL,
        name           TEXT,
        qualified_name TEXT,
        start_line     INTEGER NOT NULL,
        end_line       INTEGER NOT NULL,
        start_byte     INTEGER NOT NULL,
        end_byte       INTEGER NOT NULL,
        char_count     INTEGER NOT NULL,
        content_hash   TEXT NOT NULL,
        stored_text    TEXT NOT NULL,
        embedded_text  TEXT NOT NULL,
        metadata       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_file    ON chunks(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_parent  ON chunks(parent_id, sibling_order)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_hash    ON chunks(content_hash)",
    """
    CREATE TABLE IF NOT EXISTS history (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        created_at    REAL NOT NULL,
        short_summary TEXT NOT NULL,
        long_summary  TEXT NOT NULL,
        session_id    TEXT,
        tags          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_project_time ON history(project_id, created_at DESC)",
    # file_edges: per-project dependency graph (imports, requires, includes).
    # Populated by knowledge/resolvers/* during build/update. target_file_id
    # is nullable: NULL = external (stdlib, node_modules, unresolved
    # template). raw is the literal string from source, preserved even for
    # resolved edges so LLM output can show "from .utils" alongside the file
    # it resolved to.
    """
    CREATE TABLE IF NOT EXISTS file_edges (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        source_file_id  INTEGER NOT NULL REFERENCES files(id)    ON DELETE CASCADE,
        target_file_id  INTEGER          REFERENCES files(id)    ON DELETE CASCADE,
        kind            TEXT    NOT NULL,
        raw             TEXT    NOT NULL,
        symbol          TEXT,
        line            INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_file_edges_src     ON file_edges(source_file_id)",
    "CREATE INDEX IF NOT EXISTS idx_file_edges_tgt     ON file_edges(target_file_id) "
    "WHERE target_file_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_file_edges_project ON file_edges(project_id)",
    # project_variables: per-project Jinja/Terraform variable substitutions.
    # Consulted when resolving an edge whose ``raw`` contains ``{{ name }}``
    # (Ansible/Helm) or ``${var.name}`` (Terraform). ``scope`` namespaces
    # values by domain so ``deploy_env`` can mean different things for
    # ansible vs terraform vs helm; ``all`` is a catch-all merged into any
    # scope-specific lookup (scope-specific wins on name collision).
    """
    CREATE TABLE IF NOT EXISTS project_variables (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        scope       TEXT    NOT NULL,
        name        TEXT    NOT NULL,
        value       TEXT    NOT NULL,
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL,
        UNIQUE(project_id, scope, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_variables "
    "ON project_variables(project_id, scope)",
)


def init_schema(conn: Connection) -> None:
    """Create tables if missing, seed ``meta`` with versions on first run."""
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)

    # Vector tables — created separately so we can template the dimension.
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[{config.EMBEDDING_DIM}]
        )
        """
    )
    # history_vec embeds ONLY the short_summary. Long summaries are retrieved
    # by ID when the caller drills in — keeps the vector index lean.
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS history_vec USING vec0(
            history_id INTEGER PRIMARY KEY,
            embedding FLOAT[{config.EMBEDDING_DIM}]
        )
        """
    )

    # Seed versions on first run. APSW auto-commits outside of explicit
    # transaction blocks, so these INSERTs are durable immediately.
    existing = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    wanted = {
        "schema_version": config.SCHEMA_VERSION,
        "chunker_version": config.CHUNKER_VERSION,
        "embedding_model": config.MODEL,
        "embedding_dim": str(config.EMBEDDING_DIM),
    }
    for k, v in wanted.items():
        if k not in existing:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (k, v))


def get_meta(conn: Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
