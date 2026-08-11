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
* ``kind``         — ``"decision"`` (default) or ``"fact"``. A fact is a
  working fix / research finding recorded via ``knowledge fact`` — same row
  shape, same embedding index, no separate table (Item H). Retrieval treats
  both kinds identically unless the caller passes ``kind=`` to filter.

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
    kind: str = "decision"        # "decision" (default) or "fact"


_SELECT_COLS = (
    "id, project_id, created_at, topic, decision, rationale, "
    "files_touched, session_id, author, supersedes, override_reason, kind"
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
    kind: str = "decision",
    context: str | None = None,
) -> int:
    """Insert one decision (or fact) + its embedding. Returns new row id.

    Embedded text is ``topic || ' :: ' || decision`` — both fields matter
    for retrieval, and the separator keeps tokenization from bleeding
    one into the other. When ``context`` is given (the ``knowledge fact``
    path — a symptom / error string), it is appended as a third segment
    (``topic :: decision :: context``) so a future session searching by the
    literal error text still hits this row.

    ``context`` is not a separate column — it is folded into ``rationale``
    with a label (``"Symptom: ...\\n\\nWhy it works: ..."``) so a fact stays a
    plain decisions row (Item H: no new table). ``rationale`` alone (no
    context — the plain ``decide`` path) is stored verbatim, unchanged from
    before this parameter existed.

    ``author`` is stamped on every decision for shared-DB attribution.
    ``supersedes`` / ``override_reason`` are set together only when this
    decision overrides a prior one (the CLI enforces the justification).
    ``kind`` is ``"decision"`` (default) or ``"fact"``.
    """
    # Scrub secrets from all free-text fields before embedding and storage.
    # topic is usually a short slug but can carry leaked values from user
    # shell history; rationale/context are optional prose — all are scrubbed
    # cheaply before either storage or embedding sees them.
    topic = scrub_text(topic)
    decision = scrub_text(decision)
    if rationale is not None:
        rationale = scrub_text(rationale)
    if context is not None:
        context = scrub_text(context)

    if context:
        # Fold context into rationale with labels so the fact stays a plain
        # decisions row — dense and self-explanatory without a new column.
        parts = [f"Symptom: {context}"]
        if rationale:
            parts.append(f"Why it works: {rationale}")
        stored_rationale = "\n\n".join(parts)
        text_to_embed = f"{topic} :: {decision} :: {context}"
    else:
        stored_rationale = rationale
        text_to_embed = f"{topic} :: {decision}"

    vec = get_embedder().encode([text_to_embed])[0]
    files_json = json.dumps(files_touched) if files_touched else None

    with db.transaction(conn):
        now = time.time()
        new_id = db.execute_returning_id(
            conn,
            "INSERT INTO decisions("
            "project_id, created_at, topic, decision, rationale, "
            "files_touched, session_id, author, supersedes, override_reason, "
            "kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, now, topic, decision, stored_rationale, files_json,
             session_id, author, supersedes, override_reason, kind),
        )
        db.insert_decision_embedding(conn, new_id, vec)
    return new_id


def _split_rationale(stored: str | None) -> tuple[str | None, str | None]:
    """Inverse of the context-into-rationale fold in :func:`add`.

    ``add()`` writes the ``rationale`` column as exactly one of:

    * ``None`` — no context, no rationale
    * ``"<rationale>"`` — plain ``decide`` path, no context
    * ``"Symptom: <context>"`` — ``fact`` path, no rationale
    * ``"Symptom: <context>\\n\\nWhy it works: <rationale>"`` — ``fact`` path, both

    Returns ``(context, rationale)``. Input that doesn't start with the
    ``"Symptom: "`` label is the plain ``decide`` path — passed through
    verbatim as the rationale half with ``context=None``.

    Known limitation (bounded, verified non-destructive): the labels are
    prose, not delimiters, so user text that mimics them is misread — a
    ``rationale`` beginning ``"Symptom: "`` is taken for a context, and a
    ``context`` containing ``"\\n\\nWhy it works: "`` splits at that point.
    Re-folding always reproduces the stored string byte-for-byte in both
    cases (no data loss, and repeated patches don't accumulate labels), so
    the only effect is which half a later patch treats as embeddable. The
    ambiguity is inherited from :func:`add`'s single-column fold, not
    introduced here; a real ``context`` column would be the actual fix.
    """
    if not stored:
        return None, None
    prefix = "Symptom: "
    if not stored.startswith(prefix):
        return None, stored
    sep = "\n\nWhy it works: "
    if sep in stored:
        context, why = stored.split(sep, 1)
        return context[len(prefix):], why
    return stored[len(prefix):], None


def patch(
    conn: Connection,
    decision_id: int,
    *,
    topic: str | None = None,
    decision: str | None = None,
    context: str | None = None,
    rationale: str | None = None,
    files_touched: list[str] | None = None,
) -> tuple[Decision, bool] | None:
    """Update one row in place — the normal correction path for a bad
    ``decide``/``fact`` entry. Only fields passed as non-``None`` change;
    omitted fields keep their stored value. Returns ``None`` if
    ``decision_id`` doesn't exist.

    ``context``/``rationale`` share the single ``rationale`` column (see
    module docstring + :func:`add`) — patching one half rebuilds the
    composite via :func:`_split_rationale` so the other half already
    stored survives untouched. ``created_at``, ``author``, ``kind``,
    ``supersedes``, ``override_reason`` and ``project_id`` are never
    modified here.

    Re-embeds ONLY when the embedded triple (``topic``, ``decision``,
    ``context``) actually changed value — not merely supplied unchanged.
    ``rationale`` alone is never embedded, matching :func:`add`. Returns
    ``(updated, re_embedded)`` so the CLI can report whether the vector
    was refreshed.
    """
    current = get(conn, decision_id)
    if current is None:
        return None

    new_topic = scrub_text(topic) if topic is not None else current.topic
    new_decision = scrub_text(decision) if decision is not None else current.decision

    old_context, old_why = _split_rationale(current.rationale)
    new_context = scrub_text(context) if context is not None else old_context
    new_why = scrub_text(rationale) if rationale is not None else old_why

    if new_context:
        # Same fold as add(): label both halves, why is optional.
        parts = [f"Symptom: {new_context}"]
        if new_why:
            parts.append(f"Why it works: {new_why}")
        new_stored_rationale = "\n\n".join(parts)
    else:
        new_stored_rationale = new_why

    re_embed = (
        new_topic != current.topic
        or new_decision != current.decision
        or new_context != old_context
    )

    new_files = files_touched if files_touched is not None else current.files_touched
    files_json = json.dumps(new_files) if new_files else None

    # Embed BEFORE opening the transaction, exactly as add() does. encode() can
    # be a daemon round-trip or a cold 130MB model load; doing it inside the
    # transaction would hold the SQLite write lock (or an open PG transaction)
    # for that whole time. Failing here leaves the row untouched, which is the
    # same outcome as failing inside — so there's nothing to gain by waiting.
    vec = None
    if re_embed:
        text_to_embed = (
            f"{new_topic} :: {new_decision} :: {new_context}"
            if new_context
            else f"{new_topic} :: {new_decision}"
        )
        vec = get_embedder().encode([text_to_embed])[0]

    with db.transaction(conn):
        db.execute(
            conn,
            "UPDATE decisions SET topic = ?, decision = ?, rationale = ?, "
            "files_touched = ? WHERE id = ?",
            (new_topic, new_decision, new_stored_rationale, files_json, decision_id),
        )
        if vec is not None:
            db.replace_decision_embedding(conn, decision_id, vec)

    updated = current._replace(
        topic=new_topic,
        decision=new_decision,
        rationale=new_stored_rationale,
        files_touched=new_files,
    )
    return updated, re_embed


def delete(conn: Connection, decision_id: int) -> bool:
    """Delete one row + its embedding. Returns True if it existed.

    Used when the subject of a decision/fact no longer exists — the only
    other delete path is the whole-project cascade in
    :func:`knowledge.projects.forget_project`. Deletes the vector before
    the row (mirrors that function): the SQLite vec0 table has no FK
    cascade, so :func:`db.delete_decision_embedding` cleans it explicitly;
    PostgreSQL cascades on the row delete and no-ops there instead.

    Does NOT touch rows that ``supersedes`` this one — see
    :func:`referencing` for finding them.
    """
    with db.transaction(conn):
        db.delete_decision_embedding(conn, decision_id)
        deleted = db.execute(conn, "DELETE FROM decisions WHERE id = ?", (decision_id,))
    return deleted > 0


def referencing(conn: Connection, decision_id: int) -> list[Decision]:
    """Rows whose ``supersedes`` points at ``decision_id``.

    ``supersedes`` has no FK constraint on either backend, so deleting a
    superseded row leaves a silent dangling pointer here — the caller
    (``knowledge delete``) uses this to warn about it, not to rewrite
    anything.
    """
    rows = db.fetch_all(
        conn,
        f"SELECT {_SELECT_COLS} FROM decisions WHERE supersedes = ?",
        (decision_id,),
    )
    return [_row_to_decision(r) for r in rows]


def superseded_by(
    conn: Connection, project_id: int | None = None
) -> dict[int, int]:
    """Map dead id -> the id of the NEWEST row that supersedes it.

    ``superseded_ids`` answers "is this row dead?"; render paths also need
    "dead in favour of what?" so they can point a reader at the replacement.
    Ordering by ``created_at`` and letting later rows overwrite earlier ones
    means a row superseded twice resolves to the most recent replacement, not
    an intermediate one.

    ``supersedes`` has no FK on either backend and older rows may store an
    empty string rather than NULL, so both are filtered out.
    """
    where = "supersedes IS NOT NULL AND supersedes <> ''"
    params: list = []
    if project_id is not None:
        where += " AND project_id = ?"
        params.append(project_id)
    rows = db.fetch_all(
        conn,
        f"SELECT id, supersedes FROM decisions WHERE {where} "
        f"ORDER BY created_at ASC",
        tuple(params),
    )
    mapping: dict[int, int] = {}
    for row in rows:
        try:
            mapping[int(row[1])] = int(row[0])
        except (TypeError, ValueError):
            continue
    return mapping


def superseded_ids(
    conn: Connection, project_id: int | None = None
) -> set[int]:
    """Ids that some NEWER row supersedes — i.e. the dead rows.

    The forward pointer lives on the newer row (``supersedes``), so "has this
    row been superseded?" is only answerable by scanning for rows that point
    AT it. :func:`referencing` answers that for one id; this answers it
    set-wise for a whole request, which is what the retrieval and render
    paths need. Thin wrapper over :func:`superseded_by` so there is one query
    and one source of truth for "dead or alive".
    """
    return set(superseded_by(conn, project_id))


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
    kind: str | None = None,
) -> list[Decision]:
    """Newest-first list; no vector work.

    ``topic`` filter is case-insensitive LIKE — a coarse prefix/substring
    filter for the common "show me decisions about cache" flow. ``kind``
    (``"decision"`` or ``"fact"``) filters to just that kind; ``None``
    (default) returns both — the mandated conflict check must see facts too.
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
    if kind:
        where.append("kind = ?")
        params.append(kind)

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
    kind: str | None = None,
) -> list[tuple[Decision, float]]:
    """Semantic search over ``topic || decision [|| context]``. ``(decision,
    distance)`` ordered by ascending distance.

    ``kind`` (``"decision"`` or ``"fact"``) filters to just that kind;
    ``None`` (default) returns both — the mandated conflict check must see
    facts too.
    """
    q_vec = get_embedder().encode([query])[0]
    cols_prefixed = ", ".join("d." + c for c in _SELECT_COLS.split(", "))

    if db.current_mode() == "postgresql":
        where_clauses: list[str] = []
        filter_params: list = []
        if project_id is not None:
            where_clauses.append("d.project_id = %s")
            filter_params.append(project_id)
        if kind:
            where_clauses.append("d.kind = %s")
            filter_params.append(kind)
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

    # SQLite path — sqlite-vec virtual table. ``k = ?`` inside a vec0 MATCH
    # is a HARD pre-limit resolved BEFORE any joined WHERE runs, so filtering
    # project_id/kind only in the WHERE (the old approach) silently returns
    # fewer than top_k rows for scoped queries — often zero. Instead we
    # prefilter with an ``IN (subquery)`` on the vec table's declared PK
    # column (``decision_id`` — NOT ``v.rowid``, which does not exist on
    # this virtual table) so the KNN itself only considers matching rows.
    # With a prefilter, k is exact and needs no over-fetch multiplier.
    filter_where: list[str] = []
    filter_params: list = []
    if project_id is not None:
        filter_where.append("project_id = ?")
        filter_params.append(project_id)
    if kind:
        filter_where.append("kind = ?")
        filter_params.append(kind)
    if filter_where:
        prefilter = (
            "AND v.decision_id IN (SELECT id FROM decisions WHERE "
            + " AND ".join(filter_where)
            + ")"
        )
    else:
        prefilter = ""
    sql = f"""
        SELECT {cols_prefixed}, v.distance
        FROM decisions_vec v
        JOIN decisions d ON d.id = v.decision_id
        WHERE v.embedding MATCH ? AND k = ?
        {prefilter}
        ORDER BY v.distance ASC
        LIMIT ?
    """
    params = [q_vec.tobytes(), top_k, *filter_params, top_k]
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
        kind=row[11] if len(row) > 11 and row[11] else "decision",
    )
