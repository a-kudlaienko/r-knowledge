"""Per-project work-summary store (RAG memory).

A separate track from the code-chunk index. Each entry captures one unit
of work — a fix, a refactor, a decision — as a pair of (short, long)
summaries. Only the short summary is embedded; long is pulled by ID when
the caller drills in. That keeps the vector index lean and the retrieval
flow obvious:

    recent  → time-scoped list, no vector work
    search  → semantic over short summaries
    get     → fetch short + long for a hit

Typical flow: the LLM appends entries to a staged JSONL file during a
session, then a single ``ingest_stage`` call flushes all pending entries
into SQLite in one transaction. The stage file is truncated only after
the DB commit succeeds — a failed ingest leaves staged work intact for
retry or continued appending.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import NamedTuple

from . import config
from .db import Connection
from .embedder import get_embedder


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
    vec = get_embedder().encode([short_summary])[0]
    with conn:
        now = time.time()
        conn.execute(
            "INSERT INTO history("
            "project_id, created_at, short_summary, long_summary, "
            "session_id, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, now, short_summary, long_summary, session_id, tags),
        )
        new_id = conn.last_insert_rowid()
        conn.execute(
            "INSERT INTO history_vec(history_id, embedding) VALUES (?, ?)",
            (new_id, vec.tobytes()),
        )
    return new_id


def get(conn: Connection, history_id: int) -> HistoryEntry | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM history WHERE id = ?",
        (history_id,),
    ).fetchone()
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
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM history {extra} "
        f"ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
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

    where_clauses: list[str] = []
    params: list = [q_vec.tobytes(), k_fetch]
    if project_id is not None:
        where_clauses.append("h.project_id = ?")
        params.append(project_id)
    extra_where = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT {", ".join("h." + c for c in _SELECT_COLS.split(", "))},
               v.distance
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


def ingest_stage(
    conn: Connection,
    stage_path: Path,
    project_id: int,
) -> tuple[int, int]:
    """Read ``stage_path`` (JSONL), insert every valid entry under
    ``project_id``, truncate the file on success.

    Returns ``(ingested, skipped)``. Malformed lines are skipped and
    counted. DB errors propagate and the stage file is left intact so
    the caller can retry or continue appending.

    JSONL schema per line (unknown keys ignored):
        {"short": "<str>", "long": "<str>",
         "session_id": "<str?>", "tags": "<str?>"}
    """
    if not stage_path.exists():
        return (0, 0)

    raw = stage_path.read_text(encoding="utf-8")
    if not raw.strip():
        # File exists but empty — nothing to do, leave it.
        return (0, 0)

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

    if not entries:
        return (0, skipped)

    shorts = [e["short"] for e in entries]
    vecs = get_embedder().encode(shorts)

    with conn:  # APSW savepoint: all-or-nothing for this batch
        now = time.time()
        for obj, vec in zip(entries, vecs):
            conn.execute(
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
            new_id = conn.last_insert_rowid()
            conn.execute(
                "INSERT INTO history_vec(history_id, embedding) VALUES (?, ?)",
                (new_id, vec.tobytes()),
            )

    # Only reached if the savepoint committed successfully. Safe to clear.
    stage_path.write_text("", encoding="utf-8")
    return (len(entries), skipped)


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
