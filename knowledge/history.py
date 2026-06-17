"""Per-project work-summary store (RAG memory).

A separate track from the code-chunk index. Each entry captures one unit
of work — a fix, a refactor, a decision — as a pair of (short, long)
summaries. Only the short summary is embedded; long is pulled by ID when
the caller drills in. That keeps the vector index lean and the retrieval
flow obvious:

    recent  → time-scoped list, no vector work
    search  → semantic over short summaries
    get     → fetch short + long for a hit

Typical flow: during a session the CLI appends entries to a per-session
JSONL file under ``~/.knowledge/stage/<project-slug>/sess-<id>.jsonl``
(see ``paths.session_stage_file``). A later ``knowledge history ingest``
walks every project-stage subdir and flushes each file into SQLite in a
transaction of its own. Per-file ingest takes exclusive ownership by
atomically renaming ``sess-*.jsonl`` to ``*.inflight-<pid>-<ts>`` before
reading — so three hook events firing (Stop, PreCompact, SessionEnd)
can't double-ingest the same file, and one failed file doesn't block the
others.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import NamedTuple

from . import config, db
from .db import Connection
from .embedder import get_embedder
from .sanitizer import scrub_text


class HistoryEntry(NamedTuple):
    id: int
    project_id: int
    created_at: float
    short_summary: str
    long_summary: str
    session_id: str | None
    tags: str | None


_SELECT_COLS = (
    "id, project_id, created_at, short_summary, long_summary, "
    "session_id, tags"
)


def add(
    conn: Connection,
    project_id: int,
    short_summary: str,
    long_summary: str,
    session_id: str | None = None,
    tags: str | None = None,
) -> int:
    """Insert one entry + its short-summary embedding. Returns new row id.

    Runs in its own transaction so a failed embed or insert leaves the
    table untouched. Callers ingesting many entries should use
    ``ingest_stage`` instead — it batches the encode call.
    """
    short_summary = scrub_text(short_summary)
    long_summary = scrub_text(long_summary)
    vec = get_embedder().encode([short_summary])[0]
    with db.transaction(conn):
        now = time.time()
        new_id = db.execute_returning_id(
            conn,
            "INSERT INTO history("
            "project_id, created_at, short_summary, long_summary, "
            "session_id, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, now, short_summary, long_summary, session_id, tags),
        )
        db.insert_history_embedding(conn, new_id, vec)
    return new_id


def get(conn: Connection, history_id: int) -> HistoryEntry | None:
    row = db.fetch_one(
        conn,
        f"SELECT {_SELECT_COLS} FROM history WHERE id = ?",
        (history_id,),
    )
    return _row_to_entry(row) if row else None


def recent(
    conn: Connection,
    project_id: int | None = None,
    days: int | None = None,
    limit: int = 20,
) -> list[HistoryEntry]:
    """Newest-first list; no vector work. The session-start 'where did we
    stop' query.
    """
    where: list[str] = []
    params: list = []
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if days is not None:
        where.append("created_at >= ?")
        params.append(time.time() - days * 86400)
    extra = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = db.fetch_all(
        conn,
        f"SELECT {_SELECT_COLS} FROM history {extra} "
        f"ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    return [_row_to_entry(r) for r in rows]


def search(
    conn: Connection,
    query: str,
    project_id: int | None = None,
    top_k: int = config.DEFAULT_TOP_K,
) -> list[tuple[HistoryEntry, float]]:
    """Semantic search over ``short_summary``. Returns ``(entry, distance)``
    tuples ordered by ascending distance.

    Mirrors ``search.search``'s over-fetch-and-filter pattern so project
    scoping doesn't silently return a too-short list.
    """
    q_vec = get_embedder().encode([query])[0]
    k_fetch = top_k * 3 if project_id is not None else top_k

    if db.current_mode() == "postgresql":
        where_clauses: list[str] = []
        filter_params: list = []
        if project_id is not None:
            where_clauses.append("h.project_id = %s")
            filter_params.append(project_id)
        extra_where = (
            "AND " + " AND ".join(where_clauses) if where_clauses else ""
        )
        cols = ", ".join("h." + c for c in _SELECT_COLS.split(", "))
        sql = f"""
            SELECT {cols}, (e.embedding <=> %s) AS distance
            FROM history_embeddings e
            JOIN history h ON h.id = e.history_id
            WHERE TRUE {extra_where}
            ORDER BY e.embedding <=> %s
            LIMIT %s
        """
        # SQL placeholder order: distance projection, filter clauses,
        # ORDER BY operand, LIMIT.
        params = [q_vec, *filter_params, q_vec, top_k]
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [(_row_to_entry(r[:-1]), float(r[-1])) for r in rows]

    # SQLite path — sqlite-vec virtual table.
    where_clauses = []
    params = [q_vec.tobytes(), k_fetch]
    if project_id is not None:
        where_clauses.append("h.project_id = ?")
        params.append(project_id)
    extra_where = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""
    cols = ", ".join("h." + c for c in _SELECT_COLS.split(", "))
    sql = f"""
        SELECT {cols}, v.distance
        FROM history_vec v
        JOIN history h ON h.id = v.history_id
        WHERE v.embedding MATCH ? AND k = ?
        {extra_where}
        ORDER BY v.distance ASC
        LIMIT ?
    """
    params.append(top_k)
    rows = conn.execute(sql, params).fetchall()
    return [(_row_to_entry(r[:-1]), float(r[-1])) for r in rows]


def _parse_stage_lines(raw: str) -> tuple[list[dict], int]:
    """Parse JSONL text into valid entries + skip-count of malformed lines."""
    entries: list[dict] = []
    skipped = 0
    for ln in raw.splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        short = obj.get("short")
        long_ = obj.get("long")
        if not isinstance(short, str) or not isinstance(long_, str):
            skipped += 1
            continue
        if not short.strip() or not long_.strip():
            skipped += 1
            continue
        entries.append(obj)
    return entries, skipped


def _insert_entries(
    conn: Connection,
    entries: list[dict],
    project_id: int,
) -> None:
    """Encode short summaries in one batch and insert all rows in one
    APSW savepoint. Caller decides what to do with the source file.
    """
    # Scrub secrets before embedding and storage — so both the vector and the
    # stored text reflect the sanitized version. Done over a copy so the
    # caller's list is not mutated (entries may be re-used on error retry).
    clean_entries = [
        {**e, "short": scrub_text(e["short"]), "long": scrub_text(e["long"])}
        for e in entries
    ]
    shorts = [e["short"] for e in clean_entries]
    vecs = get_embedder().encode(shorts)
    with db.transaction(conn):  # all-or-nothing for this batch
        now = time.time()
        for obj, vec in zip(clean_entries, vecs):
            new_id = db.execute_returning_id(
                conn,
                "INSERT INTO history("
                "project_id, created_at, short_summary, long_summary, "
                "session_id, tags) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    now,
                    obj["short"],
                    obj["long"],
                    obj.get("session_id"),
                    obj.get("tags"),
                ),
            )
            db.insert_history_embedding(conn, new_id, vec)


def ingest_stage(
    conn: Connection,
    stage_path: Path,
    project_id: int,
) -> tuple[int, int]:
    """Legacy/explicit single-file flush. Truncate-on-success.

    Used by ``knowledge history ingest --stage-file <path>`` and the
    one-shot migration of the pre-slug ``pending.jsonl``. Prefer
    :func:`ingest_stage_dir` for the normal hook-driven flow.

    Returns ``(ingested, skipped)``. Malformed lines are counted in
    ``skipped``. DB errors propagate; the source file is left intact so
    the caller can retry.

    JSONL schema per line (unknown keys ignored):
        {"short": "<str>", "long": "<str>",
         "session_id": "<str?>", "tags": "<str?>"}
    """
    if not stage_path.exists():
        return (0, 0)
    raw = stage_path.read_text(encoding="utf-8")
    if not raw.strip():
        return (0, 0)

    entries, skipped = _parse_stage_lines(raw)
    if not entries:
        return (0, skipped)

    _insert_entries(conn, entries, project_id)
    # Only reached on commit success. Truncate so callers/tests can reuse
    # the same path without stale lines.
    stage_path.write_text("", encoding="utf-8")
    return (len(entries), skipped)


def ingest_stage_dir(
    conn: Connection,
    project_dir: Path,
    project_id: int,
) -> tuple[int, int]:
    """Flush every ``sess-*.jsonl`` in ``project_dir`` into ``project_id``.

    Each file is processed in its own APSW savepoint. Before reading, the
    file is atomically renamed to ``*.inflight-<pid>-<ms>`` — a concurrent
    ingest whose rename loses (``FileNotFoundError``) skips silently, so
    Stop/PreCompact/SessionEnd firing near-simultaneously never produces
    duplicate rows.

    A failing file's ``.inflight-*`` sibling is **left on disk** as a
    debugging breadcrumb; subsequent ingests ignore it (the glob matches
    only ``sess-*.jsonl``), so one bad file doesn't block the others.
    The error propagates to the caller after every other file has been
    attempted.

    Returns ``(ingested, skipped)`` summed across all files. Returns
    ``(0, 0)`` if the dir has no matching files.
    """
    if not project_dir.exists():
        return (0, 0)

    total_ingested = 0
    total_skipped = 0
    first_error: BaseException | None = None

    for jf in sorted(project_dir.glob("sess-*.jsonl")):
        stamp = f"{os.getpid()}-{int(time.time() * 1000)}"
        inflight = jf.with_name(f"{jf.name}.inflight-{stamp}")
        try:
            os.rename(jf, inflight)
        except FileNotFoundError:
            # Another ingest won the race for this file.
            continue

        try:
            raw = inflight.read_text(encoding="utf-8")
            entries, skipped = _parse_stage_lines(raw)
            total_skipped += skipped
            if entries:
                _insert_entries(conn, entries, project_id)
                total_ingested += len(entries)
            # Delete only on full success (insert + parse). Empty files
            # (no entries, no skips) are also deleted — nothing to keep.
            inflight.unlink()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            # Leave inflight on disk for forensics; keep going on other files.
            if first_error is None:
                first_error = exc

    if first_error is not None:
        raise first_error
    return (total_ingested, total_skipped)


def sweep_inflight_debris(stage_root: Path, older_than_seconds: float) -> int:
    """Delete ``*.inflight-*`` debris older than ``older_than_seconds``.

    ``ingest_stage_dir`` renames ``sess-*.jsonl`` to ``*.inflight-<pid>-<ms>``
    before reading, and unlinks on commit. A crash between rename and unlink
    leaves the inflight sibling on disk — harmless (future ingests skip it
    because the glob matches only ``sess-*.jsonl``), but it accumulates as
    dead bytes over time.

    Walks every project subdir under ``stage_root`` and unlinks matching
    files whose mtime is older than the threshold. Returns the count.
    Fresh inflight files (an ingest running right now) are spared.
    """
    if not stage_root.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for project_dir in stage_root.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.inflight-*"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except FileNotFoundError:
                # Raced with another GC / process — fine, it's gone.
                continue
    return removed


def _row_to_entry(row) -> HistoryEntry:
    return HistoryEntry(
        id=row[0],
        project_id=row[1],
        created_at=row[2],
        short_summary=row[3],
        long_summary=row[4],
        session_id=row[5],
        tags=row[6],
    )
