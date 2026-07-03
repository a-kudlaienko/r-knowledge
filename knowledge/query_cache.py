"""Per-project LOCAL answer cache for ``knowledge ask``.

Lives entirely outside the main DB, in a dedicated SQLite file per project
at ``~/.knowledge/cache/<slug>.sqlite`` (see
:func:`knowledge.paths.query_cache_db`). This is true in BOTH storage
modes (local ``sqlite`` and ``shared_postgresql``) — there is one code
path, no backend dispatch. Previously the cache lived in the main DB's
``query_cache`` table, which in ``shared_postgresql`` mode meant every
``get``/``put`` paid 1-3 network round trips on top of LB latency for a
cache whose value is purely local-agent-speed: per-HEAD, 1h TTL, zero
team value. Decision id=100 already excluded ``query_cache`` from the
sqlite-to-PG migration on exactly this basis ("local + short TTL").

Uses stdlib ``sqlite3`` rather than APSW (which ``knowledge/db.py`` uses
for the main DB): APSW earns its keep there because it supports loadable
extensions (``sqlite-vec``) on Homebrew/python.org builds where stdlib
``sqlite3`` doesn't. This cache file never touches a vector index or any
extension, so stdlib ``sqlite3`` is the simpler, dependency-free choice.

Keyed on ``(query_hash, head_sha)`` where ``query_hash`` already folds in
kind/lang/top_k/``config.SCHEMA_VERSION`` (see ``compute_key``). No
``project_id`` column — the file itself is already per-project, which
sidesteps sqlite-vs-PG ``project_id`` collisions entirely.

``index_stamp`` (``max(last_build, last_update)`` from the caller's
already-fetched ``projects`` row — see ``knowledge/cli.py``'s ``cmd_ask``)
is the cross-client invalidation signal: any client's index mutation
(this machine, or a teammate's in ``shared_postgresql`` mode) bumps the
stamp, so a row cached under an older stamp misses even though nobody
explicitly deleted it. ``put`` overwrites the row for a given
``(query_hash, head_sha)`` with the current stamp, so a stamp mismatch
self-heals on the next write — no separate sweep is required for
correctness (``invalidate`` below is still called for prompt local
cleanup right after a build/update).

Deliberately NOT keyed on ``git status --porcelain``. The agent's
in-flight edits would cause pathological misses otherwise — the cache is
for query-side acceleration, not a correctness guarantee against
unstaged edits.

Caches the **pre-rerank** result list only. Rerank is cheap (map lookups
+ arithmetic) and its inputs (recent git log, session stage) change over
time, so we always apply it fresh on each call.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path

from . import config, paths
from .search import SearchResult


_TTL_SECONDS = 3600  # 1 hour
_BUSY_TIMEOUT_MS = 5000  # absorb rare races between concurrent knowledge processes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_cache (
    query_hash  TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    index_stamp REAL NOT NULL,
    result_json TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (query_hash, head_sha)
)
"""


def compute_key(
    query: str,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> str:
    """Stable hash for cache lookup.

    Includes ``config.SCHEMA_VERSION`` so any schema bump auto-invalidates
    cached answers without a separate clear step.
    """
    raw = f"{query}|{kind or ''}|{lang or ''}|{top_k}|{config.SCHEMA_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_head_sha(root: Path) -> str:
    """Return ``git HEAD`` SHA, or empty string if not available.

    Empty string is a valid cache key too — a non-git directory's cache
    is invalidated by every ``knowledge update`` via the index_stamp bump.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _connect(project_root: Path) -> sqlite3.Connection:
    """Open (creating on first use) this project's local cache file.

    Autocommit (``isolation_level=None``): every caller in this module
    issues exactly one statement per connection, so there's no multi-
    statement transaction to wrap — autocommit keeps callers simple.
    """
    path = paths.query_cache_db(project_root)
    conn = sqlite3.connect(
        str(path), timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(_SCHEMA)
    return conn


def get(
    project_root: Path,
    query_hash: str,
    head_sha: str,
    index_stamp: float,
) -> list[SearchResult] | None:
    """Return cached ``SearchResult`` list, or ``None`` on miss/expired.

    A row whose stored ``index_stamp`` differs from the current one is
    not matched by this query at all — it reads as a plain miss, no
    separate staleness check needed.
    """
    now = time.time()
    with contextlib.closing(_connect(project_root)) as conn:
        row = conn.execute(
            "SELECT result_json FROM query_cache "
            "WHERE query_hash = ? AND head_sha = ? AND index_stamp = ? "
            "  AND expires_at > ?",
            (query_hash, head_sha, index_stamp, now),
        ).fetchone()
    if row is None:
        return None
    try:
        items = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        # Poisoned row — silently treat as miss; next put() overwrites it.
        return None
    return [SearchResult(**d) for d in items]


def put(
    project_root: Path,
    query_hash: str,
    head_sha: str,
    index_stamp: float,
    results: list[SearchResult],
) -> None:
    """Persist the pre-rerank result list with 1h TTL.

    Idempotent: re-caching the same key overwrites the prior entry
    (including its ``index_stamp``), refreshing the TTL. ``created_at``
    tracks the latest write.
    """
    now = time.time()
    expires = now + _TTL_SECONDS
    # SearchResult is a NamedTuple; _asdict() is stable.
    payload = json.dumps([r._asdict() for r in results], default=str)
    with contextlib.closing(_connect(project_root)) as conn:
        conn.execute(
            "INSERT INTO query_cache(query_hash, head_sha, index_stamp, "
            "result_json, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(query_hash, head_sha) DO UPDATE SET "
            "  index_stamp = excluded.index_stamp, "
            "  result_json = excluded.result_json, "
            "  created_at  = excluded.created_at, "
            "  expires_at  = excluded.expires_at",
            (query_hash, head_sha, index_stamp, payload, now, expires),
        )


def invalidate(project_root: Path) -> int:
    """Drop all cached answers for this project's local cache file.

    Called from the indexer right after a build/update actually changes
    something — prompt local cleanup so the *same* process's next `ask`
    doesn't need to wait for a stamp-mismatch miss to notice. Not required
    for cross-client correctness (the ``index_stamp`` bump already
    handles that for every other client) — just tidiness.
    """
    with contextlib.closing(_connect(project_root)) as conn:
        return conn.execute("DELETE FROM query_cache").rowcount


def sweep_expired(project_root: Path) -> int:
    """Drop rows past their TTL. Cheap opportunistic housekeeping."""
    with contextlib.closing(_connect(project_root)) as conn:
        return conn.execute(
            "DELETE FROM query_cache WHERE expires_at < ?", (time.time(),)
        ).rowcount
