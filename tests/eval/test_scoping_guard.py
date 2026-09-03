"""Regression guard: project-scoped vector search must not be starved.

Historical bug (fact id=404 / decide id=409 in this repo's knowledge index):
every sqlite-vec read path asked vec0 for ``k = top_k * 3`` **globally**,
then filtered ``project_id`` in the joined ``WHERE``. In vec0 ``k = ?`` is a
hard pre-limit resolved BEFORE any joined predicate runs, so the filter could
only shrink an already-truncated candidate set — a project holding a small
share of a shared index silently got fewer than ``top_k`` rows, frequently
ZERO, with no exception and no failing test.

This guard is independent of ``tests/test_vec_prefilter_scoping.py`` (which
already locks the fix in unit form against the real ``knowledge.search``
module) — its job here is different: prove the golden-query harness's own
regression sensor would have caught this bug, by reproducing the historical
buggy SQL shape *inline* (never touching ``knowledge/``) and asserting it
fails where the real, fixed ``search.search`` succeeds. A guard that cannot
fail under the bug it names is worthless.

Uses :class:`~tests.eval.fake_embedder.ControlledAngleEmbedder` (own,
independent implementation — not imported from any other test module) so
the global nearest-neighbour ordering is exact arithmetic, not luck: project
A crowds the near angles, project B sits far away, so the global top-k*3
window is pure A and a scoped B query is the exact scenario that starved
under the old code.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from knowledge import db, projects, search
from knowledge.db import Connection

from .fake_embedder import ControlledAngleEmbedder

pytestmark = pytest.mark.eval


# ---------------------------------------------------------------------------
# Fixtures / seed helpers (independent of tests/test_vec_prefilter_scoping.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def angle_embedder(monkeypatch):
    embedder = ControlledAngleEmbedder()
    monkeypatch.setattr(search, "get_embedder", lambda: embedder)
    return embedder


def _mk_project(conn: Connection, name: str) -> projects.Project:
    return projects.get_or_create_project(conn, Path(f"/tmp/eval-scoping-{name}"))


def _mk_file(conn: Connection, project_id: int, rel_path: str) -> int:
    now = time.time()
    return db.execute_returning_id(
        conn,
        "INSERT INTO files(project_id, rel_path, content_hash, mtime, size, "
        "lang, last_scanned) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, rel_path, f"h-{rel_path}", now, 100, "python", now),
    )


def _mk_chunk(conn: Connection, project_id: int, file_id: int, angle: float) -> int:
    chunk_id = db.execute_returning_id(
        conn,
        "INSERT INTO chunks(project_id, file_id, sibling_order, kind, name, "
        "qualified_name, start_line, end_line, start_byte, end_byte, "
        "char_count, content_hash, stored_text, embedded_text) "
        "VALUES (?, ?, 0, 'function', ?, ?, 1, 2, 0, 10, 10, ?, ?, ?)",
        (
            project_id,
            file_id,
            f"fn{angle}",
            f"fn{angle}",
            f"ch-{project_id}-{angle}",
            f"def fn{angle}(): pass",
            f"a{angle}",
        ),
    )
    vec = ControlledAngleEmbedder().encode([f"a{angle}"])[0]
    db.insert_chunk_embedding(conn, chunk_id, vec)
    return chunk_id


# Project A crowds the global nearest neighbours; project B is a small,
# distant pool — exactly the shape that starved under the old post-filter.
_NEAR = [float(i) for i in range(1, 41)]  # 40 rows, project A
_FAR = [100.0, 101.0, 102.0, 103.0, 104.0]  # 5 rows, project B


def _seed_two_projects(conn: Connection) -> tuple[projects.Project, projects.Project]:
    a, b = _mk_project(conn, "a"), _mk_project(conn, "b")
    fa, fb = _mk_file(conn, a.id, "a.py"), _mk_file(conn, b.id, "b.py")
    for ang in _NEAR:
        _mk_chunk(conn, a.id, fa, ang)
    for ang in _FAR:
        _mk_chunk(conn, b.id, fb, ang)
    return a, b


def _buggy_global_then_postfilter(conn: Connection, project_id: int, top_k: int):
    """Reproduces the historical defect verbatim: global ``k = top_k * 3``
    with NO prefilter inside the ``MATCH`` clause, ``project_id`` applied
    only in the outer ``WHERE`` — i.e. after vec0's hard pre-limit already
    truncated the candidate set. This is the buggy shape ``search._search_sqlite``
    used to have; it is reproduced here, not imported, since ``knowledge/`` is
    out of scope for this task and must not be changed to prove a negative."""
    q_vec = search.get_embedder().encode(["a0"])[0]
    sql = """
        SELECT c.id, v.distance
        FROM chunks_vec v
        JOIN chunks c ON c.id = v.chunk_id
        WHERE v.embedding MATCH ? AND k = ?
          AND c.project_id = ?
        ORDER BY v.distance ASC
        LIMIT ?
    """
    k_global = top_k * 3
    rows = conn.execute(
        sql, [q_vec.tobytes(), k_global, project_id, top_k]
    ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# 1. Positive: the real, fixed code returns the full top_k for the small pool
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("angle_embedder")
def test_real_search_returns_full_top_k_for_small_scoped_project(isolated_home):
    with db.connect() as conn:
        _a, b = _seed_two_projects(conn)

        hits = search.search(conn, "a0", project_id=b.id, top_k=5)

        assert len(hits) == 5, "project-B rows were starved — the fixed code regressed"
        assert {h.project_name for h in hits} == {b.name}
        assert all(h.rel_path == "b.py" for h in hits)


# ---------------------------------------------------------------------------
# 2. Negative control: the historical buggy shape DOES fail this exact setup
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("angle_embedder")
def test_historical_buggy_shape_starves_the_same_scoped_query(isolated_home):
    """Proves the guard above is not vacuous: on the identical seed data, the
    pre-fix SQL shape (global k, post-filtered project_id) returns FEWER than
    top_k rows for project B — reproducing the exact silent starvation this
    harness exists to catch. If this assertion ever stopped failing, the
    reproduction itself would no longer represent the historical bug."""
    with db.connect() as conn:
        _a, b = _seed_two_projects(conn)

        buggy_rows = _buggy_global_then_postfilter(conn, b.id, top_k=5)

        assert len(buggy_rows) == 0, (
            "the buggy global-k/post-filter shape was expected to starve "
            "project B (0 of 5 requested rows) — got "
            f"{len(buggy_rows)}; the negative control no longer reproduces "
            "the historical defect, so it can no longer prove the fix matters"
        )

        # And the real, prefiltered code path is unaffected by the same seed.
        fixed_hits = search.search(conn, "a0", project_id=b.id, top_k=5)
        assert len(fixed_hits) == 5
