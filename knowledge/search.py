"""Vector search + result formatting.

sqlite-vec's KNN syntax takes the query vector and a ``k`` parameter
inside the ``WHERE`` clause. Additional filters (project, kind, lang) are
applied by joining ``chunks`` + ``files`` and filtering post-KNN. For a
local tool with single-digit thousands of chunks per project that's
fine — the KNN stage returns ``k`` candidates and the JOIN filters are
zero-cost. If that changes we'd inline the filters into the MATCH query.
"""

from __future__ import annotations

from typing import NamedTuple

from . import config
from .db import Connection
from .embedder import get_embedder


class SearchResult(NamedTuple):
    chunk_id: int
    kind: str
    name: str | None
    qualified_name: str | None
    start_line: int
    end_line: int
    rel_path: str
    lang: str
    project_name: str
    project_root: str
    preview: str
    distance: float


def search(
    conn: Connection,
    query: str,
    project_id: int | None = None,
    kind: str | None = None,
    lang: str | None = None,
    top_k: int = config.DEFAULT_TOP_K,
) -> list[SearchResult]:
    embedder = get_embedder()
    q_vec = embedder.encode([query])[0]

    # Over-fetch from sqlite-vec when post-filters are set — some of the
    # KNN hits will be filtered out by project/kind/lang, so asking for
    # only ``top_k`` would return a short list. 3x slack handles the
    # common case; deep filters may still return under top_k, which is
    # acceptable.
    k_fetch = top_k * 3 if (project_id or kind or lang) else top_k

    where_clauses: list[str] = []
    params: list = [q_vec.tobytes(), k_fetch]
    if project_id is not None:
        where_clauses.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        where_clauses.append("c.kind = ?")
        params.append(kind)
    if lang:
        where_clauses.append("f.lang = ?")
        params.append(lang)
    extra_where = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line,
               f.rel_path, f.lang, p.name AS project_name, p.root_path,
               substr(c.stored_text, 1, 400) AS preview, v.distance
        FROM chunks_vec v
        JOIN chunks   c ON c.id = v.chunk_id
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE v.embedding MATCH ? AND k = ?
        {extra_where}
        ORDER BY v.distance ASC
        LIMIT ?
    """
    params.append(top_k)

    rows = conn.execute(sql, params).fetchall()
    return [
        SearchResult(
            chunk_id=r[0],
            kind=r[1],
            name=r[2],
            qualified_name=r[3],
            start_line=r[4],
            end_line=r[5],
            rel_path=r[6],
            lang=r[7],
            project_name=r[8],
            project_root=r[9],
            preview=r[10],
            distance=float(r[11]),
        )
        for r in rows
    ]


def get_chunk(conn: Connection, chunk_id: int):
    """Fetch a single chunk row by id. Used by ``knowledge get`` / ``path``."""
    return conn.execute(
        "SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line, "
        "c.start_byte, c.end_byte, c.stored_text, f.rel_path, p.root_path, "
        "c.parent_id "
        "FROM chunks c JOIN files f ON f.id = c.file_id "
        "JOIN projects p ON p.id = c.project_id WHERE c.id = ?",
        (chunk_id,),
    ).fetchone()


def get_family(conn: Connection, chunk_id: int) -> list:
    """Return the chunk plus its parent/children in hierarchy order.

    If ``chunk_id`` refers to a ``big_parent``: returns ``[parent, sub_0,
    sub_1, ...]`` sorted by ``sibling_order``.
    If it refers to a ``big_subchunk``: returns the same family rooted at
    its parent.
    Otherwise (regular chunk with no parent/children): returns just the one.

    Rows are ``(id, kind, name, start_line, end_line, start_byte, end_byte,
    stored_text, rel_path, project_root)`` — enough for ``cmd_get`` to
    re-slice or print.
    """
    row = conn.execute(
        "SELECT id, kind, parent_id FROM chunks WHERE id = ?", (chunk_id,)
    ).fetchone()
    if row is None:
        return []
    _cid, kind, parent_id = row

    # Pick the root: the chunk itself if it's a parent (or has no parent),
    # otherwise walk up one level.
    if kind == "big_subchunk" and parent_id is not None:
        root_id = parent_id
    else:
        root_id = chunk_id

    # One query: root + all its children (ordered).
    return conn.execute(
        """
        SELECT c.id, c.kind, c.name, c.start_line, c.end_line,
               c.start_byte, c.end_byte, c.stored_text,
               f.rel_path, p.root_path, c.sibling_order
        FROM chunks c
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE c.id = ? OR c.parent_id = ?
        ORDER BY CASE WHEN c.id = ? THEN -1 ELSE c.sibling_order END
        """,
        (root_id, root_id, root_id),
    ).fetchall()
