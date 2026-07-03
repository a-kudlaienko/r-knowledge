"""Hybrid retrieval: FTS5 lexical + vector semantic → RRF merge → static rerank.

``ask()`` is the agent-first search entry point. It runs both indexes,
merges via Reciprocal Rank Fusion (k=60 — parameter-free, well-behaved),
then applies a cheap rerank that boosts files an LLM would want to see
first: recently touched in git, referenced in the current session's
staged work, and central to the import graph.

Why both indexes instead of one?

* FTS5 is precise but lexical — it finds "the word in the file".
* Vector KNN captures semantic intent — "functions that do X-like things".

Neither alone gives the best top-K for a free-text agent query. RRF
combines their rankings without requiring score-calibration between the
two (distances and BM25 ranks aren't comparable).

Result caching: only the pre-rerank merged list is cached. Rerank inputs
change over time (new commits, new stage entries), so we always apply it
fresh — cheap, milliseconds.
"""

from __future__ import annotations

import math
import subprocess
from functools import lru_cache
from pathlib import Path

from . import config, db, fts, paths, query_cache, search
from .db import Connection
from .search import SearchResult


# RRF constant. Microsoft's 2009 paper uses 60; it's the canonical value
# that's insensitive to ranking noise and spreads weight reasonably.
_RRF_K = 60

# Over-fetch factor for each retrieval channel. Higher = better merge
# quality (more chunks overlap), at the cost of more JOIN work. 3× of
# requested top_k is a reasonable compromise; RRF rewards items that
# appear in both lists.
_OVER_FETCH = 3

# Rerank weights — calibrated so `session_boost > recent_boost > hub_boost`
# (session edits are the strongest signal of "what's hot right now").
_RECENT_DAYS = 30
_RECENT_BOOST = 0.01
_SESSION_BOOST = 0.02
_HUB_BOOST_COEFF = 0.005  # multiplied by log(1 + in_degree)


def ask(
    conn: Connection,
    query: str,
    project_id: int,
    project_root: Path,
    *,
    kind: str | None = None,
    lang: str | None = None,
    top_k: int = config.DEFAULT_TOP_K,
    use_cache: bool = True,
    index_stamp: float = 0.0,
) -> list[SearchResult]:
    """Full hybrid pipeline. Returns ``SearchResult`` list sorted by final score.

    The ``distance`` slot on each result is repurposed as ``-final_score``
    (lower is still "better" so the existing citations formatter doesn't
    need to flip signs). Call sites that care can recover the score as
    ``-r.distance``.

    ``index_stamp`` should be ``max(last_build, last_update)`` from the
    caller's already-fetched ``projects`` row — it's the cache's
    cross-client invalidation signal (see ``knowledge/query_cache.py``).
    Callers that don't have it (or don't care) can leave it at the
    default; the cache then just keys on ``0.0``, which still round-trips
    correctly but won't detect a stale index the way ``cmd_ask`` does.
    """
    fetch_k = max(top_k * _OVER_FETCH, 30)

    # Cache lookup (pre-rerank) — LOCAL file, no main-DB round trip. This
    # must stay before the embedder call in ``search.search`` below: a
    # cache hit must never pay the cost of loading the embedding model.
    cache_key = query_cache.compute_key(query, kind, lang, top_k)
    head_sha = query_cache.get_head_sha(project_root)
    cached = (
        query_cache.get(project_root, cache_key, head_sha, index_stamp)
        if use_cache else None
    )
    if cached is not None:
        return _rerank(conn, cached, project_id, project_root)[:top_k]

    # Both channels in one process — SQLite doesn't parallelize usefully
    # across a single connection, and the embedder call dominates anyway
    # (first call loads the model; subsequent are fast).
    vec_results = search.search(
        conn, query, project_id=project_id, kind=kind, lang=lang, top_k=fetch_k
    )
    fts_query = _to_fts_match(query)
    fts_results: list[SearchResult] = []
    if fts_query:
        try:
            fts_results = fts.grep(
                conn, fts_query, project_id=project_id,
                kind=kind, lang=lang, limit=fetch_k,
            )
        except Exception:
            # Malformed FTS5 query from adversarial input shouldn't kill
            # the whole ask — fall back to vec-only ranking.
            fts_results = []

    merged = _rrf_merge(vec_results, fts_results, limit=fetch_k)

    # Cache the pre-rerank output so the next call with the same query
    # skips FTS + vec + JOIN.
    if use_cache:
        query_cache.put(project_root, cache_key, head_sha, index_stamp, merged)

    return _rerank(conn, merged, project_id, project_root)[:top_k]


# ---------------------------------------------------------------------------
# Token-budget truncation
# ---------------------------------------------------------------------------


def truncate_to_budget(
    results: list[SearchResult],
    budget: int,
    tokens_per_citation: int = 40,
) -> tuple[list[SearchResult], int]:
    """Return ``(kept, omitted)`` sized to fit a soft token budget.

    ``tokens_per_citation`` is a rough estimate — one citation line
    (path + kind + excerpt) tokenizes to ~30–50 tokens in typical LLM
    tokenizers. The default 40 is a middle-ground we can re-tune.

    ``budget <= 0`` disables truncation (returns all results).
    """
    if budget <= 0 or not results:
        return results, 0
    max_items = max(1, budget // tokens_per_citation)
    if len(results) <= max_items:
        return results, 0
    return results[:max_items], len(results) - max_items


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _to_fts_match(query: str) -> str:
    """Map free-text to a safe FTS5 MATCH expression.

    FTS5 default tokenization treats unquoted bare words as AND. For
    free-text questions we want OR semantics (better recall). We also
    need to strip characters that have FTS5 special meaning (``"``,
    ``*``, ``(``, ``)``, ``+``, ``-`` at start of a token, etc).

    Strategy: tokenize on non-word chars, drop tokens under 2 chars,
    OR the rest. Returns empty string if nothing survives — caller
    then skips the FTS channel entirely.
    """
    import re

    tokens = re.findall(r"[A-Za-z0-9_]{2,}", query)
    if not tokens:
        return ""
    return " OR ".join(tokens)


def _rrf_merge(
    vec: list[SearchResult],
    fts_res: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    """Reciprocal Rank Fusion.

    For each chunk appearing in either ranked list:
        score_i = Σ 1/(k + rank_in_list_j)  over lists j

    RRF is parameter-free (besides k=60), doesn't need score
    normalization across the two ranking sources, and handles the
    "appears in one list only" case naturally.

    The returned ``SearchResult`` list carries ``distance = -rrf_score``
    so callers can reuse the existing "lower-is-better" conventions.
    """
    scores: dict[int, float] = {}
    originals: dict[int, SearchResult] = {}

    for rank, r in enumerate(vec):
        scores[r.chunk_id] = scores.get(r.chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
        originals.setdefault(r.chunk_id, r)

    for rank, r in enumerate(fts_res):
        scores[r.chunk_id] = scores.get(r.chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
        originals.setdefault(r.chunk_id, r)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [originals[cid]._replace(distance=-score) for cid, score in ranked]


def _rerank(
    conn: Connection,
    results: list[SearchResult],
    project_id: int,
    project_root: Path,
) -> list[SearchResult]:
    """Apply recency / session / in-degree boosts to RRF scores.

    Boost inputs are one-shot lookups — no per-result SQL. For 20–30
    results this is all under a millisecond.
    """
    if not results:
        return results

    recent = _recent_files(project_root)
    session = _session_files(project_root)
    in_degree = _in_degrees(conn, project_id, {r.rel_path for r in results})

    scored: list[tuple[float, SearchResult]] = []
    for r in results:
        base = -r.distance  # RRF score from _rrf_merge
        boost = 0.0
        if r.rel_path in recent:
            boost += _RECENT_BOOST
        if r.rel_path in session:
            boost += _SESSION_BOOST
        deg = in_degree.get(r.rel_path, 0)
        if deg > 0:
            boost += _HUB_BOOST_COEFF * math.log1p(deg)
        scored.append((base + boost, r))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [r._replace(distance=-score) for score, r in scored]


def _in_degrees(
    conn: Connection, project_id: int, rel_paths: set[str]
) -> dict[str, int]:
    """Per-file in-degree for a set of paths. One query."""
    if not rel_paths:
        return {}
    placeholders = ",".join("?" * len(rel_paths))
    rows = db.fetch_all(
        conn,
        f"""
        SELECT f.rel_path, COUNT(*) AS d
        FROM file_edges e
        JOIN files f ON f.id = e.target_file_id
        WHERE e.project_id = ?
          AND e.target_file_id IS NOT NULL
          AND f.rel_path IN ({placeholders})
        GROUP BY e.target_file_id, f.rel_path
        """,
        (project_id, *rel_paths),
    )
    return {r[0]: r[1] for r in rows}


@lru_cache(maxsize=8)
def _recent_files(project_root: Path) -> frozenset[str]:
    """Set of files touched in the last ``_RECENT_DAYS`` days (git log).

    Cached per-process keyed on project_root. Empty set for non-git repos
    or git errors — recency boost becomes a no-op, which is the right
    fallback (the other boosts still apply).
    """
    try:
        out = subprocess.run(
            [
                "git", "-C", str(project_root),
                "log", f"--since={_RECENT_DAYS}.days",
                "--name-only", "--pretty=format:",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return frozenset()
    if out.returncode != 0:
        return frozenset()
    return frozenset(ln.strip() for ln in out.stdout.splitlines() if ln.strip())


def _session_files(project_root: Path) -> frozenset[str]:
    """Files name-dropped in the current session's staged work-summary.

    Regex scan over ``short`` / ``long`` text in the per-session JSONL.
    Crude but cheap and directionally correct — files the agent just
    wrote notes about are almost certainly relevant to subsequent
    queries.
    """
    import json as _json
    import re

    stage = paths.session_stage_file(project_root)
    if not stage.exists():
        return frozenset()

    # Path-shaped tokens: at least one slash, ends with typical ext.
    path_rx = re.compile(
        r"\b[a-zA-Z0-9_.\-/]+\.(?:py|ts|tsx|js|jsx|yml|yaml|tf|tfvars|hcl|"
        r"md|json|sh|j2|tpl)\b"
    )

    hits: set[str] = set()
    try:
        with stage.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                blob = " ".join(
                    str(entry.get(k, ""))
                    for k in ("short", "long", "tags")
                )
                for m in path_rx.finditer(blob):
                    hits.add(m.group(0))
    except OSError:
        return frozenset()
    return frozenset(hits)
