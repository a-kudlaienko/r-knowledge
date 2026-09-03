"""Deterministic golden-query retrieval harness.

Zero LLM, zero judge model, no network. Builds a small, self-contained
fixture project (``tests/eval/fixtures/billing/``, three languages) with a
:class:`~tests.eval.fake_embedder.HashingBowEmbedder` — a feature-hashed
bag-of-words vectorizer that responds to real token overlap between a
natural-language query and real, chunked source code — then asserts
recall@5 and MRR against floors committed in ``golden_queries.yaml``. The
fixture tree is indexed as its own project; the repo itself is never
indexed, so scores cannot drift as this codebase grows.

This module intentionally does NOT index the real repo and does NOT cover
the two historical bug classes (project-scoping starvation, supersede
authority) — those have dedicated, purpose-built guards in
``test_scoping_guard.py`` / ``test_supersede_guard.py`` because they need
exact, arithmetic-controlled distances rather than lexical overlap.

Run only via ``-m eval`` (``make eval``); excluded from the default suite by
the ``pytest_collection_modifyitems`` hook in ``tests/eval/conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from knowledge import db, decisions as decisions_mod, indexer, search

from .fake_embedder import HashingBowEmbedder

pytestmark = pytest.mark.eval

GOLDEN_FILE = Path(__file__).parent / "golden_queries.yaml"
with open(GOLDEN_FILE, encoding="utf-8") as _f:
    GOLDEN = yaml.safe_load(_f)

QUERIES = GOLDEN["queries"]


def _recall_and_rank(found_keys: list[str], expect_keys: set[str]) -> tuple[float, int | None]:
    """``found_keys`` ranked best-first. Returns (recall@len(found), rank of
    the first expected key, 1-based, or ``None`` if none were found)."""
    if not expect_keys:
        return 0.0, None
    hit = expect_keys & set(found_keys)
    recall = len(hit) / len(expect_keys)
    for i, k in enumerate(found_keys, start=1):
        if k in expect_keys:
            return recall, i
    return recall, None


def _run_query(conn, project_id: int, q: dict) -> tuple[list[str], set[str]]:
    """Execute one golden query, returning (ranked found keys, expected keys)."""
    if q["verb"] == "search":
        hits = search.search(conn, q["query"], project_id=project_id, top_k=5)
        return [h.rel_path for h in hits], set(q["expect_paths"])
    if q["verb"] == "decisions_search":
        hits = decisions_mod.search(conn, q["query"], project_id=project_id, top_k=5)
        return [d.topic for d, _dist in hits], set(q["expect_topics"])
    raise ValueError(f"unknown golden-query verb {q['verb']!r} (id={q['id']})")


@pytest.fixture(scope="module")
def eval_env(tmp_path_factory):
    """Build the fixture project + seed decisions ONCE per module.

    Uses ``pytest.MonkeyPatch()`` directly (not the function-scoped
    ``monkeypatch`` fixture) so the patched embedder and redirected
    ``KNOWLEDGE_HOME`` survive for every query in this module — rebuilding
    a project (scan + chunk + embed) per parametrized query would be wasted,
    repeated work for what is otherwise a read-only fixture.
    """
    home = tmp_path_factory.mktemp("knowledge-home")
    mp = pytest.MonkeyPatch()
    mp.setenv("KNOWLEDGE_HOME", str(home))
    embedder = HashingBowEmbedder()
    mp.setattr(indexer, "get_local_embedder", lambda: embedder)
    mp.setattr(search, "get_embedder", lambda: embedder)
    mp.setattr(decisions_mod, "get_embedder", lambda: embedder)

    conn = db.connect_sqlite(home / "index.sqlite")
    fixture_root = Path(__file__).parent / GOLDEN["fixture_root"]
    project_id, _files, _chunks = indexer.build_project(conn, fixture_root, verbose=False)

    for d in GOLDEN.get("decisions", []):
        decisions_mod.add(
            conn,
            project_id=project_id,
            topic=d["topic"],
            decision=d["decision"],
            kind=d.get("kind", "decision"),
        )

    yield conn, project_id

    conn.close()
    mp.undo()


@pytest.mark.parametrize("q", QUERIES, ids=[q["id"] for q in QUERIES])
def test_golden_query_meets_recall_floor(eval_env, q):
    conn, project_id = eval_env
    found, expect = _run_query(conn, project_id, q)
    recall, _rank = _recall_and_rank(found, expect)
    floor = q["min_recall_at_5"]
    assert recall >= floor, (
        f"{q['id']}: recall@5={recall:.2f} below committed floor {floor} "
        f"(query={q['query']!r}, expected={sorted(expect)}, got={found})"
    )


def test_aggregate_recall_and_mrr_meet_floor(eval_env):
    conn, project_id = eval_env
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    for q in QUERIES:
        found, expect = _run_query(conn, project_id, q)
        recall, rank = _recall_and_rank(found, expect)
        recalls.append(recall)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)

    mean_recall = sum(recalls) / len(recalls)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
    agg = GOLDEN["aggregate"]
    assert mean_recall >= agg["min_mean_recall_at_5"], f"mean recall@5={mean_recall:.3f}"
    assert mrr >= agg["min_mrr"], f"MRR={mrr:.3f}"
