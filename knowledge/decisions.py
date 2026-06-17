"""Durable record of non-obvious choices made during sessions.

Complements :mod:`knowledge.history` (one entry per unit of work) with
structured fields that make "what did we decide about X?" answerable
without prose parsing.

Schema (see :mod:`knowledge.db`):

* ``topic``        — short label, e.g. "cache invalidation"
* ``decision``     — the choice itself, e.g. "wipe per-project on any chunk change"
* ``rationale``    — one-line why (optional)
* ``files_touched``— JSON array of rel_paths (optional)
* ``session_id``   — whichever Claude session recorded it (optional)

Mirrors the add/get/recent/search API shape from :mod:`history` so the
CLI dispatcher stays boringly similar.
"""

from __future__ import annotations

import json
import time
from typing import NamedTuple

from . import config, db
from .db import Connection
from .embedder import get_embedder
from .sanitizer import scrub_text


class Decision(NamedTuple):
    id: int
    project_id: int
    created_at: float
    topic: str
    decision: str
    rationale: str | None
    files_touched: list[str]      # parsed from JSON; always a list (possibly empty)
    session_id: str | None
    author: str | None            # who recorded it (git identity / UNIX login)
    supersedes: int | None        # id of the decision this one overrides
    override_reason: str | None   # justification comment for the override


_SELECT_COLS = (
    "id, project_id, created_at, topic, decision, rationale, "
    "files_touched, session_id, author, supersedes, override_reason"
)


def add(
    conn: Connection,
    project_id: int,
    topic: str,
    decision: str,
    rationale: str | None = None,
    files_touched: list[str] | None = None,
    session_id: str | None = None,
    author: str | None = None,
    supersedes: int | None = None,
    override_reason: str | None = None,
) -> int:
    """Insert one decision + its embedding. Returns new row id.

    Embedded text is ``topic || ' :: ' || decision`` — both fields matter
    for retrieval, and the separator keeps tokenization from bleeding
    one into the other.

    ``author`` is stamped on every decision for shared-DB attribution.
    ``supersedes`` / ``override_reason`` are set together only when this
    decision overrides a prior one (the CLI enforces the justification).
    """
    # Scrub secrets from all free-text fields before embedding and storage.
    # topic is usually a short slug but can carry leaked values from user
    # shell history; rationale is optional prose — both are scrubbed cheaply.
    topic = scrub_text(topic)
    decision = scrub_text(decision)
    if rationale is not None:
        rationale = scrub_text(rationale)
    text_to_embed = f"{topic} :: {decision}"
    vec = get_embedder().encode([text_to_embed])[0]
    files_json = json.dumps(files_touched) if files_touched else None

    with db.transaction(conn):
        now = time.time()
        new_id = db.execute_returning_id(
            conn,
            "INSERT INTO decisions("
            "project_id, created_at, topic, decision, rationale, "
            "files_touched, session_id, author, supersedes, override_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, now, topic, decision, rationale, files_json,
             session_id, author, supersedes, override_reason),
        )
        db.insert_decision_embedding(conn, new_id, vec)
    return new_id


def exact_topic_match(
    conn: Connection, project_id: int, topic: str
) -> Decision | None:
    """Newest decision in this project whose topic equals ``topic``
    (case-insensitive), or ``None``. Used for the non-blocking "you may mean
    to supersede id=N" nudge on a plain ``decide``.
    """
    # Exact (not substring) equality, case-insensitive. Avoid LIKE here so a
    # literal % or _ in a topic label isn't treated as a wildcard.
    if db.current_mode() == "postgresql":
        pred = "LOWER(topic) = LOWER(?)"
    else:
        pred = "topic = ? COLLATE NOCASE"
    row = db.fetch_one(
        conn,
        f"SELECT {_SELECT_COLS} FROM decisions "
        f"WHERE project_id = ? AND {pred} "
        f"ORDER BY created_at DESC LIMIT ?",
        (project_id, topic, 1),
    )
    return _row_to_decision(row) if row else None


def get(conn: Connection, decision_id: int) -> Decision | None:
    row = db.fetch_one(
        conn,
        f"SELECT {_SELECT_COLS} FROM decisions WHERE id = ?",
        (decision_id,),
    )
    return _row_to_decision(row) if row else None


def recent(
    conn: Connection,
    project_id: int | None = None,
    days: int | None = None,
    topic: str | None = None,
    limit: int = 20,
) -> list[Decision]:
    """Newest-first list; no vector work.

    ``topic`` filter is case-insensitive LIKE — a coarse prefix/substring
    filter for the common "show me decisions about cache" flow.
    """
    where: list[str] = []
    params: list = []
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if days is not None:
        where.append("created_at >= ?")
        params.append(time.time() - days * 86400)
    if topic:
        # SQLite LIKE is case-insensitive only with ASCII ``COLLATE
        # NOCASE``; PG needs ``ILIKE``. Both accept ``%foo%`` substring
        # syntax so the parameter shape is identical.
        if db.current_mode() == "postgresql":
            where.append("topic ILIKE ?")
        else:
            where.append("topic LIKE ? COLLATE NOCASE")
        params.append(f"%{topic}%")

    extra = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = db.fetch_all(
        conn,
        f"SELECT {_SELECT_COLS} FROM decisions {extra} "
        f"ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    return [_row_to_decision(r) for r in rows]


def search(
    conn: Connection,
    query: str,
    project_id: int | None = None,
    top_k: int = config.DEFAULT_TOP_K,
) -> list[tuple[Decision, float]]:
    """Semantic search over ``topic || decision``. ``(decision, distance)``
    ordered by ascending distance.
    """
    q_vec = get_embedder().encode([query])[0]
    k_fetch = top_k * 3 if project_id is not None else top_k
    cols_prefixed = ", ".join("d." + c for c in _SELECT_COLS.split(", "))

    if db.current_mode() == "postgresql":
        where_clauses: list[str] = []
        filter_params: list = []
        if project_id is not None:
            where_clauses.append("d.project_id = %s")
            filter_params.append(project_id)
        extra_where = (
            "AND " + " AND ".join(where_clauses) if where_clauses else ""
        )
        sql = f"""
            SELECT {cols_prefixed}, (e.embedding <=> %s) AS distance
            FROM decision_embeddings e
            JOIN decisions d ON d.id = e.decision_id
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
        return [(_row_to_decision(r[:-1]), float(r[-1])) for r in rows]

    # SQLite path — sqlite-vec virtual table.
    where_clauses = []
    params = [q_vec.tobytes(), k_fetch]
    if project_id is not None:
        where_clauses.append("d.project_id = ?")
        params.append(project_id)
    extra_where = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT {cols_prefixed}, v.distance
        FROM decisions_vec v
        JOIN decisions d ON d.id = v.decision_id
        WHERE v.embedding MATCH ? AND k = ?
        {extra_where}
        ORDER BY v.distance ASC
        LIMIT ?
    """
    params.append(top_k)
    rows = conn.execute(sql, params).fetchall()
    return [(_row_to_decision(r[:-1]), float(r[-1])) for r in rows]


def _row_to_decision(row) -> Decision:
    """Parse ``files_touched`` JSON to a list on read.

    Invalid JSON (shouldn't happen; we only write valid JSON) degrades
    silently to an empty list so callers can iterate without guards.
    """
    raw_files = row[6]
    if raw_files:
        try:
            files = json.loads(raw_files)
            if not isinstance(files, list):
                files = []
        except (json.JSONDecodeError, TypeError):
            files = []
    else:
        files = []
    return Decision(
        id=row[0],
        project_id=row[1],
        created_at=row[2],
        topic=row[3],
        decision=row[4],
        rationale=row[5],
        files_touched=files,
        session_id=row[7],
        author=row[8],
        supersedes=row[9],
        override_reason=row[10],
    )
