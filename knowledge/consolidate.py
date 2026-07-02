"""Recurring-theme gap report: which history patterns aren't captured as decisions yet?

``knowledge consolidate`` surfaces *candidate* decisions by clustering recent
history entries semantically and checking whether each cluster is already
covered by an existing decision.  It is **strictly read-only** — no INSERTs,
no UPDATEs, no DELETEs anywhere in this module.

Intended use
------------
Run periodically (end of a work stretch, or every few weeks) when ``resume``
shows many history entries but few decisions.  The output lists clusters of
semantically similar entries alongside the closest existing decision (if any)
and a ready-to-paste ``knowledge decide`` scaffold.  The user or LLM reviews
the output and records real decisions with ``decide``.

Vector contract (IMPORTANT — do not violate)
--------------------------------------------
The embedder (``knowledge.embedder``) returns **L2-normalized float32** vectors
with ``normalize_embeddings=True``.  This means cosine similarity equals the
dot product.  All pure functions in this module accept pre-normalized
``np.ndarray`` inputs and MUST NOT re-normalize them — doing so would silently
corrupt the similarity values for already-unit-length vectors.

Design constraints (fixed)
--------------------------
* Read-only: auto-writing decisions would pollute the high-precision semantic
  layer, trigger false-positive conflict STOPs, and erode the trust the
  conflict check depends on.
* Semantic recurrence is the primary signal — structural file/tag overlap is
  too sparse on short summaries.
* Pure-function core so tests need no 130 MB model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np

from . import db, decisions as decisions_mod, history
from .db import Connection
from .embedder import get_embedder
from .resume import _PATH_RX


# ---------------------------------------------------------------------------
# Hardcoded English stopword set (~40 words) — no NLTK dependency
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    {
        # generic English function words
        "the", "and", "for", "are", "was", "with", "this", "that", "have",
        "from", "not", "but", "been", "had", "has", "its", "into", "will",
        "can", "also", "when", "use", "used", "using", "via", "per", "all",
        "any", "new", "one", "two", "out", "now", "get", "set", "run", "add",
        "fix", "our", "which",
        # process / milestone verbs — common in work-log summaries but not
        # subjects. Filtering them biases suggest_topic toward what the work
        # was ABOUT ("postgresql", "cache") rather than what was done to it
        # ("shipped", "added"). Deliberately excludes ambiguous base forms
        # that double as real subjects (update, build, test, change, support).
        "shipped", "ships", "added", "adds", "updated", "updates", "fixed",
        "fixes", "changed", "changes", "removed", "refactored", "implemented",
        "verified", "created", "merged", "moved", "renamed", "deleted",
        "made", "done", "wip", "tested",
    }
)


# ---------------------------------------------------------------------------
# Named tuples / dataclasses
# ---------------------------------------------------------------------------


class CoverageResult(NamedTuple):
    nearest_decision_idx: int | None
    max_sim: float
    is_covered: bool


@dataclass
class CandidateTheme:
    entries: list[history.HistoryEntry]
    suggested_topic: str
    files: list[str] = field(default_factory=list)
    nearest_decision: decisions_mod.Decision | None = None
    nearest_sim: float = 0.0
    cohesion: float = 0.0          # mean intra-cluster pairwise similarity
    truncated: bool = False        # True if the cluster was capped at max_size
    has_near_dupes: bool = False   # True if any intra-cluster pair > 0.97


@dataclass
class ConsolidateReport:
    project_name: str
    project_root: str
    scanned_history_n: int
    total_decisions: int
    candidates: list[CandidateTheme] = field(default_factory=list)
    covered_skipped_n: int = 0
    singletons_n: int = 0


# ---------------------------------------------------------------------------
# Pure functions (no I/O) — unit-tested with hand-built dim=4 vectors
# ---------------------------------------------------------------------------


def cluster_entries(
    vectors: np.ndarray,
    sim_threshold: float = 0.55,
    min_size: int = 2,
    linkage: str = "complete",
    max_size: int = 15,
) -> list[list[int]]:
    """Cluster pre-normalized vectors by pairwise cosine similarity.

    Parameters
    ----------
    vectors:
        Shape ``(N, D)``, pre-normalized (cosine sim == dot product).
        Returns ``[]`` when ``N == 0``.
    sim_threshold:
        Minimum similarity for two entries to be in the same group.
    min_size:
        Groups smaller than this are dropped (become singletons for the caller).
    linkage:
        ``"complete"`` — every intra-group pair must be ≥ threshold (prevents
        chaining).  ``"single"`` — merge when ANY existing member is ≥ threshold
        (may chain: A~B, B~C → {A,B,C} even if A≉C).
    max_size:
        Cap; the ``max_size`` most-central members are kept (highest sum of
        within-cluster similarity).  Excess members are dropped (become
        singletons).

    Returns
    -------
    List of groups, each a sorted list of original indices.  Groups ordered by
    their smallest member index ascending.  Deterministic for fixed input.

    Vector contract: inputs are pre-normalized; this function MUST NOT
    re-normalize them.
    """
    n = len(vectors)
    if n == 0:
        return []

    # Pairwise similarity matrix (cosine == dot product for L2-normalized vecs).
    sims: np.ndarray = vectors @ vectors.T  # shape (N, N)

    assigned = [False] * n
    groups: list[list[int]] = []

    for i in range(n):
        if assigned[i]:
            continue
        group = [i]
        assigned[i] = True

        for j in range(i + 1, n):
            if assigned[j]:
                continue
            if linkage == "complete":
                # ALL current members must be ≥ threshold.
                if all(float(sims[m, j]) >= sim_threshold for m in group):
                    group.append(j)
                    assigned[j] = True
            else:  # single
                # ANY current member ≥ threshold suffices.
                if any(float(sims[m, j]) >= sim_threshold for m in group):
                    group.append(j)
                    assigned[j] = True

        if len(group) < min_size:
            # Unassign so indices remain available as singletons for the caller.
            for idx in group:
                assigned[idx] = False
            continue

        # Cap to max_size most-central members (highest sum of within-cluster sim).
        if len(group) > max_size:
            sub = np.array(group)
            sub_sims = sims[np.ix_(sub, sub)]
            centrality = sub_sims.sum(axis=1)
            top_indices = np.argsort(centrality)[::-1][:max_size]
            group = sorted(int(sub[k]) for k in top_indices)

        groups.append(sorted(group))

    # Order groups by their smallest member index (deterministic).
    groups.sort(key=lambda g: g[0])
    return groups


def coverage_gap(
    cluster_vectors: np.ndarray,
    decision_vectors: np.ndarray,
    covered_threshold: float = 0.68,
) -> CoverageResult:
    """Check whether a cluster is already covered by an existing decision.

    Parameters
    ----------
    cluster_vectors:
        Shape ``(K, D)``, pre-normalized.
    decision_vectors:
        Shape ``(M, D)``, pre-normalized.  When ``M == 0`` (no decisions),
        returns ``CoverageResult(None, 0.0, False)`` — calling ``.max()``
        on a zero-size array would raise.
    covered_threshold:
        Cluster is considered covered when ``max_sim >= covered_threshold``.

    Vector contract: inputs are pre-normalized; this function MUST NOT
    re-normalize them.
    """
    m = decision_vectors.shape[0]
    if m == 0:
        return CoverageResult(nearest_decision_idx=None, max_sim=0.0, is_covered=False)

    # pair[i,j] = sim(cluster_vec_i, decision_vec_j)
    pair: np.ndarray = cluster_vectors @ decision_vectors.T  # shape (K, M)
    max_sim = float(pair.max())
    # Which decision column has the highest sim to ANY cluster vector?
    nearest_decision_idx = int(pair.max(axis=0).argmax())
    is_covered = max_sim >= covered_threshold
    return CoverageResult(
        nearest_decision_idx=nearest_decision_idx,
        max_sim=max_sim,
        is_covered=is_covered,
    )


def suggest_topic(short_summaries: list[str], tags: list[str]) -> str:
    """Derive a short topic label from cluster content — never empty.

    Fallback order:
    1. Most-frequent non-stopword token present in ≥60% of summaries.
    2. Most common non-empty tag (from the tags list).
    3. The first summary, truncated to ~60 chars.
    """
    _SPLIT_RE = re.compile(r"[^a-z0-9]+")
    n = len(short_summaries)

    if n > 0:
        # (1) Token frequency across summaries.
        token_in_summary: dict[str, int] = {}
        for s in short_summaries:
            tokens = set(_SPLIT_RE.split(s.lower())) - {""} - _STOPWORDS
            tokens = {t for t in tokens if len(t) >= 3}
            for t in tokens:
                token_in_summary[t] = token_in_summary.get(t, 0) + 1

        threshold = n * 0.6
        candidates = {t: c for t, c in token_in_summary.items() if c >= threshold}
        if candidates:
            best = max(candidates, key=lambda t: (candidates[t], t))
            return best

    # (2) Most common non-empty tag.
    non_empty_tags = [t.strip() for t in tags if t and t.strip()]
    if non_empty_tags:
        tag_counts: dict[str, int] = {}
        for t in non_empty_tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        return max(tag_counts, key=lambda t: (tag_counts[t], t))

    # (3) Truncated first summary.
    if short_summaries:
        return short_summaries[0][:60].strip() or "unknown"

    return "unknown"


# ---------------------------------------------------------------------------
# Orchestrator (I/O)
# ---------------------------------------------------------------------------


def build(
    conn: Connection,
    project_id: int,
    project_name: str,
    project_root: str,
    *,
    days: int = 90,
    limit: int = 100,
    sim_threshold: float = 0.55,
    covered_threshold: float = 0.68,
    min_size: int = 2,
) -> ConsolidateReport:
    """Build the consolidation report. Pure reads, no writes.

    Steps
    -----
    1. Fetch recent history entries.
    2. Embed short summaries in one batch; drop near-zero-norm rows.
    3. Fetch all decisions; embed ``topic :: decision`` strings in one batch.
    4. Cluster entries; compute coverage gap per cluster.
    5. Build CandidateTheme for uncovered clusters; skip covered ones.
    6. Collect file references intersected with indexed files.
    7. Sort candidates by (cluster size desc, cohesion desc).
    """
    entries = history.recent(conn, project_id=project_id, days=days, limit=limit)

    # ---- early exit: not enough history ----
    if len(entries) < 2:
        return ConsolidateReport(
            project_name=project_name,
            project_root=project_root,
            scanned_history_n=len(entries),
            total_decisions=0,
            candidates=[],
            covered_skipped_n=0,
            singletons_n=0,
        )

    # ---- embed history short summaries in ONE call ----
    hist_texts = [e.short_summary for e in entries]
    raw_vecs: np.ndarray = get_embedder().encode(hist_texts)

    # Drop entries whose vector norm is near-zero (empty / scrubbed summaries).
    norms = np.linalg.norm(raw_vecs, axis=1)
    valid_mask = norms >= 1e-6
    valid_entries = [e for e, ok in zip(entries, valid_mask) if ok]
    hist_vecs = raw_vecs[valid_mask]

    if len(valid_entries) < 2:
        return ConsolidateReport(
            project_name=project_name,
            project_root=project_root,
            scanned_history_n=len(entries),
            total_decisions=0,
            candidates=[],
            covered_skipped_n=0,
            singletons_n=0,
        )

    # ---- embed decisions in ONE call ----
    decs = decisions_mod.recent(conn, project_id=project_id, limit=10_000)
    dim = hist_vecs.shape[1]
    if decs:
        dec_texts = [f"{d.topic} :: {d.decision}" for d in decs]
        dec_vecs: np.ndarray = get_embedder().encode(dec_texts)
    else:
        dec_vecs = np.empty((0, dim), dtype=np.float32)

    # ---- cluster ----
    clusters = cluster_entries(hist_vecs, sim_threshold=sim_threshold, min_size=min_size)

    # Singletons = indices not in any cluster.
    clustered_indices: set[int] = set()
    for c in clusters:
        clustered_indices.update(c)
    singletons_n = len(valid_entries) - len(clustered_indices)

    # ---- per-cluster analysis ----
    covered_skipped_n = 0
    candidates: list[CandidateTheme] = []

    for cluster in clusters:
        cluster_arr = hist_vecs[cluster]
        cg = coverage_gap(cluster_arr, dec_vecs, covered_threshold)

        if cg.is_covered:
            covered_skipped_n += 1
            continue

        # Build CandidateTheme for uncovered cluster.
        cluster_entries_list = [valid_entries[i] for i in cluster]

        # Intra-cluster pairwise sims for cohesion + near-dupe detection.
        sub_sims = cluster_arr @ cluster_arr.T
        k = len(cluster)
        if k > 1:
            # Mean of upper-triangle (excluding diagonal).
            pairs = []
            for a in range(k):
                for b in range(a + 1, k):
                    pairs.append(float(sub_sims[a, b]))
            cohesion = float(np.mean(pairs))
            has_near_dupes = any(s > 0.97 for s in pairs)
        else:
            cohesion = 1.0
            has_near_dupes = False

        # Nearest decision.
        nearest_decision: decisions_mod.Decision | None = None
        if cg.nearest_decision_idx is not None and decs:
            nearest_decision = decs[cg.nearest_decision_idx]

        # Topic suggestion.
        all_tags: list[str] = []
        for e in cluster_entries_list:
            if e.tags:
                all_tags.extend(t.strip() for t in e.tags.split(","))
        suggested_topic = suggest_topic(
            [e.short_summary for e in cluster_entries_list], all_tags
        )

        # File references: regex scan all entries, intersect with indexed files.
        file_candidates: dict[str, int] = {}
        for e in cluster_entries_list:
            blob = (e.short_summary or "") + " " + (e.long_summary or "")
            for m in _PATH_RX.finditer(blob):
                p = m.group(0)
                file_candidates[p] = file_candidates.get(p, 0) + 1
        files: list[str] = []
        if file_candidates:
            fc_list = list(file_candidates.keys())
            placeholders = ",".join("?" * len(fc_list))
            rows = db.fetch_all(
                conn,
                f"SELECT rel_path FROM files "
                f"WHERE project_id = ? AND rel_path IN ({placeholders})",
                (project_id, *fc_list),
            )
            files = sorted(r[0] for r in rows)

        candidates.append(
            CandidateTheme(
                entries=cluster_entries_list,
                suggested_topic=suggested_topic,
                files=files,
                nearest_decision=nearest_decision,
                nearest_sim=cg.max_sim,
                cohesion=cohesion,
                truncated=False,  # cluster_entries caps internally; v1 keeps it simple
                has_near_dupes=has_near_dupes,
            )
        )

    # Sort by (cluster size desc, cohesion desc).
    candidates.sort(key=lambda c: (-len(c.entries), -c.cohesion))

    return ConsolidateReport(
        project_name=project_name,
        project_root=project_root,
        scanned_history_n=len(entries),
        total_decisions=len(decs),
        candidates=candidates,
        covered_skipped_n=covered_skipped_n,
        singletons_n=singletons_n,
    )
