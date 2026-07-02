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
from typing import Any, Iterable

import apsw
import sqlite_vec

from . import config, paths

# Re-exported for type hints elsewhere — callers import ``Connection`` from
# this module, not from ``apsw`` directly, so the backend stays swappable.
Connection = apsw.Connection


def connect(db_path: Path | None = None) -> Connection:
    """Open a connection to the configured backend.

    Default (``storage.mode = "sqlite"``): opens the SQLite DB at
    ``~/.knowledge/index.sqlite`` (or ``db_path`` if given), loads
    ``sqlite-vec``, enables foreign keys + WAL, runs schema bootstrap.

    Shared mode (``storage.mode = "shared_postgresql"``): opens a psycopg3
    connection via :class:`knowledge.backends.PostgresBackend`. ``db_path``
    has no meaning and is rejected if non-None — caller likely passed it
    by accident from a sqlite-era code path.

    Both connection types work as transaction context managers (``with
    conn:``), so the historical CLI pattern continues to work on PG.
    """

    if current_mode() == "postgresql":
        if db_path is not None:
            raise ValueError(
                "db_path is not applicable in shared_postgresql mode"
            )
        from . import backends

        return backends.load_backend().connect()

    return connect_sqlite(db_path)


def connect_sqlite(db_path: Path | None = None) -> Connection:
    """Open the SQLite DB regardless of ``storage.mode``.

    Migration tooling that needs to read from local SQLite while writing
    to PostgreSQL (or vice versa) calls this directly to bypass the
    mode-aware dispatch in :func:`connect`. Same wiring as the legacy
    sqlite path: APSW + sqlite-vec + WAL + ``init_schema``.
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
    # Partial indexes for exact-name lookup (schema v2). `knowledge find`
    # hits these for O(log n) lookups — anonymous chunks (markdown
    # sections, shell blocks) skip the index entirely.
    "CREATE INDEX IF NOT EXISTS idx_chunks_name  ON chunks(project_id, name) "
    "WHERE name IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_chunks_qname ON chunks(project_id, qualified_name) "
    "WHERE qualified_name IS NOT NULL",
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
        source      TEXT    NOT NULL DEFAULT 'manual',
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL,
        UNIQUE(project_id, scope, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_variables "
    "ON project_variables(project_id, scope)",
    # query_cache (schema v2): per-project, per-HEAD-sha answer cache for
    # `knowledge ask`. Keyed by (project_id, query_hash, head_sha); the
    # hash already includes schema_version so v2→v3 upgrades invalidate
    # automatically. TTL 1h on expires_at. Invalidated in bulk on
    # build/update when ≥1 chunk changes (see indexer.py).
    """
    CREATE TABLE IF NOT EXISTS query_cache (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        query_hash  TEXT    NOT NULL,
        head_sha    TEXT    NOT NULL,
        result_json TEXT    NOT NULL,
        created_at  REAL    NOT NULL,
        expires_at  REAL    NOT NULL,
        UNIQUE(project_id, query_hash, head_sha)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_query_cache_exp ON query_cache(expires_at)",
    # decisions (schema v2): durable record of non-obvious choices made
    # during sessions. Complements `history` (one entry per unit of work)
    # with structured fields that make "what did we decide about X?"
    # answerable without parsing prose. `files_touched` is a JSON array
    # of rel_paths — not a FK table because most queries are "give me
    # everything" rather than "which decisions touched file Y".
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        created_at      REAL    NOT NULL,
        topic           TEXT    NOT NULL,
        decision        TEXT    NOT NULL,
        rationale       TEXT,
        files_touched   TEXT,
        session_id      TEXT,
        -- author / supersedes / override_reason: see init_schema() backfill.
        -- author is stamped on every decision (git identity, UNIX-login
        -- fallback) so shared-DB teammates can see who set each standard.
        -- supersedes/override_reason are set only when a decision overrides
        -- a prior one — the override gate requires a justification comment.
        author          TEXT,
        supersedes      INTEGER,
        override_reason TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_proj_time "
    "ON decisions(project_id, created_at DESC)",
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
    # decisions_vec (schema v2): embeds ``topic || ' :: ' || decision``.
    # Same shape as history_vec — cheap semantic search for "what did we
    # decide about X?".
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS decisions_vec USING vec0(
            decision_id INTEGER PRIMARY KEY,
            embedding FLOAT[{config.EMBEDDING_DIM}]
        )
        """
    )

    # chunks_fts (schema v2): FTS5 over chunk symbol names + stored_text
    # for `knowledge grep`. Contentless (`content=''`) — we don't need
    # highlight()/snippet(), just MATCH-for-rowid, so tokens-only halves
    # the disk footprint vs storing a copy of stored_text.
    #
    # Triggers keep the FTS in sync with chunks. Contentless FTS5 DELETE
    # requires the OLD content via the special 'delete' command — trivial
    # from AFTER DELETE / AFTER UPDATE triggers which have OLD.* available,
    # awkward to replicate as explicit indexer calls.
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            name,
            qualified_name,
            stored_text,
            content=''
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, name, qualified_name, stored_text)
            VALUES (new.id, new.name, new.qualified_name, new.stored_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, name, qualified_name, stored_text)
            VALUES ('delete', old.id, old.name, old.qualified_name, old.stored_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, name, qualified_name, stored_text)
            VALUES ('delete', old.id, old.name, old.qualified_name, old.stored_text);
            INSERT INTO chunks_fts(rowid, name, qualified_name, stored_text)
            VALUES (new.id, new.name, new.qualified_name, new.stored_text);
        END
        """
    )

    # v1 → v2 migration backfill. Contentless FTS5 `'delete'` commands require
    # the OLD content to match what was indexed; feeding the trigger OLD rows
    # that were never inserted into the FTS corrupts the index. On fresh v2
    # DBs this branch is a no-op (both tables empty). On upgraded DBs it
    # populates FTS once, so the first post-upgrade rebuild's DELETE triggers
    # operate against consistent state. Guarded so we don't re-pay the cost
    # on every connect.
    fts_count = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    chunks_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if chunks_count > 0 and fts_count == 0:
        conn.execute(
            "INSERT INTO chunks_fts(rowid, name, qualified_name, stored_text) "
            "SELECT id, name, qualified_name, stored_text FROM chunks"
        )

    # Additive backfill for the decisions override gate. New nullable columns
    # (author / supersedes / override_reason) are backward-compatible, so we
    # do NOT bump SCHEMA_VERSION (that would force a destructive full rebuild).
    # Old rows keep NULL; only newly recorded decisions populate them. Guarded
    # by table_info so it's a no-op on already-migrated DBs.
    have_cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    for col, decl in (
        ("author", "TEXT"),
        ("supersedes", "INTEGER"),
        ("override_reason", "TEXT"),
    ):
        if col not in have_cols:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {decl}")

    # Additive backfill for project_variables.source. Distinguishes manual
    # `vars set` rows from auto-loaded ones (group_vars/host_vars). Default
    # 'manual' keeps every pre-existing row's behavior intact.
    have_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(project_variables)")}
    if "source" not in have_cols:
        conn.execute(
            "ALTER TABLE project_variables "
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
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
    row = fetch_one(conn, "SELECT value FROM meta WHERE key = ?", (key,))
    return row[0] if row else None


def set_meta(conn: Connection, key: str, value: str) -> None:
    # ``ON CONFLICT(key) DO UPDATE`` works identically on SQLite (>=3.24)
    # and PostgreSQL with the same syntax — the ``excluded`` pseudo-table
    # is the standard upsert spelling.
    execute(
        conn,
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Backend dispatch (Phase 1a)
#
# ``connect()`` is the historical sqlite-only entry point — every existing
# call site uses it and continues to in Phase 1a. ``get_backend()`` returns
# a :class:`knowledge.backends.Backend` adapter (sqlite or postgres) for
# the new dispatch-on-backend code paths landing in Phase 1b.
#
# Keeping both APIs side-by-side during the transition means we can move
# call sites one feature at a time without breaking sqlite users.
# ---------------------------------------------------------------------------


def get_backend():
    """Return the configured :class:`knowledge.backends.Backend`.

    Single source of truth for "which storage am I talking to right now".
    Cheap to call — no IO until you ``backend.connect()``.
    """

    from . import backends

    return backends.load_backend()


def offline_errors() -> tuple[type[BaseException], ...]:
    """Exception types meaning "the configured DB was unreachable".

    Use in an ``except db.offline_errors():`` clause around user-authored
    writes to buffer them to the local outbox instead of crashing. Returns
    ``()`` on SQLite (no connection-loss concept) so the clause is inert there.
    Never raises — a resolution failure degrades to ``()`` (don't swallow real
    errors as "offline").
    """
    try:
        return get_backend().connection_error_types()
    except Exception:  # noqa: BLE001 — backend/dep resolution must not crash callers
        return ()


class ProjectBusyError(RuntimeError):
    """Raised when a per-project advisory lock is held by another client.

    On PostgreSQL, two `knowledge build` / `update` runs against the same
    project must serialize — concurrent inserts would corrupt the
    chunks/embeddings tables. We use ``pg_try_advisory_xact_lock`` (non-
    blocking); if it fails, we raise this so the CLI can exit with code 3
    rather than blocking the user. SQLite never raises (locks are no-ops).
    """

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        super().__init__(
            f"project {project_name!r} is being indexed by another client; retry"
        )


# ---------------------------------------------------------------------------
# Per-call-site dispatch helpers (Phase 1b)
#
# Feature modules (search.py, fts.py, indexer.py, …) carry SQL strings and
# need to fork on backend at the small number of points where statements
# differ (parameter style, last-insert-id, vec0 vs pgvector). Rather than
# threading a Backend through every function signature, we expose a tiny
# process-cached ``current_mode()`` and a couple of helpers that hide the
# most common APSW-vs-psycopg shape differences. SQLite path is exactly
# what it was before — nothing in this section is hit until a feature
# module starts using these helpers.
# ---------------------------------------------------------------------------


@__import__("functools").lru_cache(maxsize=1)
def current_mode() -> str:
    """Return the **driver name** for the active config: ``"sqlite"`` or
    ``"postgresql"``.

    Note this is *not* the storage.mode literal from the YAML — the
    config-facing name is ``"shared_postgresql"`` (descriptive), but the
    driver-facing name is ``"postgresql"`` (matches
    :attr:`knowledge.backends.PostgresBackend.name` and the dispatch
    checks scattered through this module). The mapping happens here so
    feature modules can compare against the short, driver-shaped string.

    Result is cached per-process — settings are immutable for the
    lifetime of a CLI invocation.
    """

    from . import settings as settings_mod

    cfg_mode = settings_mod.load_settings().mode
    return "postgresql" if cfg_mode == "shared_postgresql" else "sqlite"


def execute_returning_id(conn, sql_no_returning: str, params: tuple) -> int:
    """INSERT (using ``?`` placeholders) and return the new row's id.

    ``sql_no_returning`` MUST use ``?`` placeholders; the helper rewrites
    them to ``%s`` and appends ``RETURNING id`` for PostgreSQL. SQLite
    keeps the original string unchanged.

    Limited to single-row INSERTs whose target table has an integer PK
    named ``id`` — everything in this codebase qualifies. Multi-row inserts
    or RETURNING-multiple-cols need their own dispatch.
    """

    if current_mode() == "postgresql":
        pg_sql = sql_no_returning.replace("?", "%s") + " RETURNING id"
        with conn.cursor() as cur:
            cur.execute(pg_sql, params)
            row = cur.fetchone()
            return int(row[0])
    conn.execute(sql_no_returning, params)
    return conn.last_insert_rowid()


def fetch_one(conn, sql: str, params: tuple = ()):
    """Run ``SELECT`` and return one row (tuple) or None.

    Translates ``?`` to ``%s`` for PG; SQLite path is unchanged. Wraps the
    psycopg cursor pattern so callers don't need ``with conn.cursor() as cur``
    boilerplate just to read a single row.
    """

    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return cur.fetchone()
    return conn.execute(sql, params).fetchone()


def fetch_all(conn, sql: str, params: tuple = ()):
    """Run ``SELECT`` and return all rows. Same dispatch as :func:`fetch_one`."""

    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return cur.fetchall()
    return conn.execute(sql, params).fetchall()


def execute(conn, sql: str, params: tuple = ()) -> int:
    """Run a write statement that doesn't return rows.

    Returns the number of rows affected (0 if none, ``-1`` only when the
    driver doesn't expose it — shouldn't happen on either backend in
    practice). Existing callers that discard the return value are
    unaffected; new callers can use the count for "did anything happen?"
    success messages without a follow-up SELECT.
    """

    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return cur.rowcount if cur.rowcount is not None else -1
    conn.execute(sql, params)
    # APSW's connection-level ``.changes()`` reports the row count of the
    # most recent INSERT/UPDATE/DELETE; sufficient because we don't
    # interleave statements between the call and this read.
    return conn.changes() if hasattr(conn, "changes") else -1


def transaction(conn):
    """Backend-agnostic transaction context manager.

    SQLite/APSW: ``with conn`` is the savepoint (commit on clean exit, roll
    back on exception). PostgreSQL/psycopg: ``conn.transaction()`` does the
    same. Returning the right object lets call sites write::

        with db.transaction(conn):
            ...mutations...

    instead of forking on backend at every transaction boundary.
    """

    if current_mode() == "postgresql":
        return conn.transaction()
    return conn  # APSW Connection is its own context manager.


def insert_chunk_embedding(conn, chunk_id: int, vec) -> None:
    """Insert a single chunk vector into the per-backend table.

    SQLite: ``chunks_vec`` virtual table, BLOB-encoded float array.
    PostgreSQL: ``chunk_embeddings(chunk_id, embedding vector(384))`` —
    pgvector accepts numpy arrays directly when ``register_vector`` was
    called on the connection (PostgresBackend.connect handles that).
    """

    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chunk_embeddings(chunk_id, embedding) "
                "VALUES (%s, %s)",
                (chunk_id, vec),
            )
        return
    conn.execute(
        "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, vec.tobytes()),
    )


def insert_chunk_embeddings_bulk(conn, rows: Iterable[tuple[int, Any]]) -> int:
    """Bulk-insert ``(chunk_id, embedding)`` pairs. Returns row count.

    PostgreSQL: a single ``COPY chunk_embeddings FROM STDIN`` — vectors go
    over the wire as pgvector's text literal ``[v1,v2,...]``. One round-trip
    for the whole batch instead of N; on remote / LB-fronted PG this is the
    difference between a multi-minute hang and a few seconds. Rows are
    streamed from the iterator so RAM stays bounded on huge builds.

    SQLite: falls back to the existing single-row insert into ``chunks_vec``.
    APSW ``executemany`` against a vec0 virtual table isn't exercised
    anywhere in this codebase, and the SQLite path is always local — the
    round-trip cost the COPY path solves does not apply.
    """

    if current_mode() == "postgresql":
        n = 0
        with conn.cursor() as cur:
            with cur.copy(
                "COPY chunk_embeddings(chunk_id, embedding) FROM STDIN"
            ) as copy:
                for chunk_id, vec in rows:
                    # ``.tolist()`` upcasts numpy float32 to Python float;
                    # ``str()`` on a float preserves full repr precision.
                    vec_str = "[" + ",".join(str(v) for v in vec.tolist()) + "]"
                    copy.write_row((chunk_id, vec_str))
                    n += 1
        return n
    n = 0
    for chunk_id, vec in rows:
        conn.execute(
            "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, vec.tobytes()),
        )
        n += 1
    return n


def reserve_ids(conn, table: str, n: int) -> list[int]:
    """Pre-allocate ``n`` ids for ``table`` in ONE round-trip.

    PostgreSQL: pulls ``n`` values from the table's ``id`` sequence via a
    single ``generate_series`` call. This lets the indexer resolve foreign keys
    (``chunks.file_id``, self-referential ``chunks.parent_id``) client-side
    and then stream every row through one COPY — instead of N
    ``INSERT ... RETURNING id`` round-trips (each a full LB/WAN latency hop
    on remote PG). The number of round-trips becomes independent of how many
    rows the repo has.

    ``table`` is interpolated into ``pg_get_serial_sequence`` (never a user
    value — only literals ``'chunks'`` / ``'files'`` from the indexer) so the
    real sequence name is resolved rather than hard-coded. Returns the ids in
    allocation order; ``n <= 0`` returns ``[]`` with no round-trip.

    SQLite: reads ``SELECT COALESCE(MAX(id), 0) FROM <table>`` and returns
    ``[max+1 .. max+n]``. Safe under the single-writer transaction guarantee
    (no concurrent writers can bump the max between the read and the
    subsequent batch INSERT).
    """

    if n <= 0:
        return []
    if table not in ("chunks", "files"):
        raise ValueError(
            f"reserve_ids: unsupported table {table!r}; expected 'chunks' or 'files'"
        )
    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nextval(pg_get_serial_sequence(%s, 'id')) "
                "FROM generate_series(1, %s)",
                (table, n),
            )
            return [int(r[0]) for r in cur.fetchall()]
    base = conn.execute(
        f"SELECT COALESCE(MAX(id), 0) FROM {table}"
    ).fetchone()[0]
    return list(range(base + 1, base + n + 1))


def copy_file_rows(conn, rows: Iterable[tuple]) -> int:
    """Bulk-insert fully-formed file rows.

    Columns in this exact order (ids come from :func:`reserve_ids`)::

        id, project_id, rel_path, content_hash, mtime, size, lang, last_scanned

    PostgreSQL: one ``COPY files(...) FROM STDIN`` — a single round-trip.
    SQLite: ``executemany`` INSERT with explicit ids.

    Returns the row count.
    """

    rows = list(rows)
    if not rows:
        return 0
    if current_mode() == "postgresql":
        n = 0
        with conn.cursor() as cur:
            with cur.copy(
                "COPY files(id, project_id, rel_path, content_hash, mtime, "
                "size, lang, last_scanned) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
                    n += 1
        return n
    conn.executemany(
        "INSERT INTO files("
        "id, project_id, rel_path, content_hash, mtime, size, lang, last_scanned"
        ") VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def copy_chunk_rows(conn, rows: Iterable[tuple]) -> int:
    """Bulk-insert fully-formed chunk rows.

    Each row MUST supply columns in this exact order — parents ahead of
    children, since the self-referential ``parent_id`` FK is NOT DEFERRABLE
    and is checked as each row lands::

        id, project_id, file_id, parent_id, sibling_order, kind, name,
        qualified_name, start_line, end_line, start_byte, end_byte,
        char_count, content_hash, stored_text, embedded_text, metadata

    PostgreSQL: one ``COPY chunks(...) FROM STDIN`` — ``search_vector`` is a
    GENERATED column and is intentionally omitted.
    SQLite: ``executemany`` INSERT with explicit ids. FTS5 ``chunks_fts`` is
    maintained by ``AFTER INSERT`` triggers on ``chunks`` which fire per-row
    under ``executemany`` automatically — no special handling needed.

    Returns the row count; ids come from :func:`reserve_ids`.
    """

    rows = list(rows)
    if not rows:
        return 0
    if current_mode() == "postgresql":
        n = 0
        with conn.cursor() as cur:
            with cur.copy(
                "COPY chunks(id, project_id, file_id, parent_id, sibling_order, "
                "kind, name, qualified_name, start_line, end_line, start_byte, "
                "end_byte, char_count, content_hash, stored_text, embedded_text, "
                "metadata) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
                    n += 1
        return n
    conn.executemany(
        "INSERT INTO chunks("
        "id, project_id, file_id, parent_id, sibling_order, kind, name, "
        "qualified_name, start_line, end_line, start_byte, end_byte, "
        "char_count, content_hash, stored_text, embedded_text, metadata"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def bulk_touch_files(conn, rows: Iterable[tuple[int, float, float]]) -> None:
    """Sync ``(mtime, last_scanned)`` for a batch of unchanged files.

    ``rows`` = ``[(file_id, mtime, last_scanned), ...]``. On an incremental
    ``update`` most files are byte-identical but their disk mtime may have
    moved (``git checkout``, editors, ``touch``); we refresh it so ``status``
    doesn't flag them stale forever.

    PostgreSQL: ONE ``UPDATE ... FROM (VALUES ...)`` — a single round-trip for
    the whole repo, instead of one per unchanged file (the difference between
    ~1 s and ~1 min on a remote/LB-fronted DB). SQLite: per-row UPDATE, as
    before — local, nothing to batch.
    """

    rows = list(rows)
    if not rows:
        return
    if current_mode() == "postgresql":
        # Column types are taken from the first VALUES row, so cast it; the
        # rest ride along. Flatten to positional params in row order.
        first = "(%s::bigint, %s::double precision, %s::double precision)"
        rest = "(%s, %s, %s)"
        values_sql = ",".join([first] + [rest] * (len(rows) - 1))
        params: list = []
        for fid, mtime, last_scanned in rows:
            params.extend((fid, mtime, last_scanned))
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE files AS f SET mtime = v.mtime, last_scanned = v.ls "
                f"FROM (VALUES {values_sql}) AS v(id, mtime, ls) "
                f"WHERE f.id = v.id",
                params,
            )
        return
    for fid, mtime, last_scanned in rows:
        conn.execute(
            "UPDATE files SET mtime = ?, last_scanned = ? WHERE id = ?",
            (mtime, last_scanned, fid),
        )


def bulk_update_chunk_positions(conn, rows: Iterable[tuple]) -> None:
    """Refresh positional / parent fields for many REUSED chunks in one call.

    Each row supplies, in this exact order:
        (id, parent_id, sibling_order, start_line, end_line, start_byte,
         end_byte, name, qualified_name, metadata)

    content_hash / stored_text / embedded_text / char_count / kind are NOT
    touched (reused chunks matched by content_hash, so those are unchanged).

    PostgreSQL: ONE ``UPDATE chunks AS c SET ... FROM (VALUES ...) AS v(...)
    WHERE c.id = v.id`` — one round-trip for the whole batch.
    SQLite: ``executemany`` UPDATE; id LAST to match the WHERE clause.
    """

    rows = list(rows)
    if not rows:
        return
    if current_mode() == "postgresql":
        # Column types are taken from the first VALUES row, so cast it; the
        # rest ride along. Flatten to positional params in row order.
        first = (
            "(%s::bigint, %s::bigint, %s::integer, %s::integer, %s::integer, "
            "%s::integer, %s::integer, %s::text, %s::text, %s::text)"
        )
        rest = "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        values_sql = ",".join([first] + [rest] * (len(rows) - 1))
        params: list = []
        for row in rows:
            params.extend(row)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE chunks AS c SET "
                f"parent_id = v.parent_id, "
                f"sibling_order = v.sibling_order, "
                f"start_line = v.start_line, "
                f"end_line = v.end_line, "
                f"start_byte = v.start_byte, "
                f"end_byte = v.end_byte, "
                f"name = v.name, "
                f"qualified_name = v.qualified_name, "
                f"metadata = v.metadata "
                f"FROM (VALUES {values_sql}) AS v("
                f"id, parent_id, sibling_order, start_line, end_line, "
                f"start_byte, end_byte, name, qualified_name, metadata) "
                f"WHERE c.id = v.id",
                params,
            )
        return
    # SQLite: id comes LAST in the WHERE clause, so reorder per row.
    conn.executemany(
        "UPDATE chunks SET "
        "parent_id=?, sibling_order=?, start_line=?, end_line=?, "
        "start_byte=?, end_byte=?, name=?, qualified_name=?, metadata=? "
        "WHERE id=?",
        [
            (r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[0])
            for r in rows
        ],
    )


def copy_file_edge_rows(conn, rows: Iterable[tuple]) -> int:
    """Bulk-insert ``file_edges`` rows without specifying ``id``.

    ``rows`` must be in this exact column order (engine assigns ``id``)::

        (project_id, source_file_id, target_file_id, kind, raw, symbol, line)

    PostgreSQL: one ``COPY file_edges(...) FROM STDIN`` — a single
    round-trip for all edges regardless of count.

    SQLite: ``executemany`` INSERT — one Python→C batch call.

    Returns the row count.
    """
    rows = list(rows)
    if not rows:
        return 0
    if current_mode() == "postgresql":
        n = 0
        with conn.cursor() as cur:
            with cur.copy(
                "COPY file_edges("
                "project_id, source_file_id, target_file_id, "
                "kind, raw, symbol, line"
                ") FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
                    n += 1
        return n
    conn.executemany(
        "INSERT INTO file_edges("
        "project_id, source_file_id, target_file_id, kind, raw, symbol, line"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def wipe_file_edges(conn, source_file_ids) -> None:
    """Delete all edges whose ``source_file_id`` is in ``source_file_ids``.

    PostgreSQL: one ``DELETE ... WHERE source_file_id = ANY(%s)`` — a
    single round-trip for the whole batch.

    SQLite: ``DELETE ... WHERE source_file_id IN (?,...)`` chunked into
    groups of ≤ 900 ids to stay well under ``SQLITE_MAX_VARIABLE_NUMBER``.

    No-op when ``source_file_ids`` is empty.
    """
    ids = list(source_file_ids)
    if not ids:
        return
    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM file_edges WHERE source_file_id = ANY(%s)",
                (ids,),
            )
        return
    # SQLite: chunk to stay under the variable-number limit.
    chunk_size = 900
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM file_edges WHERE source_file_id IN ({placeholders})",
            chunk,
        )


def delete_chunk_embeddings_for_project(conn, project_id: int) -> None:
    """Wipe vector rows for a project before a full rebuild.

    SQLite: ``chunks_vec`` is a virtual table without FK cascade — must be
    cleaned explicitly before the chunks rows it references go away.
    PostgreSQL: ``chunk_embeddings.chunk_id`` has ``ON DELETE CASCADE`` to
    ``chunks(id)`` — wiping ``chunks`` (which the indexer does next)
    sweeps the embeddings automatically. This helper is a no-op on PG.
    """

    if current_mode() == "postgresql":
        return
    conn.execute(
        "DELETE FROM chunks_vec WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE project_id = ?)",
        (project_id,),
    )


def delete_chunk_embeddings_by_ids(conn, chunk_ids: list[int]) -> None:
    """Wipe embeddings for a list of chunk ids.

    Same SQLite / PostgreSQL split as
    :func:`delete_chunk_embeddings_for_project`. Used by the incremental
    update path when chunks are about to be deleted.
    """

    if not chunk_ids:
        return
    if current_mode() == "postgresql":
        return  # FK cascade on chunks DELETE handles it
    placeholders = ",".join("?" * len(chunk_ids))
    conn.execute(
        f"DELETE FROM chunks_vec WHERE chunk_id IN ({placeholders})",
        chunk_ids,
    )


def bulk_update_file_rows(conn, rows: Iterable[tuple]) -> None:
    """Refresh ``content_hash / mtime / size / last_scanned`` for changed files.

    Each row supplies, in this exact order:
        (id, content_hash, mtime, size, last_scanned)

    PostgreSQL: ONE ``UPDATE files AS f SET ... FROM (VALUES ...) AS v(...)
    WHERE f.id = v.id`` — mirrors :func:`bulk_touch_files` exactly.
    First VALUES row is cast to resolve types; the rest ride along.
    SQLite: ``executemany`` UPDATE; id LAST for the WHERE clause.
    """

    rows = list(rows)
    if not rows:
        return
    if current_mode() == "postgresql":
        first = (
            "(%s::bigint, %s::text, %s::double precision, "
            "%s::bigint, %s::double precision)"
        )
        rest = "(%s,%s,%s,%s,%s)"
        values_sql = ",".join([first] + [rest] * (len(rows) - 1))
        params: list = []
        for row in rows:
            params.extend(row)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE files AS f SET "
                f"content_hash = v.content_hash, "
                f"mtime = v.mtime, "
                f"size = v.size, "
                f"last_scanned = v.last_scanned "
                f"FROM (VALUES {values_sql}) "
                f"AS v(id, content_hash, mtime, size, last_scanned) "
                f"WHERE f.id = v.id",
                params,
            )
        return
    # SQLite: id LAST in WHERE clause.
    conn.executemany(
        "UPDATE files SET content_hash=?, mtime=?, size=?, last_scanned=? WHERE id=?",
        [(r[1], r[2], r[3], r[4], r[0]) for r in rows],
    )


def delete_chunks_by_ids(conn, chunk_ids) -> None:
    """Delete chunks (and their embeddings) by id list.

    PostgreSQL: one ``DELETE FROM chunks WHERE id = ANY(%s)`` — FK ON DELETE
    CASCADE sweeps ``chunk_embeddings`` automatically.
    SQLite: first explicitly sweeps ``chunks_vec`` (no FK cascade on the vec0
    virtual table), then deletes from ``chunks`` in batches of ≤ 900 ids to
    stay under ``SQLITE_MAX_VARIABLE_NUMBER``.

    No-op when ``chunk_ids`` is empty.
    """

    chunk_ids = list(chunk_ids)
    if not chunk_ids:
        return
    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chunks WHERE id = ANY(%s)",
                (chunk_ids,),
            )
        return
    # SQLite: sweep vec0 first (no FK cascade), then delete chunks in chunks.
    delete_chunk_embeddings_by_ids(conn, chunk_ids)
    chunk_size = 900
    for i in range(0, len(chunk_ids), chunk_size):
        chunk = chunk_ids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM chunks WHERE id IN ({placeholders})",
            chunk,
        )


def fetch_chunks_for_files(conn, file_ids) -> list[tuple]:
    """Return ``(file_id, id, content_hash)`` rows for the given files.

    PostgreSQL: one ``SELECT ... WHERE file_id = ANY(%s)`` — single
    round-trip.
    SQLite: ``SELECT ... WHERE file_id IN (...)`` chunked into groups of
    ≤ 900 ids to stay under ``SQLITE_MAX_VARIABLE_NUMBER``; results are
    concatenated and returned.

    Returns ``[]`` when ``file_ids`` is empty.
    """

    file_ids = list(file_ids)
    if not file_ids:
        return []
    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, id, content_hash FROM chunks WHERE file_id = ANY(%s)",
                (file_ids,),
            )
            return cur.fetchall()
    result: list[tuple] = []
    chunk_size = 900
    for i in range(0, len(file_ids), chunk_size):
        chunk = file_ids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        result.extend(
            conn.execute(
                f"SELECT file_id, id, content_hash FROM chunks "
                f"WHERE file_id IN ({placeholders})",
                chunk,
            ).fetchall()
        )
    return result


def insert_history_embedding(conn, history_id: int, vec) -> None:
    """Insert a history short-summary embedding into the per-backend table."""

    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO history_embeddings(history_id, embedding) "
                "VALUES (%s, %s)",
                (history_id, vec),
            )
        return
    conn.execute(
        "INSERT INTO history_vec(history_id, embedding) VALUES (?, ?)",
        (history_id, vec.tobytes()),
    )


def insert_decision_embedding(conn, decision_id: int, vec) -> None:
    """Insert a decision-topic embedding into the per-backend table."""

    if current_mode() == "postgresql":
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO decision_embeddings(decision_id, embedding) "
                "VALUES (%s, %s)",
                (decision_id, vec),
            )
        return
    conn.execute(
        "INSERT INTO decisions_vec(decision_id, embedding) VALUES (?, ?)",
        (decision_id, vec.tobytes()),
    )
