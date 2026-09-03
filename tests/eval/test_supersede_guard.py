"""Regression guard: superseded ("dead") decisions must not outrank their own
correction in the ``knowledge ask`` preface.

Historical bug (see ``tests/test_supersede_retrieval.py`` docstring for the
full write-up this repo already has): ``decisions.search`` carries no
supersede predicate at all — a dead row and its correction are just two more
rows in the vector index — so a dead row could (and did, in production: dist
0.695) rank AHEAD of its own correction (dist 0.711). ``cmd_ask``'s fix is a
*composition*, not a change to ``decisions.search`` itself: over-fetch at
``ASK_DECISION_TOP_K * 2`` → gate by distance → drop superseded ids → THEN
trim to ``ASK_DECISION_TOP_K``. Trimming before dropping is the bug — a dead
row inside the first ``top_k`` raw hits would eat a slot that never gets
backfilled.

This guard is independent of ``tests/test_supersede_retrieval.py`` /
``tests/test_ask_decisions.py`` (both read in full before writing this file,
so as not to contradict their contract) — those pin the pure-function
behaviour (``_drop_superseded``, ``_filter_decision_hits``) against
hand-built distance tuples. This guard instead drives the real
``knowledge.decisions`` store end-to-end with controlled, exact distances,
and explicitly proves the negative: composing without the drop-superseded
step reproduces the historical defect on the same seed data.

Uses :class:`~tests.eval.fake_embedder.ControlledAngleEmbedder` (own,
independent implementation) so "which row is objectively closer" is
arithmetic, not luck.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge import config, db, decisions, projects
from knowledge.cli import _drop_superseded, _filter_decision_hits

from .fake_embedder import ControlledAngleEmbedder

pytestmark = pytest.mark.eval


@pytest.fixture()
def angle_embedder(monkeypatch):
    embedder = ControlledAngleEmbedder()
    monkeypatch.setattr(decisions, "get_embedder", lambda: embedder)
    return embedder


def _mk_project(conn, name: str) -> int:
    return projects.get_or_create_project(conn, Path(f"/tmp/eval-supersede-{name}")).id


def _seed_dead_and_correction(conn, project_id: int) -> tuple[int, int]:
    """The dead row sits CLOSER to the query (angle 10) than its own
    correction (angle 10.5) — reproduces the historically observed shape
    verbatim (dead row at dist 0.695 ahead of its correction at 0.711)."""
    old = decisions.add(conn, project_id=project_id, topic="a10", decision="v1: pool size 5")
    new = decisions.add(
        conn,
        project_id=project_id,
        topic="a10.5",
        decision="v2: pool size 10",
        supersedes=old,
        override_reason="v1 caused connection exhaustion under load",
    )
    return old, new


# ---------------------------------------------------------------------------
# 1. Positive: cmd_ask's real composition excludes the dead row
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("angle_embedder")
def test_correction_outranks_and_replaces_dead_row_in_ask_preface(isolated_home):
    with db.connect() as conn:
        pid = _mk_project(conn, "auth")
        old, new = _seed_dead_and_correction(conn, pid)

        top_k = config.ASK_DECISION_TOP_K
        raw_hits = decisions.search(conn, "a0", project_id=pid, top_k=top_k * 2)

        # Sanity: fixture really reproduces "dead nearer than its correction",
        # or the rest of this test proves nothing.
        dist_by_id = {d.id: dist for d, dist in raw_hits}
        assert dist_by_id[old] < dist_by_id[new], (
            "fixture must reproduce the historical shape: dead row closer "
            "to the query than its own correction"
        )

        # The exact cmd_ask composition: gate -> drop superseded -> trim.
        gated = _filter_decision_hits(raw_hits, config.ASK_DECISION_MAX_DISTANCE)
        dead = decisions.superseded_ids(conn, project_id=pid)
        live = _drop_superseded(gated, dead)[:top_k]

        assert old not in [d.id for d, _ in live], "dead row leaked into the ask preface"
        assert new in [d.id for d, _ in live], "its correction must be shown"
        assert live[0][0].id == new, "the correction must rank first among what's shown"


# ---------------------------------------------------------------------------
# 2. Negative control: omit the drop-superseded step, reproduce the bug
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("angle_embedder")
def test_would_have_failed_without_the_drop_superseded_step(isolated_home):
    """This is the historical bug, not a strawman: before ``cmd_ask`` grew
    the drop-superseded step, its preface was ``raw_hits[:top_k]`` — nothing
    removed the dead row, so the nearer dead row rendered first. Reproduced
    here by composing ``decisions.search``'s real output WITHOUT the fix's
    extra step, on the identical seed as the positive test above, to prove
    that guard is not vacuous."""
    with db.connect() as conn:
        pid = _mk_project(conn, "auth2")
        old, _new = _seed_dead_and_correction(conn, pid)

        top_k = config.ASK_DECISION_TOP_K
        raw_hits = decisions.search(conn, "a0", project_id=pid, top_k=top_k * 2)
        buggy_preface = raw_hits[:top_k]  # trim only — no supersede predicate anywhere

        assert buggy_preface[0][0].id == old, (
            "the negative control was expected to surface the dead row "
            "first (reproducing the historical defect); it no longer does, "
            "so it can no longer prove the drop-superseded step matters"
        )
