"""Vector search + result formatting.

Backend dispatch:

* SQLite (sqlite-vec): KNN syntax takes the query vector via ``MATCH`` and a
  ``k`` parameter inside the ``WHERE`` clause; embeddings live in the
  ``chunks_vec`` virtual table. Filters (project, kind, lang) are pushed
  INTO the KNN as a ``chunk_id IN (…)`` prefilter — see
  :func:`_sqlite_prefilter` for why filtering *after* the KNN silently
  starves scoped queries.
* PostgreSQL (pgvector): KNN via ``ORDER BY embedding <=> $vec LIMIT k``
  using the cosine-distance operator on an HNSW index. Embeddings live in
  the side table ``chunk_embeddings`` so the wide ``chunks`` row stays
  cheap to scan.

Both paths return identical :class:`SearchResult` tuples, so the
formatters and downstream rerank don't care which backend ran the query.
"""

from __future__ import annotations

from typing import NamedTuple

from . import config, db
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

    if db.current_mode() == "postgresql":
        return _search_postgres(conn, q_vec, project_id, kind, lang, top_k)
    return _search_sqlite(conn, q_vec, project_id, kind, lang, top_k)


def _sqlite_prefilter(
    project_id: int | None,
    kind: str | None,
    lang: str | None,
) -> tuple[str, list]:
    """Build the ``chunk_id IN (…)`` prefilter for the sqlite-vec KNN.

    Returns ``(sql_fragment, params)``. The fragment is ``""`` when no filter
    is set, which keeps the unscoped query byte-identical to its old shape.

    Why a prefilter and not a post-KNN join filter: in vec0 the ``k = ?``
    constraint is a HARD pre-limit resolved before any joined ``WHERE`` runs,
    so filtering afterwards can only shrink an already-truncated candidate
    set. A project holding a small share of a shared index would silently get
    fewer than ``top_k`` rows — often zero. Constraining the KNN's own
    candidate set up front makes ``k`` exact again.

    The subquery form (rather than a bound id list) keeps this safe for large
    projects: a 26k-chunk project would otherwise blow past
    ``SQLITE_MAX_VARIABLE_NUMBER``.
    """
    clauses: list[str] = []
    params: list = []
    if project_id is not None:
        clauses.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        clauses.append("c.kind = ?")
        params.append(kind)
    if lang:
        clauses.append("f.lang = ?")
        params.append(lang)
    if not clauses:
        return "", []
    # ``files`` is only needed for the lang predicate — keep the subquery as
    # narrow as the filters demand.
    join = " JOIN files f ON f.id = c.file_id" if lang else ""
    return (
        "AND v.chunk_id IN ("
        f"SELECT c.id FROM chunks c{join} WHERE {' AND '.join(clauses)})",
        params,
    )


def _search_sqlite(
    conn: Connection,
    q_vec,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> list[SearchResult]:
    # k is exact now — the prefilter constrains the KNN's own candidate set,
    # so there's no need to over-fetch and post-filter.
    prefilter_sql, prefilter_params = _sqlite_prefilter(project_id, kind, lang)

    sql = f"""
        SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line,
               f.rel_path, f.lang, p.name AS project_name, p.root_path,
               substr(c.stored_text, 1, 400) AS preview, v.distance
        FROM chunks_vec v
        JOIN chunks   c ON c.id = v.chunk_id
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE v.embedding MATCH ? AND k = ?
        {prefilter_sql}
        ORDER BY v.distance ASC
        LIMIT ?
    """
    # Outer LIMIT is now a redundant safety net (k already bounds the KNN
    # exactly) rather than the truncation point it used to be.
    params = [q_vec.tobytes(), top_k, *prefilter_params, top_k]
    rows = conn.execute(sql, params).fetchall()
    return [row_to_result(r) for r in rows]


def build_pg_vector_query(
    q_vec,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> tuple[str, list]:
    """Pure builder for the pgvector KNN query. No DB access — callable to
    prepare a statement for either a direct ``cur.execute`` or a pipelined
    ``conn.execute`` (see ``hybrid_search._pg_pipelined_channels``).

    pgvector accepts numpy arrays directly when ``register_vector`` was
    called on the connection (see ``PostgresBackend.connect``). The cosine
    distance operator is ``<=>`` and matches our L2-normalized
    embeddings — same metric as sqlite-vec's default.

    **Param shape is decision id=102 and must not change**: SQL placeholder
    order is distance projection, filter clauses, ORDER BY operand, LIMIT.
    ``q_vec`` appears twice (once for the projected distance column, once
    for the ORDER BY operator) with the filters sandwiched *between* the
    two occurrences — so the param list must be built as
    ``[q_vec, *filter_params, q_vec, top_k]`` from a separate,
    initially-empty ``filter_params`` list. Pre-seeding a list with
    ``q_vec`` and prepending it again silently doubles ``q_vec`` at the
    front and breaks the param/placeholder count (this exact bug was
    caught live against production PG — see decision id=102).
    """
    where_clauses: list[str] = []
    filter_params: list = []
    if project_id is not None:
        where_clauses.append("c.project_id = %s")
        filter_params.append(project_id)
    if kind:
        where_clauses.append("c.kind = %s")
        filter_params.append(kind)
    if lang:
        where_clauses.append("f.lang = %s")
        filter_params.append(lang)
    extra_where = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line,
               f.rel_path, f.lang, p.name AS project_name, p.root_path,
               substr(c.stored_text, 1, 400) AS preview,
               (e.embedding <=> %s) AS distance
        FROM chunk_embeddings e
        JOIN chunks   c ON c.id = e.chunk_id
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE TRUE {extra_where}
        ORDER BY e.embedding <=> %s
        LIMIT %s
    """
    params = [q_vec, *filter_params, q_vec, top_k]
    return sql, params


def _search_postgres(
    conn: Connection,
    q_vec,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> list[SearchResult]:
    sql, params = build_pg_vector_query(q_vec, project_id, kind, lang, top_k)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows_to_results(rows)


def rows_to_results(rows) -> list[SearchResult]:
    """Convert raw pgvector-query rows into ``SearchResult``.

    Exposed (not private) so ``hybrid_search`` can convert rows fetched
    from a pipelined ``conn.execute`` the same way ``_search_postgres``
    converts rows from a plain cursor.
    """
    return [row_to_result(r) for r in rows]


def row_to_result(r) -> SearchResult:
    return SearchResult(
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


def get_chunk(conn: Connection, chunk_id: int):
    """Fetch a single chunk row by id. Used by ``knowledge get`` / ``path``."""
    return db.fetch_one(
        conn,
        "SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line, "
        "c.start_byte, c.end_byte, c.stored_text, f.rel_path, p.root_path, "
        "c.parent_id "
        "FROM chunks c JOIN files f ON f.id = c.file_id "
        "JOIN projects p ON p.id = c.project_id WHERE c.id = ?",
        (chunk_id,),
    )


def get_family(conn: Connection, chunk_id: int) -> list:
    """Return the chunk plus its parent/children in hierarchy order.

    If ``chunk_id`` refers to a ``big_parent``: returns ``[parent, sub_0,
    sub_1, ...]`` sorted by ``sibling_order``.
    If it refers to a ``big_subchunk``: returns the same family rooted at
    its parent.
    Otherwise (regular chunk with no parent/children): returns just the one.
    """
    row = db.fetch_one(
        conn, "SELECT id, kind, parent_id FROM chunks WHERE id = ?", (chunk_id,)
    )
    if row is None:
        return []
    _cid, kind, parent_id = row

    if kind == "big_subchunk" and parent_id is not None:
        root_id = parent_id
    else:
        root_id = chunk_id

    return db.fetch_all(
        conn,
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
    )
